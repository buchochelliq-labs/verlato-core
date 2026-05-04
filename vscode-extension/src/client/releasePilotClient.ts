import * as vscode from "vscode";
import { spawn } from "node:child_process";
import * as fs from "node:fs";

import { ExecutableResolution, resolveReleasePilotExecutable } from "./cliResolution";
import { resolveCommandModelProvider, shouldAttachGithubToken } from "./modelEnvironment";
import {
  ApplyResult,
  ClientStatus,
  CommandRequestSnapshot,
  CommandRequestStatus,
  DraftDocument,
  DraftItem,
  DraftPromptPreview,
  DraftRangeRequest,
  DraftSuggestionPreview,
  ModelSettings,
  RepositoryCommit,
  RepositoryOverview,
  RepositoryRef,
  ReviewUpdateRequest,
} from "./types";

export class ReleasePilotClient implements vscode.Disposable {
  private readonly onDidChangeStatusEmitter = new vscode.EventEmitter<ClientStatus>();
  readonly onDidChangeStatus = this.onDidChangeStatusEmitter.event;
  private readonly onDidChangeRequestsEmitter = new vscode.EventEmitter<CommandRequestSnapshot[]>();
  readonly onDidChangeRequests = this.onDidChangeRequestsEmitter.event;
  private lastStatus: ClientStatus = { mode: "cli", label: "ReleasePilot: CLI", detail: "Using direct CLI commands" };
  private lastResolution: ExecutableResolution | undefined;
  private readonly activeCommandControllers = new Map<number, AbortController>();
  private readonly commandRequests = new Map<number, CommandRequestSnapshot>();
  private readonly activityRemainders = new Map<string, string>();
  private nextCommandId = 1;
  private nextActivityId = 1;

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
      ...(update.summaryProvenance ? ["--summary-provenance-json", JSON.stringify(update.summaryProvenance)] : []),
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

  async previewDraftSuggestions(request: DraftRangeRequest, itemId?: string): Promise<DraftSuggestionPreview> {
    const settings = this.getModelSettings();
    return this.runJsonCommand<DraftSuggestionPreview>([
      "draft",
      "suggest",
      "--base",
      request.base,
      "--head",
      request.head ?? "HEAD",
      "--preview",
      "--count",
      String(settings.suggestionCount),
      "--tone",
      settings.suggestionTone,
      ...(itemId ? ["--id", itemId] : ["--all-visible"]),
    ]);
  }

  async recordFeedback(event: string, itemId: string, metadata: Record<string, unknown> = {}): Promise<void> {
    await this.runJsonCommand<Record<string, never>>([
      "feedback",
      "record",
      "--event",
      event,
      "--item-id",
      itemId,
      "--metadata-json",
      JSON.stringify(metadata),
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
      model: config.get<string>("ollamaModel") || "gemma4:latest",
      endpoint: config.get<string>("ollamaEndpoint") || "http://127.0.0.1:11434/api/chat",
      ollamaTimeoutSeconds: config.get<number>("ollamaTimeoutSeconds") ?? 120,
      maxModelItems: config.get<number>("maxModelItems") ?? 25,
      suggestionCount: config.get<number>("suggestionCount") ?? 1,
      suggestionTone: config.get<string>("suggestionTone") || "release-note",
      styleGuidance: config.get<string>("styleGuidance") || "",
      projectVocabulary: config.get<string[]>("projectVocabulary") ?? [],
      bannedPhrases: config.get<string[]>("bannedPhrases") ?? [],
      specificityFloor: config.get<number>("specificityFloor") ?? 0,
      feedbackIncludeText: config.get<boolean>("feedbackIncludeText") ?? false,
      includePromptPayloads: config.get<boolean>("telemetry.includePromptPayloads") ?? false,
      includeReasoningPayloads: config.get<boolean>("telemetry.includeReasoningPayloads") ?? false,
      redactPayloads: config.get<boolean>("telemetry.redactPayloads") ?? true,
      debugModelLogPath: config.get<string>("telemetry.debugModelLogPath") || "",
      ollamaStreamResponses: config.get<boolean>("ollamaStreamResponses") ?? true,
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
    if (settings.ollamaTimeoutSeconds !== undefined) {
      await config.update("ollamaTimeoutSeconds", settings.ollamaTimeoutSeconds, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.maxModelItems !== undefined) {
      await config.update("maxModelItems", settings.maxModelItems, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.suggestionCount !== undefined) {
      await config.update("suggestionCount", settings.suggestionCount, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.suggestionTone !== undefined) {
      await config.update("suggestionTone", settings.suggestionTone, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.styleGuidance !== undefined) {
      await config.update("styleGuidance", settings.styleGuidance, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.projectVocabulary !== undefined) {
      await config.update("projectVocabulary", settings.projectVocabulary, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.bannedPhrases !== undefined) {
      await config.update("bannedPhrases", settings.bannedPhrases, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.specificityFloor !== undefined) {
      await config.update("specificityFloor", settings.specificityFloor, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.feedbackIncludeText !== undefined) {
      await config.update("feedbackIncludeText", settings.feedbackIncludeText, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.includePromptPayloads !== undefined) {
      await config.update("telemetry.includePromptPayloads", settings.includePromptPayloads, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.includeReasoningPayloads !== undefined) {
      await config.update("telemetry.includeReasoningPayloads", settings.includeReasoningPayloads, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.redactPayloads !== undefined) {
      await config.update("telemetry.redactPayloads", settings.redactPayloads, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.debugModelLogPath !== undefined) {
      await config.update("telemetry.debugModelLogPath", settings.debugModelLogPath, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.ollamaStreamResponses !== undefined) {
      await config.update("ollamaStreamResponses", settings.ollamaStreamResponses, vscode.ConfigurationTarget.Workspace);
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
    this.cancelActiveCommands();
    this.onDidChangeRequestsEmitter.dispose();
    this.onDidChangeStatusEmitter.dispose();
  }

  cancelActiveCommands(): void {
    for (const controller of this.activeCommandControllers.values()) {
      controller.abort();
    }
  }

  cancelCommand(id: number): void {
    this.activeCommandControllers.get(id)?.abort();
  }

  getCommandRequests(): CommandRequestSnapshot[] {
    return this.requestSnapshots();
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
    const result = await this.spawnCommand({
      command: executable,
      args,
      cwd: this.workspaceFolder.uri.fsPath,
      env: await this.buildEnvironment(args),
      label: args.slice(0, 2).join(" "),
      missingExecutableMessage: `ReleasePilot CLI was not found. Tried: ${resolution.searched.join(", ")}. Install the backend package or set releasepilot.cliPath to the full path of releasepilot.exe.`,
    });
    if (result.stderr) {
      this.output.appendLine(result.stderr);
    }
    return result;
  }

  private async runGitCommand(args: string[]): Promise<string> {
    const result = await this.spawnCommand({
      command: "git",
      args,
      cwd: this.workspaceFolder.uri.fsPath,
      env: process.env,
      label: `git ${args[0] ?? ""}`.trim(),
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
    this.lastResolution = resolveReleasePilotExecutable({
      configuredPath,
      workspacePath: this.workspaceFolder.uri.fsPath,
      extensionPath: this.extensionPath,
      existsSync: fs.existsSync,
    });
    return this.lastResolution;
  }

  private emitStatus(status: ClientStatus): void {
    this.lastStatus = status;
    this.onDidChangeStatusEmitter.fire(status);
  }

  private trackCommandController(requestId: number): AbortController {
    const controller = new AbortController();
    this.activeCommandControllers.set(requestId, controller);
    return controller;
  }

  private async spawnCommand(options: {
    command: string;
    args: string[];
    cwd: string;
    env: NodeJS.ProcessEnv;
    label: string;
    missingExecutableMessage?: string;
  }): Promise<{ stdout: string; stderr: string }> {
    const request = this.startRequest(options.command, options.args, options.label);
    const abortController = this.trackCommandController(request.id);
    return new Promise((resolve, reject) => {
      let settled = false;
      let stdout = "";
      let stderr = "";
      const child = spawn(options.command, options.args, {
        cwd: options.cwd,
        env: options.env,
        windowsHide: true,
        signal: abortController.signal,
      });

      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => {
        stdout += chunk;
        this.appendRequestOutput(request.id, "stdout", chunk);
      });
      child.stderr.on("data", (chunk: string) => {
        stderr += chunk;
        this.output.appendLine(chunk);
        this.appendRequestOutput(request.id, "stderr", chunk);
      });
      child.on("error", (error: NodeJS.ErrnoException) => {
        if (settled) {
          return;
        }
        settled = true;
        this.activeCommandControllers.delete(request.id);
        this.clearActivityRemainders(request.id);
        if (error.name === "AbortError" || error.code === "ABORT_ERR") {
          this.finishRequest(request.id, "cancelled", { error: "ReleasePilot command cancelled." });
          reject(new Error("ReleasePilot command cancelled."));
          return;
        }
        const message = error.code === "ENOENT" && options.missingExecutableMessage ? options.missingExecutableMessage : error.message;
        this.finishRequest(request.id, "failed", { error: message });
        reject(new Error(message));
      });
      child.on("close", (code) => {
        if (settled) {
          return;
        }
        settled = true;
        this.activeCommandControllers.delete(request.id);
        this.clearActivityRemainders(request.id);
        if (abortController.signal.aborted) {
          this.finishRequest(request.id, "cancelled", { exitCode: code, error: "ReleasePilot command cancelled." });
          reject(new Error("ReleasePilot command cancelled."));
          return;
        }
        if (code && code !== 0) {
          const message = stderr.trim() || `${options.command} exited with code ${code}`;
          this.finishRequest(request.id, "failed", { exitCode: code, error: message });
          reject(new Error(message));
          return;
        }
        this.finishRequest(request.id, "completed", { exitCode: code });
        resolve({ stdout, stderr });
      });
    });
  }

  private startRequest(command: string, args: string[], label: string): CommandRequestSnapshot {
    const request: CommandRequestSnapshot = {
      id: this.nextCommandId++,
      command,
      args,
      label,
      status: "running",
      startedAt: new Date().toISOString(),
      stdout: "",
      stderr: "",
      activity: [],
    };
    this.commandRequests.set(request.id, request);
    this.emitRequests();
    return request;
  }

  private appendRequestOutput(id: number, stream: "stdout" | "stderr", chunk: string): void {
    const request = this.commandRequests.get(id);
    if (!request) {
      return;
    }
    const next = {
      ...request,
      [stream]: trimRequestOutput(request[stream] + chunk),
      activity: trimActivity(mergeStreamActivity([...(request.activity ?? []), ...this.buildActivityEntries(id, stream, chunk)])),
    };
    this.commandRequests.set(id, next);
    this.emitRequests();
  }

  private finishRequest(
    id: number,
    status: CommandRequestStatus,
    details: { exitCode?: number | null; error?: string },
  ): void {
    const request = this.commandRequests.get(id);
    if (!request) {
      return;
    }
    this.commandRequests.set(id, {
      ...request,
      status,
      endedAt: new Date().toISOString(),
      exitCode: details.exitCode,
      error: details.error,
    });
    this.emitRequests();
  }

  private emitRequests(): void {
    this.onDidChangeRequestsEmitter.fire(this.requestSnapshots());
  }

  private requestSnapshots(): CommandRequestSnapshot[] {
    return [...this.commandRequests.values()].sort((a, b) => b.id - a.id).slice(0, 25);
  }

  private async buildEnvironment(args: string[]): Promise<NodeJS.ProcessEnv> {
    const settings = this.getModelSettings();
    const provider = resolveCommandModelProvider(args, settings.provider);
    const env = { ...process.env };
    env.RELEASEPILOT_OLLAMA_MODEL = settings.model;
    env.RELEASEPILOT_OLLAMA_ENDPOINT = settings.endpoint;
    env.RELEASEPILOT_OLLAMA_TIMEOUT_SECONDS = String(settings.ollamaTimeoutSeconds);
    env.RELEASEPILOT_MODEL_MAX_ITEMS = String(settings.maxModelItems);
    env.RELEASEPILOT_SUGGESTION_COUNT = String(settings.suggestionCount);
    env.RELEASEPILOT_SUGGESTION_TONE = settings.suggestionTone;
    env.RELEASEPILOT_STYLE_GUIDANCE = settings.styleGuidance;
    env.RELEASEPILOT_PROJECT_VOCABULARY = settings.projectVocabulary.join(",");
    env.RELEASEPILOT_BANNED_PHRASES = settings.bannedPhrases.join(",");
    env.RELEASEPILOT_SPECIFICITY_FLOOR = String(settings.specificityFloor);
    env.RELEASEPILOT_FEEDBACK_INCLUDE_TEXT = settings.feedbackIncludeText ? "1" : "0";
    env.RELEASEPILOT_TRACE_MODEL_CALLS = "1";
    env.RELEASEPILOT_OLLAMA_STREAM = settings.ollamaStreamResponses ? "1" : "0";
    if (settings.includePromptPayloads) {
      env.RELEASEPILOT_TRACE_MODEL_PAYLOADS = "1";
    }
    if (settings.includeReasoningPayloads) {
      env.RELEASEPILOT_TRACE_MODEL_REASONING = "1";
    }
    env.RELEASEPILOT_REDACT_MODEL_PAYLOADS = settings.redactPayloads ? "1" : "0";
    if (settings.debugModelLogPath.trim()) {
      env.RELEASEPILOT_MODEL_LOG_PATH = settings.debugModelLogPath.trim();
    }
    if (provider === "configured") {
      return this.withGithubAuthToken(env, args, provider);
    }
    env.RELEASEPILOT_MODEL_PROVIDER = provider;
    return this.withGithubAuthToken(env, args, provider);
  }

  private buildActivityEntries(requestId: number, stream: "stdout" | "stderr", chunk: string) {
    const key = `${requestId}:${stream}`;
    const combined = `${this.activityRemainders.get(key) ?? ""}${chunk}`;
    const hasTrailingNewline = /\r?\n$/.test(combined);
    const lines = combined.split(/\r?\n/);
    const completeLines = hasTrailingNewline ? lines : lines.slice(0, -1);
    this.activityRemainders.set(key, hasTrailingNewline ? "" : (lines[lines.length - 1] ?? ""));
    return completeLines
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line) => stream === "stderr" || line.startsWith("[releasepilot:"))
      .map((line) => ({
        id: this.nextActivityId++,
        stream,
        category: line.startsWith("[releasepilot:model]") ? "model" as const : "process" as const,
        ...parseModelActivityLine(line),
        receivedAt: new Date().toISOString(),
      }));
  }

  private clearActivityRemainders(requestId: number): void {
    this.activityRemainders.delete(`${requestId}:stdout`);
    this.activityRemainders.delete(`${requestId}:stderr`);
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

function trimRequestOutput(value: string): string {
  return value.length > 6000 ? value.slice(value.length - 6000) : value;
}

function trimActivity<T>(entries: T[]): T[] {
  return entries.length > 80 ? entries.slice(entries.length - 80) : entries;
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
  const payloads = settings.includePromptPayloads ? "; payloads on" : "";
  const reasoning = settings.includeReasoningPayloads ? "; reasoning on" : "";
  const redaction = settings.redactPayloads ? "; redaction on" : "; redaction off";
  const debugLog = settings.debugModelLogPath ? "; debug log on" : "";
  const streaming = settings.ollamaStreamResponses ? "; streaming on" : "";
  const style = settings.styleGuidance || settings.projectVocabulary.length || settings.bannedPhrases.length || settings.specificityFloor
    ? "; style controls on"
    : "";
  const timeout = settings.provider === "ollama" ? `; timeout: ${settings.ollamaTimeoutSeconds}s` : "";
  return `${settings.provider}${model}; fanout guard: ${settings.maxModelItems}; suggestions: ${settings.suggestionCount}; tone: ${settings.suggestionTone}${style}${timeout}${payloads}${reasoning}${redaction}${debugLog}${streaming}`;
}

function parseModelActivityLine(line: string): {
  message: string;
  event?: string;
  attributes?: Record<string, unknown>;
  payload?: string;
} {
  const jsonPrefix = "[releasepilot:model] json ";
  if (!line.startsWith(jsonPrefix)) {
    const streamChunk = parseStreamChunkLine(line);
    if (streamChunk) {
      return streamChunk;
    }
    return { message: line };
  }
  try {
    const parsed = JSON.parse(line.slice(jsonPrefix.length)) as {
      event?: unknown;
      message?: unknown;
      attributes?: unknown;
      payload?: unknown;
    };
    return {
      message: typeof parsed.message === "string" ? parsed.message : line,
      event: typeof parsed.event === "string" ? parsed.event : undefined,
      attributes: isRecord(parsed.attributes) ? parsed.attributes : undefined,
      payload: typeof parsed.payload === "string" ? parsed.payload : undefined,
    };
  } catch {
    return { message: line };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseStreamChunkLine(line: string): {
  message: string;
  event: "stream_chunk";
  attributes: Record<string, unknown>;
  payload: string;
} | undefined {
  const match = line.match(/^\[releasepilot:model\]\s+stream-chunk\s+provider=(\S+)\s+task=(\S+)\s+kind=(\S+)\s+chars=(\d+)(?:\s+redacted=(\S+))?\s+text=(.*)$/);
  if (!match) {
    return undefined;
  }
  const [, provider, task, kind, chars, redacted, text] = match;
  return {
    message: line,
    event: "stream_chunk",
    attributes: {
      provider,
      task,
      kind,
      chars: Number(chars),
      chunks: 1,
      redacted: redacted === "true",
    },
    payload: text,
  };
}

function mergeStreamActivity(entries: CommandRequestSnapshot["activity"]): NonNullable<CommandRequestSnapshot["activity"]> {
  const merged: NonNullable<CommandRequestSnapshot["activity"]> = [];
  for (const entry of entries ?? []) {
    if (entry.event !== "stream_chunk") {
      merged.push(entry);
      continue;
    }
    const kind = entry.attributes?.kind;
    const task = entry.attributes?.task;
    const prior = [...merged].reverse().find(
      (candidate) =>
        candidate.event === "stream_chunk" &&
        candidate.attributes?.kind === kind &&
        candidate.attributes?.task === task,
    );
    if (!prior) {
      merged.push(entry);
      continue;
    }
    const priorChars = typeof prior.attributes?.chars === "number" ? prior.attributes.chars : 0;
    const nextChars = typeof entry.attributes?.chars === "number" ? entry.attributes.chars : 0;
    const priorChunks = typeof prior.attributes?.chunks === "number" ? prior.attributes.chunks : 1;
    const nextChunks = typeof entry.attributes?.chunks === "number" ? entry.attributes.chunks : 1;
    prior.attributes = {
      ...(prior.attributes ?? {}),
      chars: priorChars + nextChars,
      chunks: priorChunks + nextChunks,
      redacted: Boolean(prior.attributes?.redacted) || Boolean(entry.attributes?.redacted),
    };
    prior.payload = trimStreamPayload([prior.payload, entry.payload].filter(Boolean).join(" "));
    prior.message = `[releasepilot:model] stream ${String(kind)} chunks=${priorChunks + nextChunks} chars=${priorChars + nextChars}`;
    prior.receivedAt = entry.receivedAt;
  }
  return merged;
}

function trimStreamPayload(value: string): string {
  return value.length > 6000 ? value.slice(value.length - 6000) : value;
}
