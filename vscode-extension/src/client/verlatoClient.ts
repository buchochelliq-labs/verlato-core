import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import * as fs from "node:fs";

import { ExecutableResolution, resolveVerlatoExecutable } from "./cliResolution";
import { resolveCommandModelProvider, shouldAttachGithubToken } from "./modelEnvironment";
import {
  ApplyResult,
  ClearSuggestionCacheResult,
  ClearWorkingStateResult,
  ClientStatus,
  CommandLogEntry,
  CommandRequestSnapshot,
  CommandRequestStatus,
  DraftDocument,
  DraftItem,
  DraftPromptPreview,
  DraftRangeRequest,
  DraftSuggestionItem,
  DraftSuggestionPreview,
  ModelSettings,
  OllamaDoctorResult,
  RepositoryCommit,
  RepositoryOverview,
  RepositoryPullRequest,
  RepositoryRef,
  ReviewUpdateRequest,
} from "./types";

type CommandActivityHandler = (entry: CommandLogEntry) => void;
type SuggestionPartialHandler = (preview: DraftSuggestionPreview) => void;
export type SuggestionCacheMode = "default" | "no-cache" | "refresh-cache";

export class VerlatoClient implements vscode.Disposable {
  private readonly onDidChangeStatusEmitter = new vscode.EventEmitter<ClientStatus>();
  readonly onDidChangeStatus = this.onDidChangeStatusEmitter.event;
  private readonly onDidChangeRequestsEmitter = new vscode.EventEmitter<CommandRequestSnapshot[]>();
  readonly onDidChangeRequests = this.onDidChangeRequestsEmitter.event;
  private lastStatus: ClientStatus = { mode: "cli", label: "Verlato: CLI", detail: "Using direct CLI commands" };
  private lastResolution: ExecutableResolution | undefined;
  private readonly activeCommandControllers = new Map<number, AbortController>();
  private readonly activeCommandChildren = new Map<number, ChildProcessWithoutNullStreams>();
  private readonly commandRequests = new Map<number, CommandRequestSnapshot>();
  private readonly activityRemainders = new Map<string, string>();
  private pendingRequestEmit: NodeJS.Timeout | undefined;
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

  async previewDraftSuggestions(
    request: DraftRangeRequest,
    itemId?: string,
    onPartial?: SuggestionPartialHandler,
    cacheMode: SuggestionCacheMode = "default",
  ): Promise<DraftSuggestionPreview> {
    const settings = this.getModelSettings();
    const partialState = createSuggestionPartialState(request, settings);
    const cacheArgs = cacheMode === "refresh-cache"
      ? ["--refresh-cache"]
      : cacheMode === "no-cache"
        ? ["--no-cache"]
        : [];
    const onActivity = onPartial
      ? (entry: CommandLogEntry) => {
        const preview = mergeSuggestionActivity(partialState, entry);
        if (preview) {
          onPartial(preview);
        }
      }
      : undefined;
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
      "--parallelism",
      String(settings.suggestionParallelism),
      "--batch-size",
      String(settings.suggestionBatchSize),
      "--tone",
      settings.suggestionTone,
      ...cacheArgs,
      ...(itemId ? ["--id", itemId] : ["--all-visible"]),
    ], { onActivity });
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

  async clearWorkingState(): Promise<ClearWorkingStateResult> {
    return this.runJsonCommand<ClearWorkingStateResult>(["state", "clear-working"]);
  }

  async clearSuggestionCache(): Promise<ClearSuggestionCacheResult> {
    return this.runJsonCommand<ClearSuggestionCacheResult>(["state", "clear-suggestion-cache"]);
  }

  async diagnoseOllama(): Promise<OllamaDoctorResult> {
    const settings = this.getModelSettings();
    return this.runJsonCommand<OllamaDoctorResult>([
      "doctor",
      "ollama",
      ...(settings.suggestionParallelism > 1 ? ["--parallel-probe"] : []),
    ]);
  }

  async warmupOllama(): Promise<OllamaDoctorResult> {
    return this.runJsonCommand<OllamaDoctorResult>(["doctor", "ollama", "--warmup"]);
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
    const prByCommit = mapPrByCommit(state.items);
    const commits = await this.expandMergedPullRequestCommits(parseCommits(commitsRaw, releaseByCommit, prByCommit), releaseByCommit, prByCommit);
    const pullRequests = await this.fetchPullRequests(commits);
    return {
      branches: parseRefs(branchesRaw),
      tags: parseRefs(tagsRaw),
      commits,
      pullRequests,
      releases,
      unpublishedCount: state.items.filter((item) => !item.published).length,
    };
  }

  getModelSettings(): ModelSettings {
    const config = vscode.workspace.getConfiguration("verlato");
    const provider = config.get<string>("modelProvider") || "ollama";
    return {
      provider,
      model: config.get<string>("ollamaModel") || "gemma4:latest",
      endpoint: config.get<string>("ollamaEndpoint") || "http://127.0.0.1:11434/api/chat",
      ollamaTimeoutSeconds: config.get<number>("ollamaTimeoutSeconds") ?? 120,
      maxModelItems: config.get<number>("maxModelItems") ?? 25,
      suggestionCount: config.get<number>("suggestionCount") ?? 1,
      suggestionParallelism: config.get<number>("suggestionParallelism") ?? 2,
      suggestionBatchSize: config.get<number>("suggestionBatchSize") ?? 2,
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
    const config = vscode.workspace.getConfiguration("verlato");
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
    if (settings.suggestionParallelism !== undefined) {
      await config.update("suggestionParallelism", settings.suggestionParallelism, vscode.ConfigurationTarget.Workspace);
    }
    if (settings.suggestionBatchSize !== undefined) {
      await config.update("suggestionBatchSize", settings.suggestionBatchSize, vscode.ConfigurationTarget.Workspace);
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
    this.output.appendLine(`Verlato CLI: ${resolution.executable}`);
    this.output.appendLine(`Model: ${formatModelSettings(modelSettings)}`);
    this.emitStatus({
      mode: "cli",
      label: `Verlato: ${modelSettings.provider}`,
      detail: `Workspace: ${this.workspaceFolder.uri.fsPath}\nCLI: ${resolution.executable}\nModel: ${formatModelSettings(modelSettings)}`,
    });
  }

  dispose(): void {
    this.cancelActiveCommands();
    if (this.pendingRequestEmit) {
      clearTimeout(this.pendingRequestEmit);
      this.pendingRequestEmit = undefined;
    }
    this.onDidChangeRequestsEmitter.dispose();
    this.onDidChangeStatusEmitter.dispose();
  }

  cancelActiveCommands(): void {
    for (const requestId of this.activeCommandControllers.keys()) {
      this.cancelCommand(requestId);
    }
  }

  cancelCommand(id: number): void {
    this.killCommandProcess(id);
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

  private async runJsonCommand<T>(
    args: string[],
    options: { onActivity?: CommandActivityHandler } = {},
  ): Promise<T> {
    const { stdout } = await this.runCommand(args, options);
    return JSON.parse(stdout) as T;
  }

  private async runCommand(
    args: string[],
    options: { onActivity?: CommandActivityHandler } = {},
  ): Promise<{ stdout: string; stderr: string }> {
    const resolution = this.resolveExecutable();
    const executable = resolution.executable;
    this.output.appendLine(`$ ${executable} ${args.join(" ")}`);
    const result = await this.spawnCommand({
      command: executable,
      args,
      cwd: this.workspaceFolder.uri.fsPath,
      env: await this.buildEnvironment(args),
      label: args.slice(0, 2).join(" "),
      missingExecutableMessage: `Verlato CLI was not found. Tried: ${resolution.searched.join(", ")}. Install the backend package or set verlato.cliPath to the full path of verlato.exe.`,
      onActivity: options.onActivity,
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

  private async expandMergedPullRequestCommits(
    commits: RepositoryCommit[],
    releaseByCommit: Map<string, string>,
    prByCommit: Map<string, number>,
  ): Promise<RepositoryCommit[]> {
    const expanded: RepositoryCommit[] = [];
    const seen = new Set<string>();
    for (const commit of commits) {
      if (!seen.has(commit.sha)) {
        expanded.push(commit);
        seen.add(commit.sha);
      }
      if (commit.prNumber === null) {
        continue;
      }
      const parentsRaw = await this.runOptionalProcess("git", ["show", "-s", "--format=%P", commit.sha]);
      const parents = (parentsRaw ?? "").trim().split(/\s+/).filter(Boolean);
      if (parents.length < 2) {
        continue;
      }
      const mergedRaw = await this.runOptionalProcess("git", [
        "log",
        "--date=short",
        "--pretty=format:%h%x09%s%x09%an%x09%ad",
        `${parents[0]}..${parents[1]}`,
      ]);
      if (!mergedRaw) {
        continue;
      }
      const mergedPrByCommit = new Map(prByCommit);
      for (const sha of mergedRaw.split(/\r?\n/).map((line) => line.split("\t", 1)[0]).filter(Boolean)) {
        mergedPrByCommit.set(sha, commit.prNumber);
        mergedPrByCommit.set(sha.slice(0, 7), commit.prNumber);
      }
      for (const mergedCommit of parseCommits(mergedRaw, releaseByCommit, mergedPrByCommit)) {
        if (seen.has(mergedCommit.sha)) {
          const existing = expanded.find((candidate) => candidate.sha === mergedCommit.sha);
          if (existing && existing.prNumber === null) {
            existing.prNumber = commit.prNumber;
          }
          continue;
        }
        expanded.push({ ...mergedCommit, prNumber: commit.prNumber });
        seen.add(mergedCommit.sha);
      }
    }
    return expanded;
  }

  private async fetchPullRequests(commits: RepositoryCommit[]): Promise<RepositoryPullRequest[]> {
    const numbers = Array.from(new Set(commits.map((commit) => commit.prNumber).filter((number): number is number => number !== null)));
    if (!numbers.length) {
      return [];
    }
    const results = await Promise.all(numbers.map(async (number) => {
      const stdout = await this.runOptionalProcess("gh", [
        "pr",
        "view",
        String(number),
        "--json",
        "number,title,state,mergedAt",
      ]);
      if (!stdout) {
        return undefined;
      }
      try {
        return normalizePullRequest(JSON.parse(stdout));
      } catch (error) {
        this.output.appendLine(`Unable to parse GitHub PR #${number} metadata: ${String(error)}`);
        return undefined;
      }
    }));
    return results.filter((pr): pr is RepositoryPullRequest => Boolean(pr));
  }

  private async runOptionalProcess(command: string, args: string[]): Promise<string | undefined> {
    try {
      const result = await this.spawnCommand({
        command,
        args,
        cwd: this.workspaceFolder.uri.fsPath,
        env: process.env,
        label: `${command} ${args[0] ?? ""}`.trim(),
        optionalFailure: true,
      });
      return result.stdout;
    } catch (error) {
      if (error instanceof Error && error.message === "Verlato command cancelled.") {
        throw error;
      }
      return undefined;
    }
  }

  private async readState(): Promise<StatePayload> {
    try {
      const state = await this.runJsonCommand<StatePayload>(["state", "show"]);
      return { items: state.items ?? [] };
    } catch (error) {
      this.output.appendLine(`Verlato state unavailable: ${String(error)}`);
      return { items: [] };
    }
  }

  private resolveExecutable(): ExecutableResolution {
    const configuredPath = vscode.workspace.getConfiguration("verlato").get<string>("cliPath") || "verlato";
    this.lastResolution = resolveVerlatoExecutable({
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
    optionalFailure?: boolean;
    onActivity?: CommandActivityHandler;
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
      this.activeCommandChildren.set(request.id, child);

      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => {
        stdout += chunk;
        this.appendRequestOutput(request.id, "stdout", chunk, options.onActivity);
      });
      child.stderr.on("data", (chunk: string) => {
        stderr += chunk;
        this.output.appendLine(chunk);
        this.appendRequestOutput(request.id, "stderr", chunk, options.onActivity);
      });
      child.on("error", (error: NodeJS.ErrnoException) => {
        if (settled) {
          return;
        }
        settled = true;
        this.activeCommandControllers.delete(request.id);
        this.activeCommandChildren.delete(request.id);
        this.clearActivityRemainders(request.id);
        if (error.name === "AbortError" || error.code === "ABORT_ERR") {
          this.finishRequest(request.id, "cancelled", { error: "Verlato command cancelled." });
          reject(new Error("Verlato command cancelled."));
          return;
        }
        const message = error.code === "ENOENT" && options.missingExecutableMessage ? options.missingExecutableMessage : error.message;
        if (options.optionalFailure) {
          this.finishRequest(request.id, "completed", { error: message });
          reject(new Error(message));
          return;
        }
        this.finishRequest(request.id, "failed", { error: message });
        reject(new Error(message));
      });
      child.on("close", (code) => {
        if (settled) {
          return;
        }
        settled = true;
        this.activeCommandControllers.delete(request.id);
        this.activeCommandChildren.delete(request.id);
        this.clearActivityRemainders(request.id);
        if (abortController.signal.aborted) {
          this.finishRequest(request.id, "cancelled", { exitCode: code, error: "Verlato command cancelled." });
          reject(new Error("Verlato command cancelled."));
          return;
        }
        if (code && code !== 0) {
          const message = stderr.trim() || `${options.command} exited with code ${code}`;
          if (options.optionalFailure) {
            this.finishRequest(request.id, "completed", { exitCode: code, error: message });
            reject(new Error(message));
            return;
          }
          this.finishRequest(request.id, "failed", { exitCode: code, error: message });
          reject(new Error(message));
          return;
        }
        this.finishRequest(request.id, "completed", { exitCode: code });
        resolve({ stdout, stderr });
      });
    });
  }

  private killCommandProcess(requestId: number): void {
    const child = this.activeCommandChildren.get(requestId);
    if (!child || child.killed) {
      return;
    }
    const pid = child.pid;
    if (pid && process.platform === "win32") {
      const killer = spawn("taskkill", ["/pid", String(pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
      killer.on("error", (error) => {
        this.output.appendLine(`Unable to stop Verlato process tree ${pid}: ${String(error)}`);
      });
      return;
    }
    child.kill("SIGTERM");
    setTimeout(() => {
      if (!child.killed) {
        child.kill("SIGKILL");
      }
    }, 1500).unref();
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

  private appendRequestOutput(
    id: number,
    stream: "stdout" | "stderr",
    chunk: string,
    onActivity?: CommandActivityHandler,
  ): void {
    const request = this.commandRequests.get(id);
    if (!request) {
      return;
    }
    const activityEntries = this.buildActivityEntries(id, stream, chunk);
    if (onActivity) {
      for (const entry of activityEntries) {
        try {
          onActivity(entry);
        } catch (error) {
          this.output.appendLine(`Unable to process Verlato activity event: ${String(error)}`);
        }
      }
    }
    const next = {
      ...request,
      [stream]: trimRequestOutput(request[stream] + chunk),
      activity: trimActivity(mergeStreamActivity([...(request.activity ?? []), ...activityEntries])),
    };
    this.commandRequests.set(id, next);
    this.scheduleRequestEmit();
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
    this.emitRequestsNow();
  }

  private emitRequests(): void {
    this.onDidChangeRequestsEmitter.fire(this.requestSnapshots());
  }

  private scheduleRequestEmit(): void {
    if (this.pendingRequestEmit) {
      return;
    }
    this.pendingRequestEmit = setTimeout(() => {
      this.pendingRequestEmit = undefined;
      this.emitRequests();
    }, 150);
  }

  private emitRequestsNow(): void {
    if (this.pendingRequestEmit) {
      clearTimeout(this.pendingRequestEmit);
      this.pendingRequestEmit = undefined;
    }
    this.emitRequests();
  }

  private requestSnapshots(): CommandRequestSnapshot[] {
    return [...this.commandRequests.values()].sort((a, b) => b.id - a.id).slice(0, 25);
  }

  private async buildEnvironment(args: string[]): Promise<NodeJS.ProcessEnv> {
    const settings = this.getModelSettings();
    const provider = resolveCommandModelProvider(args, settings.provider);
    const env = { ...process.env };
    env.VERLATO_OLLAMA_MODEL = settings.model;
    env.VERLATO_OLLAMA_ENDPOINT = settings.endpoint;
    env.VERLATO_OLLAMA_TIMEOUT_SECONDS = String(settings.ollamaTimeoutSeconds);
    env.VERLATO_MODEL_MAX_ITEMS = String(settings.maxModelItems);
    env.VERLATO_SUGGESTION_COUNT = String(settings.suggestionCount);
    env.VERLATO_SUGGESTION_PARALLELISM = String(settings.suggestionParallelism);
    env.VERLATO_SUGGESTION_BATCH_SIZE = String(settings.suggestionBatchSize);
    env.VERLATO_SUGGESTION_TONE = settings.suggestionTone;
    env.VERLATO_STYLE_GUIDANCE = settings.styleGuidance;
    env.VERLATO_PROJECT_VOCABULARY = settings.projectVocabulary.join(",");
    env.VERLATO_BANNED_PHRASES = settings.bannedPhrases.join(",");
    env.VERLATO_SPECIFICITY_FLOOR = String(settings.specificityFloor);
    env.VERLATO_FEEDBACK_INCLUDE_TEXT = settings.feedbackIncludeText ? "1" : "0";
    env.VERLATO_TRACE_MODEL_CALLS = "1";
    env.VERLATO_TRACE_PROGRESS = "1";
    env.VERLATO_OLLAMA_STREAM = settings.ollamaStreamResponses ? "1" : "0";
    if (settings.includePromptPayloads) {
      env.VERLATO_TRACE_MODEL_PAYLOADS = "1";
    }
    if (settings.includeReasoningPayloads) {
      env.VERLATO_TRACE_MODEL_REASONING = "1";
    }
    env.VERLATO_REDACT_MODEL_PAYLOADS = settings.redactPayloads ? "1" : "0";
    if (settings.debugModelLogPath.trim()) {
      env.VERLATO_MODEL_LOG_PATH = settings.debugModelLogPath.trim();
    }
    if (provider === "configured") {
      return this.withGithubAuthToken(env, args, provider);
    }
    env.VERLATO_MODEL_PROVIDER = provider;
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
      .filter((line) => stream === "stderr" || line.startsWith("[verlato:"))
      .map((line) => ({
        id: this.nextActivityId++,
        stream,
        category: activityCategory(line),
        ...parseActivityLine(line),
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
        this.output.appendLine("Attached VS Code GitHub authentication token for Verlato model request.");
      } else {
        this.output.appendLine("VS Code did not return a GitHub authentication token for Verlato model request.");
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
  status?: string;
  edited_summary?: string | null;
  summary_provenance?: {
    source?: string;
  } | null;
  content?: {
    title?: string;
    summary?: string;
  };
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

function parseCommits(raw: string, releaseByCommit: Map<string, string>, prByCommit: Map<string, number>): RepositoryCommit[] {
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      const [sha, subject, author, date] = line.split("\t");
      return {
        sha,
        subject,
        author: `${author || "unknown"}${date ? `, ${date}` : ""}`,
        prNumber: prByCommit.get(sha) ?? extractPrNumber(subject),
        release: releaseByCommit.get(sha) ?? null,
      };
    });
}

function normalizePullRequest(value: unknown): RepositoryPullRequest | undefined {
  if (!value || typeof value !== "object") {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  const number = typeof record.number === "number" ? record.number : Number(record.number);
  if (!Number.isFinite(number)) {
    return undefined;
  }
  const rawState = String(record.state ?? "UNKNOWN").toUpperCase();
  const state = rawState === "OPEN" || rawState === "MERGED" || rawState === "CLOSED" ? rawState : "UNKNOWN";
  return {
    number,
    title: typeof record.title === "string" ? record.title : undefined,
    state,
    mergedAt: typeof record.mergedAt === "string" ? record.mergedAt : null,
  };
}

function extractPrNumber(subject: string): number | null {
  const match = subject.match(/\(#(\d+)\)|pull request #(\d+)|merge pr #(\d+)/i);
  const value = match?.[1] ?? match?.[2] ?? match?.[3];
  return value ? Number(value) : null;
}

function summarizeReleases(items: StateItem[]): { version: string; itemCount: number; items: { id: string; title: string; summary: string; status?: string; source?: string }[] }[] {
  const releases = new Map<string, { id: string; title: string; summary: string; status?: string; source?: string }[]>();
  for (const item of items) {
    if (!item.published || !item.published_in) {
      continue;
    }
    const releaseItems = releases.get(item.published_in) ?? [];
    releaseItems.push({
      id: item.id,
      title: item.content?.title ?? item.id,
      summary: item.edited_summary ?? item.content?.summary ?? "",
      status: item.status,
      source: item.summary_provenance?.source,
    });
    releases.set(item.published_in, releaseItems);
  }
  return [...releases.entries()].map(([version, releaseItems]) => ({
    version,
    itemCount: releaseItems.length,
    items: releaseItems,
  }));
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

function mapPrByCommit(items: StateItem[]): Map<string, number> {
  const byCommit = new Map<string, number>();
  for (const item of items) {
    if (!item.source?.pr_number) {
      continue;
    }
    for (const sha of item.source.commit_shas ?? []) {
      byCommit.set(sha, item.source.pr_number);
      byCommit.set(sha.slice(0, 7), item.source.pr_number);
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
  return `${settings.provider}${model}; fanout guard: ${settings.maxModelItems}; suggestions: ${settings.suggestionCount}; parallelism: ${settings.suggestionParallelism}; batch size: ${settings.suggestionBatchSize}; tone: ${settings.suggestionTone}${style}${timeout}${payloads}${reasoning}${redaction}${debugLog}${streaming}`;
}

interface SuggestionPartialState {
  fallback: DraftSuggestionPreview;
  preview?: DraftSuggestionPreview;
  itemOrders: Map<string, number>;
}

function createSuggestionPartialState(
  request: DraftRangeRequest,
  settings: ModelSettings,
): SuggestionPartialState {
  return {
    fallback: {
      base_ref: request.base,
      head_ref: request.head ?? "HEAD",
      provider: settings.provider,
      count: settings.suggestionCount,
      tone: settings.suggestionTone,
      performance: {
        parallelism: settings.suggestionParallelism,
        batch_size: settings.suggestionBatchSize,
      },
      items: [],
    },
    itemOrders: new Map(),
  };
}

function mergeSuggestionActivity(
  state: SuggestionPartialState,
  entry: CommandLogEntry,
): DraftSuggestionPreview | undefined {
  if (entry.category !== "suggestion") {
    return undefined;
  }
  const attrs = entry.attributes ?? {};
  if (entry.event === "suggestion_preview_started") {
    const preview = coerceSuggestionPreview(attrs.preview, state.fallback);
    if (!preview) {
      return undefined;
    }
    state.preview = preview;
    state.itemOrders.clear();
    preview.items.forEach((item, index) => state.itemOrders.set(item.item_id, index));
    return state.preview;
  }
  if (entry.event !== "suggestion_item_result") {
    return undefined;
  }
  const item = coerceSuggestionItem(attrs.item);
  if (!item) {
    return undefined;
  }
  const order = typeof attrs.order === "number" && Number.isFinite(attrs.order)
    ? attrs.order
    : state.itemOrders.get(item.item_id) ?? state.itemOrders.size;
  state.itemOrders.set(item.item_id, order);
  const preview = state.preview ?? state.fallback;
  const itemsById = new Map(preview.items.map((candidate) => [candidate.item_id, candidate]));
  itemsById.set(item.item_id, item);
  state.preview = {
    ...preview,
    items: [...itemsById.values()].sort((left, right) => {
      const leftOrder = state.itemOrders.get(left.item_id) ?? Number.MAX_SAFE_INTEGER;
      const rightOrder = state.itemOrders.get(right.item_id) ?? Number.MAX_SAFE_INTEGER;
      return leftOrder - rightOrder;
    }),
  };
  return state.preview;
}

function coerceSuggestionPreview(
  value: unknown,
  fallback: DraftSuggestionPreview,
): DraftSuggestionPreview | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const items = Array.isArray(value.items)
    ? value.items.map(coerceSuggestionItem).filter((item): item is DraftSuggestionItem => Boolean(item))
    : [];
  return {
    base_ref: typeof value.base_ref === "string" ? value.base_ref : fallback.base_ref,
    head_ref: typeof value.head_ref === "string" ? value.head_ref : fallback.head_ref,
    provider: typeof value.provider === "string" ? value.provider : fallback.provider,
    count: typeof value.count === "number" ? value.count : fallback.count,
    tone: typeof value.tone === "string" ? value.tone : fallback.tone,
    style_profile: isRecord(value.style_profile)
      ? value.style_profile as DraftSuggestionPreview["style_profile"]
      : fallback.style_profile,
    visible_item_count: typeof value.visible_item_count === "number" ? value.visible_item_count : fallback.visible_item_count,
    fanout_guard: typeof value.fanout_guard === "number" ? value.fanout_guard : fallback.fanout_guard,
    batch_size: typeof value.batch_size === "number" ? value.batch_size : fallback.batch_size,
    batch_count: typeof value.batch_count === "number" ? value.batch_count : fallback.batch_count,
    performance: isRecord(value.performance)
      ? value.performance as DraftSuggestionPreview["performance"]
      : fallback.performance,
    items,
  };
}

function coerceSuggestionItem(value: unknown): DraftSuggestionItem | undefined {
  if (!isRecord(value) || typeof value.item_id !== "string" || !Array.isArray(value.suggestions)) {
    return undefined;
  }
  const suggestions = value.suggestions.filter((suggestion): suggestion is string => typeof suggestion === "string");
  if (!suggestions.length) {
    return undefined;
  }
  return {
    ...(value as unknown as DraftSuggestionItem),
    item_id: value.item_id,
    title: typeof value.title === "string" ? value.title : value.item_id,
    current_summary: typeof value.current_summary === "string" ? value.current_summary : "",
    provider: typeof value.provider === "string" ? value.provider : "configured",
    suggestions,
  };
}

function activityCategory(line: string): "model" | "progress" | "suggestion" | "process" {
  if (line.startsWith("[verlato:model]")) {
    return "model";
  }
  if (line.startsWith("[verlato:progress]")) {
    return "progress";
  }
  if (line.startsWith("[verlato:suggestion]")) {
    return "suggestion";
  }
  return "process";
}

function parseActivityLine(line: string): {
  message: string;
  event?: string;
  attributes?: Record<string, unknown>;
  payload?: string;
} {
  const progress = parseProgressActivityLine(line);
  if (progress) {
    return progress;
  }
  const suggestion = parseSuggestionActivityLine(line);
  if (suggestion) {
    return suggestion;
  }
  const jsonPrefix = "[verlato:model] json ";
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

function parseProgressActivityLine(line: string): {
  message: string;
  event: string;
  attributes: Record<string, unknown>;
} | undefined {
  const jsonPrefix = "[verlato:progress] json ";
  if (!line.startsWith(jsonPrefix)) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(line.slice(jsonPrefix.length)) as Record<string, unknown>;
    const label = typeof parsed.label === "string" ? parsed.label : "Progress update";
    const detail = typeof parsed.detail === "string" ? parsed.detail : "";
    return {
      message: detail ? `${label} - ${detail}` : label,
      event: typeof parsed.event === "string" ? parsed.event : "progress",
      attributes: parsed,
    };
  } catch {
    return {
      message: line,
      event: "progress",
      attributes: {},
    };
  }
}

function parseSuggestionActivityLine(line: string): {
  message: string;
  event: string;
  attributes: Record<string, unknown>;
} | undefined {
  const jsonPrefix = "[verlato:suggestion] json ";
  if (!line.startsWith(jsonPrefix)) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(line.slice(jsonPrefix.length)) as Record<string, unknown>;
    const event = typeof parsed.event === "string" ? parsed.event : "suggestion";
    return {
      message: suggestionActivityMessage(event, parsed, line),
      event,
      attributes: parsed,
    };
  } catch {
    return {
      message: line,
      event: "suggestion",
      attributes: {},
    };
  }
}

function suggestionActivityMessage(event: string, attrs: Record<string, unknown>, fallback: string): string {
  const itemId = typeof attrs.item_id === "string" ? attrs.item_id : undefined;
  if (event === "suggestion_preview_started") {
    const total = typeof attrs.total === "number" ? ` for ${attrs.total} item(s)` : "";
    return `Suggestion preview started${total}`;
  }
  if (event === "suggestion_item_result") {
    return itemId ? `Generated suggestion for ${itemId}` : "Generated suggestion";
  }
  if (event === "suggestion_item_failed") {
    return itemId ? `Suggestion failed for ${itemId}` : "Suggestion failed";
  }
  return fallback;
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
  const match = line.match(/^\[verlato:model\]\s+stream-chunk\s+provider=(\S+)\s+task=(\S+)\s+kind=(\S+)\s+chars=(\d+)(?:\s+redacted=(\S+))?\s+text=(.*)$/);
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
    prior.payload = trimStreamPayload(`${prior.payload ?? ""}${entry.payload ?? ""}`);
    prior.message = `[verlato:model] stream ${String(kind)} chunks=${priorChunks + nextChunks} chars=${priorChars + nextChars}`;
    prior.receivedAt = entry.receivedAt;
  }
  return merged;
}

function trimStreamPayload(value: string): string {
  return value.length > 6000 ? value.slice(value.length - 6000) : value;
}
