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
  DraftPromptPreview,
  DraftRangeRequest,
  ModelSettings,
  RepositoryCommit,
  RepositoryOverview,
  RepositoryRef,
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

  async getModelPrompt(request: DraftRangeRequest, itemId: string): Promise<DraftPromptPreview> {
    return this.runJsonCommand<DraftPromptPreview>([
      "draft",
      "prompt",
      "--base",
      request.base,
      "--head",
      request.head ?? "HEAD",
      "--id",
      itemId,
    ]);
  }

  async getRepositoryOverview(): Promise<RepositoryOverview> {
    const [branchesRaw, tagsRaw, commitsRaw, state] = await Promise.all([
      this.runGitCommand(["branch", "--format=%(refname:short)%09%(objectname:short)"]),
      this.runGitCommand(["tag", "--sort=-creatordate", "--format=%(refname:short)%09%(creatordate:short)"]),
      this.runGitCommand(["log", "--date=short", "--pretty=format:%h%x09%s%x09%an%x09%ad", "-n", "50"]),
      this.readState(),
    ]);
    const releases = summarizeReleases(state.items);
    const releaseByCommit = mapReleaseByCommit(state.items);
    return {
      branches: parseRefs(branchesRaw),
      tags: parseRefs(tagsRaw),
      commits: parseCommits(commitsRaw, releaseByCommit),
      releases,
      unpublishedCount: state.items.filter((item) => !item.published).length,
    };
  }

  getModelSettings(): ModelSettings {
    const config = vscode.workspace.getConfiguration("releasepilot");
    const provider = config.get<string>("modelProvider") || "ollama";
    return {
      provider,
      model: config.get<string>("ollamaModel") || "gemma4:31b",
      endpoint: config.get<string>("ollamaEndpoint") || "http://127.0.0.1:11434/api/chat",
      maxModelItems: config.get<number>("maxModelItems") ?? 25,
    };
  }

  async updateModelSettings(settings: Partial<ModelSettings>): Promise<void> {
    const config = vscode.workspace.getConfiguration("releasepilot");
    if (settings.provider !== undefined) {
      await config.update("modelProvider", settings.provider, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.model !== undefined) {
      await config.update("ollamaModel", settings.model, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.endpoint !== undefined) {
      await config.update("ollamaEndpoint", settings.endpoint, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.maxModelItems !== undefined) {
      await config.update("maxModelItems", settings.maxModelItems, vscode.ConfigurationTarget.Workspace);
    }
  }

  async initialize(): Promise<void> {
    const resolution = this.resolveExecutable();
    const modelSettings = this.getModelSettings();
    this.output.appendLine(`Workspace: ${this.workspaceFolder.uri.fsPath}`);
    this.output.appendLine(`Extension: ${this.extensionPath}`);
    this.output.appendLine(`ReleasePilot CLI: ${resolution.executable}`);
    this.output.appendLine(`Model: ${formatModelSettings(modelSettings)}`);
    this.emitStatus({
      mode: "cli",
      label: `ReleasePilot: ${modelSettings.provider}`,
      detail: `Workspace: ${this.workspaceFolder.uri.fsPath}\nCLI: ${resolution.executable}\nModel: ${formatModelSettings(modelSettings)}`,
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
      `Model: ${formatModelSettings(this.getModelSettings())}`,
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

  private async runGitCommand(args: string[]): Promise<string> {
    const result = await execFileAsync("git", args, {
      cwd: this.workspaceFolder.uri.fsPath,
      encoding: "utf8",
    });
    return result.stdout;
  }

  private async readState(): Promise<StatePayload> {
    try {
      const state = await this.runJsonCommand<StatePayload>(["state", "show"]);
      return { items: state.items ?? [] };
    } catch (error) {
      this.output.appendLine(`ReleasePilot state unavailable: ${String(error)}`);
      return { items: [] };
    }
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
    const settings = this.getModelSettings();
    const provider = settings.provider;
    const env = { ...process.env };
    env.RELEASEPILOT_OLLAMA_MODEL = settings.model;
    env.RELEASEPILOT_OLLAMA_ENDPOINT = settings.endpoint;
    env.RELEASEPILOT_MODEL_MAX_ITEMS = String(settings.maxModelItems);
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
    const shouldAttachToken = shouldAttachGithubToken(args, provider);
    if (env.GITHUB_TOKEN || env.GH_TOKEN || !shouldAttachToken) {
      return env;
    }
    try {
      this.output.appendLine("GitHub authentication token not found in environment; requesting VS Code GitHub session.");
      const session = await vscode.authentication.getSession("github", ["read:user"], { createIfNone: true });
      if (session?.accessToken) {
        env.GITHUB_TOKEN = session.accessToken;
        env.GH_TOKEN = session.accessToken;
        this.output.appendLine("Attached VS Code GitHub authentication token for ReleasePilot model request.");
      } else {
        this.output.appendLine("VS Code did not return a GitHub authentication token for ReleasePilot model request.");
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
  return provider === "github-copilot" && args[0] === "draft" && args[1] === "suggest";
}

interface StatePayload {
  items: StateItem[];
}

interface StateItem {
  id: string;
  published?: boolean;
  published_in?: string | null;
  source?: {
    pr_number?: number | null;
    commit_shas?: string[];
  };
}

function parseRefs(raw: string): RepositoryRef[] {
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      const [name, detail] = line.split("\t", 2);
      return { name, detail };
    });
}

function parseCommits(raw: string, releaseByCommit: Map<string, string>): RepositoryCommit[] {
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      const [sha, subject, author, date] = line.split("\t");
      return {
        sha,
        subject,
        author: `${author || "unknown"}${date ? `, ${date}` : ""}`,
        prNumber: extractPrNumber(subject),
        release: releaseByCommit.get(sha) ?? null,
      };
    });
}

function extractPrNumber(subject: string): number | null {
  const match = subject.match(/\(#(\d+)\)|pull request #(\d+)|merge pr #(\d+)/i);
  const value = match?.[1] ?? match?.[2] ?? match?.[3];
  return value ? Number(value) : null;
}

function summarizeReleases(items: StateItem[]): { version: string; itemCount: number }[] {
  const counts = new Map<string, number>();
  for (const item of items) {
    if (!item.published || !item.published_in) {
      continue;
    }
    counts.set(item.published_in, (counts.get(item.published_in) ?? 0) + 1);
  }
  return [...counts.entries()].map(([version, itemCount]) => ({ version, itemCount }));
}

function mapReleaseByCommit(items: StateItem[]): Map<string, string> {
  const byCommit = new Map<string, string>();
  for (const item of items) {
    if (!item.published || !item.published_in) {
      continue;
    }
    for (const sha of item.source?.commit_shas ?? []) {
      byCommit.set(sha.slice(0, 7), item.published_in);
    }
  }
  return byCommit;
}

function formatModelSettings(settings: ModelSettings): string {
  const model = settings.provider === "ollama" ? `/${settings.model}` : "";
  return `${settings.provider}${model}; fanout guard: ${settings.maxModelItems}`;
}
