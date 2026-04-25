import * as vscode from "vscode";
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { promisify } from "node:util";

import {
  ApplyResult,
  ClientStatus,
  DraftDocument,
  DraftItem,
  DraftRangeRequest,
  ReviewUpdateRequest,
} from "./types";

const execFileAsync = promisify(execFile);

export class ReleasePilotClient implements vscode.Disposable {
  private readonly onDidChangeStatusEmitter = new vscode.EventEmitter<ClientStatus>();
  readonly onDidChangeStatus = this.onDidChangeStatusEmitter.event;
  private lastStatus: ClientStatus = { mode: "cli", label: "ReleasePilot: CLI", detail: "Using direct CLI commands" };
  private lastResolution: ExecutableResolution | undefined;

  constructor(
    private readonly workspaceFolder: vscode.WorkspaceFolder,
    private readonly extensionPath: string,
    private readonly output: vscode.OutputChannel,
  ) {
    this.emitStatus(this.lastStatus);
  }

  async getDraft(request: DraftRangeRequest): Promise<DraftDocument> {
    return this.runJsonCommand<DraftDocument>([
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
    ]);
  }

  async previewDraft(request: DraftRangeRequest): Promise<string> {
    const { stdout } = await this.runCommand([
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
    ]);
    return stdout;
  }

  async applyDraft(request: DraftRangeRequest, target: "changelog" | "github-release"): Promise<ApplyResult> {
    return this.runJsonCommand<ApplyResult>([
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
    ]);
  }

  async reviewDraftItem(
    request: DraftRangeRequest,
    itemId: string,
    update: ReviewUpdateRequest,
  ): Promise<DraftItem> {
    return this.runJsonCommand<DraftItem>([
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
    ]);
  }

  async requestModelInput(request: DraftRangeRequest, itemId: string, reviewer?: string): Promise<DraftItem> {
    return this.runJsonCommand<DraftItem>([
      "draft",
      "suggest",
      "--base",
      request.base,
      "--head",
      request.head ?? "HEAD",
      "--id",
      itemId,
      ...(reviewer ? ["--reviewer", reviewer] : []),
    ]);
  }

  async initialize(): Promise<void> {
    const resolution = this.resolveExecutable();
    this.output.appendLine(`Workspace: ${this.workspaceFolder.uri.fsPath}`);
    this.output.appendLine(`Extension: ${this.extensionPath}`);
    this.output.appendLine(`ReleasePilot CLI: ${resolution.executable}`);
    this.emitStatus({
      mode: "cli",
      label: "ReleasePilot: CLI",
      detail: `Workspace: ${this.workspaceFolder.uri.fsPath}\nCLI: ${resolution.executable}`,
    });
  }

  dispose(): void {
    this.onDidChangeStatusEmitter.dispose();
  }

  getDiagnosticInfo(): string {
    const resolution = this.lastResolution ?? this.resolveExecutable();
    return [
      `Workspace: ${this.workspaceFolder.uri.fsPath}`,
      `Extension: ${this.extensionPath}`,
      `CLI: ${resolution.executable}`,
      `Searched: ${resolution.searched.join(", ")}`,
    ].join("\n");
  }

  private async runJsonCommand<T>(args: string[]): Promise<T> {
    const { stdout } = await this.runCommand(args);
    return JSON.parse(stdout) as T;
  }

  private async runCommand(args: string[]): Promise<{ stdout: string; stderr: string }> {
    const resolution = this.resolveExecutable();
    const executable = resolution.executable;
    this.output.appendLine(`$ ${executable} ${args.join(" ")}`);
    let result: { stdout: string; stderr: string };
    try {
      result = await execFileAsync(executable, args, {
        cwd: this.workspaceFolder.uri.fsPath,
        encoding: "utf8",
        env: await this.buildEnvironment(args),
      });
    } catch (error) {
      if (isMissingExecutableError(error)) {
        throw new Error(
          `ReleasePilot CLI was not found. Tried: ${resolution.searched.join(", ")}. Install the backend package or set releasepilot.cliPath to the full path of releasepilot.exe.`,
        );
      }
      throw error;
    }
    if (result.stderr) {
      this.output.appendLine(result.stderr);
    }
    return result;
  }

  private resolveExecutable(): ExecutableResolution {
    const configuredPath = vscode.workspace.getConfiguration("releasepilot").get<string>("cliPath") || "releasepilot";
    if (configuredPath !== "releasepilot") {
      this.lastResolution = { executable: configuredPath, searched: [configuredPath] };
      return this.lastResolution;
    }

    const workspaceCli = getVenvExecutable(this.workspaceFolder.uri.fsPath);
    const extensionCheckoutCli = getVenvExecutable(path.dirname(this.extensionPath));
    const candidates = [workspaceCli, extensionCheckoutCli];
    const executable = candidates.find((candidate) => fs.existsSync(candidate)) ?? configuredPath;

    this.lastResolution = { executable, searched: [...candidates, configuredPath] };
    return this.lastResolution;
  }

  private emitStatus(status: ClientStatus): void {
    this.lastStatus = status;
    this.onDidChangeStatusEmitter.fire(status);
  }

  private async buildEnvironment(args: string[]): Promise<NodeJS.ProcessEnv> {
    const provider = vscode.workspace.getConfiguration("releasepilot").get<string>("modelProvider") || "configured";
    const env = { ...process.env };
    if (provider === "configured") {
      return this.withGithubAuthToken(env, args, provider);
    }
    env.RELEASEPILOT_MODEL_PROVIDER = provider;
    return this.withGithubAuthToken(env, args, provider);
  }

  private async withGithubAuthToken(
    env: NodeJS.ProcessEnv,
    args: string[],
    provider: string,
  ): Promise<NodeJS.ProcessEnv> {
    if (env.GITHUB_TOKEN || env.GH_TOKEN || !shouldAttachGithubToken(args, provider)) {
      return env;
    }
    try {
      const session = await vscode.authentication.getSession("github", ["read:user"], { createIfNone: false });
      if (session?.accessToken) {
        env.GITHUB_TOKEN = session.accessToken;
      }
    } catch (error) {
      this.output.appendLine(`GitHub authentication token unavailable: ${String(error)}`);
    }
    return env;
  }
}

function isMissingExecutableError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT";
}

interface ExecutableResolution {
  executable: string;
  searched: string[];
}

function getVenvExecutable(rootPath: string): string {
  return process.platform === "win32"
    ? path.join(rootPath, ".venv", "Scripts", "releasepilot.exe")
    : path.join(rootPath, ".venv", "bin", "releasepilot");
}

function shouldAttachGithubToken(args: string[], provider: string): boolean {
  if (provider === "github-copilot") {
    return true;
  }
  return provider === "configured" && args[0] === "draft" && args[1] === "suggest";
}
