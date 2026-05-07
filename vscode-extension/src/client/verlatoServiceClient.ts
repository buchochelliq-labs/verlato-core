import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import * as vscode from "vscode";

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

export class VerlatoServiceClient {
  private child: ChildProcessWithoutNullStreams | undefined;
  private remainder = "";
  private nextId = 1;
  private stopping = false;
  private readonly pending = new Map<string, PendingRequest>();

  constructor(
    private readonly command: string,
    private readonly args: string[],
    private readonly cwd: string,
    private readonly env: NodeJS.ProcessEnv,
    private readonly output: vscode.OutputChannel,
  ) {}

  get isRunning(): boolean {
    return Boolean(this.child && !this.child.killed);
  }

  async request<T>(method: string, params: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
    this.start();
    const child = this.child;
    if (!child) {
      throw new Error("Verlato service is not running.");
    }
    if (signal?.aborted) {
      throw new Error("Verlato service request cancelled.");
    }
    const id = String(this.nextId++);
    const payload = `${JSON.stringify({ id, method, params })}\n`;
    return new Promise<T>((resolve, reject) => {
      const abort = () => {
        this.pending.delete(id);
        reject(new Error("Verlato service request cancelled."));
      };
      if (signal) {
        signal.addEventListener("abort", abort, { once: true });
      }
      this.pending.set(id, {
        resolve: (value) => {
          signal?.removeEventListener("abort", abort);
          resolve(value as T);
        },
        reject: (error) => {
          signal?.removeEventListener("abort", abort);
          reject(error);
        },
      });
      child.stdin.write(payload, (error?: Error | null) => {
        if (!error) {
          return;
        }
        this.pending.delete(id);
        signal?.removeEventListener("abort", abort);
        reject(error);
      });
    });
  }

  stop(reason = "Verlato service stopped."): void {
    const child = this.child;
    this.child = undefined;
    this.stopping = true;
    this.failPending(new Error(reason));
    if (!child || child.killed) {
      return;
    }
    try {
      child.stdin.end();
    } catch {
      // The process may already be gone.
    }
    child.kill();
  }

  private start(): void {
    if (this.isRunning) {
      return;
    }
    this.stopping = false;
    const child = spawn(this.command, this.args, {
      cwd: this.cwd,
      env: this.env,
      windowsHide: true,
    });
    this.child = child;
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => this.handleStdout(chunk));
    child.stderr.on("data", (chunk: string) => this.output.appendLine(chunk));
    child.on("error", (error) => {
      if (this.child === child) {
        this.child = undefined;
        this.failPending(error);
      }
    });
    child.on("close", (code) => {
      if (this.child !== child) {
        return;
      }
      this.child = undefined;
      if (this.stopping) {
        return;
      }
      this.failPending(new Error(`Verlato service exited with code ${code ?? "unknown"}.`));
    });
  }

  private handleStdout(chunk: string): void {
    const combined = `${this.remainder}${chunk}`;
    const hasTrailingNewline = /\r?\n$/.test(combined);
    const lines = combined.split(/\r?\n/);
    const completeLines = hasTrailingNewline ? lines : lines.slice(0, -1);
    this.remainder = hasTrailingNewline ? "" : (lines[lines.length - 1] ?? "");
    for (const line of completeLines) {
      if (line.trim()) {
        this.handleResponseLine(line);
      }
    }
  }

  private handleResponseLine(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      this.output.appendLine(`Invalid Verlato service response: ${line}`);
      return;
    }
    if (!parsed || typeof parsed !== "object") {
      return;
    }
    const record = parsed as Record<string, unknown>;
    const id = typeof record.id === "string" || typeof record.id === "number" ? String(record.id) : "";
    const pending = this.pending.get(id);
    if (!pending) {
      return;
    }
    this.pending.delete(id);
    if (record.ok === true) {
      pending.resolve(record.result);
      return;
    }
    const error = record.error && typeof record.error === "object"
      ? record.error as Record<string, unknown>
      : {};
    pending.reject(new Error(typeof error.message === "string" ? error.message : "Verlato service request failed."));
  }

  private failPending(error: Error): void {
    const pending = [...this.pending.values()];
    this.pending.clear();
    for (const request of pending) {
      request.reject(error);
    }
  }
}
