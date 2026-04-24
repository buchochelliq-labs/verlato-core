import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, execFile, spawn } from "node:child_process";
import * as readline from "node:readline";
import { promisify } from "node:util";

import { ApplyResult, DraftDocument, DraftItem, DraftRangeRequest, ReviewUpdateRequest } from "./types";

const execFileAsync = promisify(execFile);

interface PendingRequest {
  resolve: (value: any) => void;
  reject: (reason?: unknown) => void;
  timeout: NodeJS.Timeout;
}

export class ReleasePilotClient implements vscode.Disposable {
  private serviceProcess: ChildProcessWithoutNullStreams | undefined;
  private serviceReader: readline.Interface | undefined;
  private nextRequestId = 1;
  private readonly pending = new Map<string, PendingRequest>();

  constructor(
    private readonly workspaceFolder: vscode.WorkspaceFolder,
    private readonly output: vscode.OutputChannel,
  ) {}

  async getDraft(request: DraftRangeRequest): Promise<DraftDocument> {
    return this.runServiceFirst<DraftDocument>(
      "draft.getDocument",
      this.toServiceRangeParams(request),
      [
        "draft",
        "json",
        "--base",
        request.base,
        "--head",
        request.head ?? "HEAD",
        "--version",
        request.version ?? "Unreleased",
        ...(request.includeConfidence ? ["--include-confidence"] : []),
        ...(request.includeProvenance ? ["--include-provenance"] : []),
      ],
    );
  }

  async previewDraft(request: DraftRangeRequest): Promise<string> {
    return this.runServiceFirst<string>(
      "draft.preview",
      this.toServiceRangeParams(request),
      [
        "draft",
        "preview",
        "--base",
        request.base,
        "--head",
        request.head ?? "HEAD",
        "--version",
        request.version ?? "Unreleased",
        ...(request.includeConfidence ? ["--include-confidence"] : []),
        ...(request.includeProvenance ? ["--include-provenance"] : []),
      ],
    );
  }

  async applyDraft(request: DraftRangeRequest, target: "changelog" | "github-release"): Promise<ApplyResult> {
    return this.runServiceFirst<ApplyResult>(
      "draft.apply",
      {
        ...this.toServiceRangeParams(request),
        target,
      },
      [
        "draft",
        "apply",
        "--base",
        request.base,
        "--head",
        request.head ?? "HEAD",
        "--version",
        request.version ?? "Unreleased",
        "--target",
        target,
      ],
    );
  }

  async reviewDraftItem(
    request: DraftRangeRequest,
    itemId: string,
    update: ReviewUpdateRequest,
  ): Promise<DraftItem> {
    return this.runServiceFirst<DraftItem>(
      "draft.review",
      {
        ...this.toServiceRangeParams(request),
        id: itemId,
        ...update,
      },
      [
        "draft",
        "review",
        "--base",
        request.base,
        "--head",
        request.head ?? "HEAD",
        "--id",
        itemId,
        ...(update.status ? ["--status", update.status] : []),
        ...(update.summary !== undefined ? ["--summary", update.summary] : []),
        ...(update.reviewer ? ["--reviewer", update.reviewer] : []),
      ],
    );
  }

  dispose(): void {
    this.shutdownServiceProcess();
  }

  private async runJsonCommand<T>(args: string[]): Promise<T> {
    const { stdout } = await this.runCommand(args);
    return JSON.parse(stdout) as T;
  }

  private async runServiceFirst<T>(method: string, params: Record<string, unknown>, fallbackArgs: string[]): Promise<T> {
    const configuration = vscode.workspace.getConfiguration("releasepilot");
    if (configuration.get<boolean>("preferLocalService", true)) {
      try {
        return await this.runServiceRequest<T>(method, params);
      } catch (error) {
        this.output.appendLine(`[service-fallback] ${String(error)}`);
      }
    }
    return this.runJsonCommand<T>(fallbackArgs);
  }

  private async runServiceRequest<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const process = await this.ensureServiceProcess();
    const requestId = String(this.nextRequestId++);
    const timeoutMs = vscode.workspace.getConfiguration("releasepilot").get<number>("requestTimeoutMs", 10000);
    const payload = JSON.stringify({ id: requestId, method, params });
    this.output.appendLine(`$ [service] ${payload}`);

    const result = await new Promise<T>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(`ReleasePilot service timed out for ${method}`));
      }, timeoutMs);
      this.pending.set(requestId, { resolve, reject, timeout });
      process.stdin.write(payload + "\n");
    });
    return result;
  }

  private async runCommand(args: string[]): Promise<{ stdout: string; stderr: string }> {
    const executable = vscode.workspace.getConfiguration("releasepilot").get<string>("cliPath") || "releasepilot";
    this.output.appendLine(`$ ${executable} ${args.join(" ")}`);
    const result = await execFileAsync(executable, args, {
      cwd: this.workspaceFolder.uri.fsPath,
      encoding: "utf8",
    });
    if (result.stderr) {
      this.output.appendLine(result.stderr);
    }
    return result;
  }

  private async ensureServiceProcess(): Promise<ChildProcessWithoutNullStreams> {
    if (this.serviceProcess && !this.serviceProcess.killed) {
      return this.serviceProcess;
    }

    const executable = vscode.workspace.getConfiguration("releasepilot").get<string>("cliPath") || "releasepilot";
    const configuredArgs = vscode.workspace.getConfiguration("releasepilot").get<string[]>("serviceArgs", ["serve"]);
    this.output.appendLine(`$ ${executable} ${configuredArgs.join(" ")}`);

    const child = spawn(executable, configuredArgs, {
      cwd: this.workspaceFolder.uri.fsPath,
      stdio: "pipe",
    });
    this.serviceProcess = child;
    this.serviceReader = readline.createInterface({ input: child.stdout });
    this.serviceReader.on("line", (line) => {
      this.handleServiceLine(line);
    });
    child.stderr.on("data", (chunk: Buffer | string) => {
      this.output.appendLine(String(chunk));
    });
    child.on("error", (error) => {
      this.failPendingRequests(error);
      this.shutdownServiceProcess();
    });
    child.on("exit", (code, signal) => {
      this.failPendingRequests(new Error(`ReleasePilot service exited (${code ?? "null"}${signal ? `, ${signal}` : ""})`));
      this.shutdownServiceProcess();
    });
    return child;
  }

  private handleServiceLine(line: string): void {
    this.output.appendLine(`[service] ${line}`);
    let payload: { id?: string; result?: unknown; error?: { message?: string } };
    try {
      payload = JSON.parse(line) as { id?: string; result?: unknown; error?: { message?: string } };
    } catch (error) {
      this.output.appendLine(`[service-error] ${String(error)}`);
      return;
    }
    const requestId = payload.id;
    if (!requestId) {
      return;
    }
    const pending = this.pending.get(requestId);
    if (!pending) {
      return;
    }
    clearTimeout(pending.timeout);
    this.pending.delete(requestId);
    if (payload.error) {
      pending.reject(new Error(payload.error.message || "ReleasePilot service request failed"));
      return;
    }
    pending.resolve(payload.result as unknown);
  }

  private failPendingRequests(error: unknown): void {
    for (const [requestId, pending] of this.pending.entries()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
      this.pending.delete(requestId);
    }
  }

  private shutdownServiceProcess(): void {
    this.serviceReader?.close();
    this.serviceReader = undefined;
    if (this.serviceProcess && !this.serviceProcess.killed) {
      this.serviceProcess.kill();
    }
    this.serviceProcess = undefined;
  }

  private toServiceRangeParams(request: DraftRangeRequest): Record<string, unknown> {
    return {
      base: request.base,
      head: request.head ?? "HEAD",
      version: request.version ?? "Unreleased",
      includeConfidence: request.includeConfidence ?? false,
      includeProvenance: request.includeProvenance ?? false,
    };
  }
}
