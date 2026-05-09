import * as vscode from "vscode";

import { VerlatoClient } from "../client/verlatoClient";
import {
  CommandRequestSnapshot,
  ConventionalCommitDraft,
  DraftDocument,
  DraftRangeRequest,
  DraftSuggestionItem,
  DraftSuggestionPreview,
  McpServerStatus,
  ModelSettings,
  OllamaDoctorResult,
  RepositoryOverview,
} from "../client/types";
import { McpServerController, notRunningStatus } from "../mcpServer";
import { type NotificationAction, handleNotificationAction, showErrorNotification, showSuccessNotification } from "../notifications";
import { buildReleaseControlHtml } from "./releaseControlMarkup";
import { PreviewPanel } from "./previewPanel";

const SUGGESTION_CACHE_WARM_DELAY_MS = 100;

type RefreshHandler = (
  document?: DraftDocument,
  overview?: RepositoryOverview,
  modelSettings?: ModelSettings,
  suggestions?: DraftSuggestionPreview,
) => void;

export class ReleaseControlViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private document: DraftDocument | undefined;
  private overview: RepositoryOverview | undefined;
  private suggestions: DraftSuggestionPreview | undefined;
  private conventionalCommits: Record<string, ConventionalCommitDraft> = {};
  private ollamaDoctor: OllamaDoctorResult | undefined;
  private requests: CommandRequestSnapshot[] = [];
  private modelSettings: ModelSettings;
  private range: DraftRangeRequest = { base: "HEAD~10", head: "HEAD", version: "Unreleased" };
  private busy = false;
  private statusMessage: string | undefined;
  private errorMessage: string | undefined;
  private pendingRender: NodeJS.Timeout | undefined;
  private pendingSuggestionWarm: SuggestionWarmRequest | undefined;
  private suggestionWarmTimer: NodeJS.Timeout | undefined;
  private activeSuggestionWarm: { signature: string; promise: Promise<void> } | undefined;
  private lastSuggestionWarmSignature: string | undefined;
  private mcpServerStatus: McpServerStatus = notRunningStatus();
  private readonly mcpServer: McpServerController | undefined;
  private readonly onDidRefresh: RefreshHandler | undefined;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly client: VerlatoClient,
    private readonly previewPanel: PreviewPanel,
    mcpServerOrOnDidRefresh?: McpServerController | RefreshHandler,
    onDidRefresh?: RefreshHandler,
  ) {
    if (typeof mcpServerOrOnDidRefresh === "function") {
      this.onDidRefresh = mcpServerOrOnDidRefresh;
    } else {
      this.mcpServer = mcpServerOrOnDidRefresh;
      this.onDidRefresh = onDidRefresh;
    }
    this.mcpServerStatus = this.mcpServer?.getStatus() ?? notRunningStatus();
    this.mcpServer?.onDidChangeStatus((status) => {
      this.mcpServerStatus = status;
      this.render();
    });
    this.modelSettings = client.getModelSettings();
    this.requests = client.getCommandRequests();
    this.client.onDidChangeRequests((requests) => {
      const hadRunning = this.requests.some((request) => request.status === "running");
      const hasRunning = requests.some((request) => request.status === "running");
      this.requests = requests;
      if (hadRunning && hasRunning) {
        this.scheduleRender();
        return;
      }
      this.render();
    });
  }

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this.extensionUri, "media", "vendor"),
      ],
    };
    webviewView.webview.onDidReceiveMessage((message) => {
      void this.handleMessage(message as ViewMessage);
    });
    this.render();
    void this.refreshRepository(false);
  }

  refresh(document?: DraftDocument, overview?: RepositoryOverview, modelSettings?: ModelSettings): void {
    if (document) {
      this.document = document;
      this.range = { base: document.base_ref, head: document.head_ref, version: document.version };
    }
    if (overview) {
      this.overview = overview;
    }
    if (modelSettings) {
      this.modelSettings = modelSettings;
    }
    this.render();
    this.onDidRefresh?.(document, overview, modelSettings, this.suggestions);
    if (document || modelSettings) {
      this.scheduleSuggestionCacheWarm();
    }
  }

  getDocument(): DraftDocument | undefined {
    return this.document;
  }

  useRef(ref: string, slot: "base" | "head"): void {
    this.range = { ...this.range, [slot]: ref };
    this.render();
  }

  private async handleMessage(message: ViewMessage): Promise<void> {
    if (message.type === "setRange") {
      this.range = {
        base: message.base ?? "",
        head: message.head || "HEAD",
        version: message.version || "Unreleased",
      };
      this.cancelPendingSuggestionWarm();
      return;
    }
    if (message.type === "cancel") {
      this.cancelCurrentOperation();
      return;
    }
    if (message.type === "cancelRequest" && message.requestId !== undefined) {
      this.cancelRequest(message.requestId);
      return;
    }
    if (message.type === "useRef" && message.ref && message.slot) {
      this.range = { ...this.range, [message.slot]: message.ref };
      this.cancelPendingSuggestionWarm();
      this.render();
      return;
    }
    if (message.type === "scan") {
      await this.scan(false);
    }
    if (message.type === "preview") {
      await this.scan(true);
    }
    if (message.type === "suggest" && message.itemId) {
      await this.suggest(message.itemId);
    }
    if (message.type === "generateConventionalCommit" && message.sha) {
      await this.generateConventionalCommit(message.sha);
    }
    if (message.type === "suggestAll") {
      await this.suggest();
    }
    if (message.type === "setTone" && message.tone) {
      await this.setTone(message.tone);
    }
    if (message.type === "regenerateSuggestion" && message.itemId) {
      await this.regenerateSuggestion(message.itemId);
    }
    if (message.type === "regenerateAll") {
      this.suggestions?.items.forEach((item) => this.recordFeedback(
        "suggestion_regenerated",
        item.item_id,
        { scope: "all-visible", ...this.suggestionFeedbackMetadata(item.item_id) },
      ));
      await this.suggest(undefined, "refresh-cache");
    }
    if (message.type === "feedbackPositive" && message.itemId) {
      this.recordFeedback("suggestion_marked_useful", message.itemId, this.suggestionFeedbackMetadata(message.itemId));
      this.statusMessage = `Recorded useful feedback for ${message.itemId}.`;
      this.render();
    }
    if (message.type === "feedbackNegative" && message.itemId) {
      this.recordFeedback("suggestion_marked_not_useful", message.itemId, this.suggestionFeedbackMetadata(message.itemId));
      this.statusMessage = `Recorded not useful feedback for ${message.itemId}.`;
      this.render();
    }
    if (message.type === "applySuggestion" && message.itemId && message.suggestionIndex !== undefined) {
      await this.applySuggestion(message.itemId, message.suggestionIndex);
    }
    if (message.type === "skipSuggestion" && message.itemId) {
      this.recordFeedback("suggestion_skipped", message.itemId, this.suggestionFeedbackMetadata(message.itemId));
      this.skipSuggestion(message.itemId);
    }
    if (message.type === "refresh") {
      await this.refreshRepository(true);
    }
    if (message.type === "clearWorkingState") {
      await this.clearWorkingState();
    }
    if (message.type === "clearSuggestionCache") {
      await this.clearSuggestionCache();
    }
    if (message.type === "openSources") {
      await vscode.commands.executeCommand("verlato.sources.focus");
    }
    if (message.type === "openReleases") {
      await vscode.commands.executeCommand("verlato.releases.focus");
    }
    if (message.type === "changeModel") {
      await vscode.commands.executeCommand("verlato.changeModel");
    }
    if (message.type === "diagnoseOllama") {
      await this.diagnoseOllama();
    }
    if (message.type === "warmupOllama") {
      await this.warmupOllama();
    }
    if (message.type === "startMcpServer") {
      if (this.mcpServer) {
        const status = await this.mcpServer.start();
        this.mcpServerStatus = status;
        this.statusMessage = status.state === "running"
          ? "Verlato MCP server started from the control view."
          : `Verlato MCP server status: ${status.label}.`;
        this.render();
        return;
      }
      await vscode.commands.executeCommand("verlato.startMcpServer");
    }
    if (message.type === "togglePromptPayloads") {
      await this.togglePromptPayloads();
    }
    if (message.type === "toggleReasoningPayloads") {
      await this.toggleReasoningPayloads();
    }
    if (message.type === "togglePayloadRedaction") {
      await this.togglePayloadRedaction();
    }
    if (message.type === "copyText" && message.text !== undefined) {
      await this.copyText(message.text, message.label);
    }
    if (message.type === "apply" && message.target) {
      await this.apply(message.target);
    }
    if (message.type === "mark" && message.itemId && message.status) {
      await this.markItem(message.itemId, message.status);
    }
    if (message.type === "prompt" && message.itemId) {
      await this.openPrompt(message.itemId);
    }
  }

  private async scan(showPreview: boolean): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    if (!(await this.confirmTrustedPatchMode())) {
      return;
    }
    let successMessage: string | undefined;
    await this.runBusy(async () => {
      const document = await this.client.getDraft(this.range);
      const overview = await this.client.getRepositoryOverview();
      this.statusMessage = `Loaded ${document.item_count} draft item(s) from ${document.base_ref}..${document.head_ref}.`;
      successMessage = `Verlato loaded ${document.item_count} draft item(s).`;
      this.suggestions = undefined;
      this.refresh(document, overview, this.client.getModelSettings());
      if (showPreview) {
        this.previewPanel.show(document);
      }
    });
    if (successMessage) {
      await handleNotificationAction(
        await showSuccessNotification(
          successMessage,
          showPreview ? "View Activity" : "Open Verlato",
          showPreview ? "Open Verlato" : "View Activity",
        ),
      );
    }
  }

  private async confirmTrustedPatchMode(): Promise<boolean> {
    const settings = this.client.getModelSettings();
    this.modelSettings = settings;
    if (settings.modelPrivacy !== "trusted-provider-patch" && !settings.allowRemotePatches) {
      return true;
    }
    const provider = settings.provider || "configured";
    const confirmed = await vscode.window.showWarningMessage(
      `Verlato is configured to allow raw patch payloads for ${provider}. Continue only if this destination is approved for source code.`,
      { modal: true },
      "Send Patch Evidence",
    );
    return confirmed === "Send Patch Evidence";
  }

  private async refreshRepository(showToast: boolean): Promise<void> {
    let successMessage: string | undefined;
    await this.runBusy(async () => {
      const overview = await this.client.getRepositoryOverview();
      if (this.range.base === "HEAD~10" && overview.tags[0]?.name) {
        this.range = { ...this.range, base: overview.tags[0].name };
      }
      this.statusMessage = `Repository activity loaded: ${overview.commits.length} recent commit(s), ${overview.branches.length} branch(es), ${overview.tags.length} tag(s).`;
      this.refresh(undefined, overview, this.client.getModelSettings());
      if (showToast) {
        successMessage = `Verlato found ${overview.branches.length} branch(es), ${overview.tags.length} tag(s), and ${overview.unpublishedCount} unpublished item(s).`;
      }
    });
    if (successMessage) {
      await handleNotificationAction(await showSuccessNotification(successMessage, "Open Verlato", "View Activity"));
    }
  }

  private async apply(target: "changelog" | "github-release"): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    let resultPath: string | undefined;
    await this.runBusy(async () => {
      const result = await this.client.applyDraft(this.range, target);
      resultPath = result.path;
    });
    if (resultPath) {
      const appliedPath = resultPath;
      await handleNotificationAction(
        await showSuccessNotification(`Updated ${appliedPath}`, "Open File", "Preview", "View Activity"),
        {
          "Open File": async () => {
            const folder = vscode.workspace.workspaceFolders?.[0];
            const uri = folder ? vscode.Uri.joinPath(folder.uri, appliedPath) : vscode.Uri.file(appliedPath);
            const document = await vscode.workspace.openTextDocument(uri);
            await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
          },
          Preview: async () => {
            const document = await this.client.getDraft(this.range);
            this.previewPanel.show(document);
          },
        },
      );
    }
  }

  private async clearWorkingState(): Promise<void> {
    const confirmed = await vscode.window.showWarningMessage(
      "Clear unpublished Verlato draft and review state? Published release markers will be preserved.",
      { modal: true },
      "Clear Working State",
    );
    if (confirmed !== "Clear Working State") {
      return;
    }
    let successMessage: string | undefined;
    await this.runBusy(async () => {
      const result = await this.client.clearWorkingState();
      const document = this.hasSelectedBase() ? await this.client.getDraft(this.range) : undefined;
      const overview = await this.client.getRepositoryOverview();
      this.suggestions = undefined;
      this.statusMessage = `Cleared ${result.removed_working_items} working item(s); preserved ${result.preserved_published_items} published item(s).`;
      successMessage = this.statusMessage;
      this.refresh(document, overview, this.client.getModelSettings());
    });
    if (successMessage) {
      await handleNotificationAction(await showSuccessNotification(successMessage, "Open Verlato", "View Activity"));
    }
  }

  private async clearSuggestionCache(): Promise<void> {
    const confirmed = await vscode.window.showWarningMessage(
      "Clear cached Verlato suggestions for this workspace?",
      { modal: true },
      "Clear Suggestion Cache",
    );
    if (confirmed !== "Clear Suggestion Cache") {
      return;
    }
    let successMessage: string | undefined;
    await this.runBusy(async () => {
      const result = await this.client.clearSuggestionCache();
      this.statusMessage = `Cleared ${result.removed_entries} cached suggestion entr${result.removed_entries === 1 ? "y" : "ies"}.`;
      successMessage = this.statusMessage;
      this.suggestions = undefined;
      this.refresh(undefined, undefined, this.client.getModelSettings());
    });
    if (successMessage) {
      await handleNotificationAction(await showSuccessNotification(successMessage, "Open Verlato", "View Activity"));
    }
  }

  private async diagnoseOllama(): Promise<void> {
    let successMessage: string | undefined;
    await this.runBusy(async () => {
      const result = await this.client.diagnoseOllama();
      this.ollamaDoctor = result;
      this.statusMessage = `Ollama health: ${result.summary.status} (${result.summary.bottleneck}).`;
      successMessage = this.statusMessage;
      this.render();
    }, { errorActions: ["View Activity", "Change Model"] });
    if (successMessage) {
      await handleNotificationAction(await showSuccessNotification(successMessage, "Open Verlato", "View Activity"));
    }
  }

  private async warmupOllama(): Promise<void> {
    let successMessage: string | undefined;
    await this.runBusy(async () => {
      const result = await this.client.warmupOllama();
      this.ollamaDoctor = result;
      this.statusMessage = result.summary.warmed_up
        ? `Ollama warmup completed (${result.summary.bottleneck}).`
        : `Ollama warmup finished with ${result.summary.status}.`;
      successMessage = this.statusMessage;
      this.render();
    }, { errorActions: ["View Activity", "Change Model"] });
    if (successMessage) {
      await handleNotificationAction(await showSuccessNotification(successMessage, "Open Verlato", "View Activity"));
    }
  }

  private async suggest(itemId?: string, cacheMode: "default" | "refresh-cache" = "default"): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    let successMessage: string | undefined;
    this.suggestions = undefined;
    this.onDidRefresh?.(this.document, this.overview, this.modelSettings, this.suggestions);
    await this.runBusy(async () => {
      if (!itemId && cacheMode === "default") {
        this.cancelPendingSuggestionWarm();
      }
      const suggestions = await this.client.previewDraftSuggestions(this.range, itemId, (partial) => {
        this.suggestions = partial;
        if (partial.items.length) {
          this.statusMessage = itemId
            ? "Generated a partial suggestion."
            : `Generated suggestions for ${partial.items.length} item(s) so far.`;
        }
        this.render();
        this.onDidRefresh?.(this.document, this.overview, this.modelSettings, this.suggestions);
      }, cacheMode);
      this.suggestions = suggestions;
      this.onDidRefresh?.(this.document, this.overview, this.modelSettings, this.suggestions);
      const generatedCount = suggestions.items[0]?.suggestions.length ?? 0;
      const batchCount = suggestions.batch_count ?? 1;
      this.statusMessage = itemId
        ? generatedCount === 1 ? "Generated one suggested description." : `Generated ${generatedCount} suggestions.`
        : batchCount > 1
          ? `Generated suggestions for ${suggestions.items.length} item(s) in ${batchCount} batch(es).`
          : `Generated suggestions for ${suggestions.items.length} item(s).`;
      successMessage = this.statusMessage;
    }, { errorActions: ["View Activity", "Change Model"] });
    if (successMessage) {
      await handleNotificationAction(await showSuccessNotification(successMessage, "Review Suggestions", "View Activity"));
    }
  }

  private async generateConventionalCommit(sha: string): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    if (!(await this.confirmTrustedPatchMode())) {
      return;
    }
    let draft: ConventionalCommitDraft | undefined;
    await this.runBusy(async () => {
      draft = await this.client.draftConventionalCommit(this.range, sha);
      this.conventionalCommits = {
        ...this.conventionalCommits,
        [draft.sha || sha]: draft,
        [sha]: draft,
      };
      await vscode.env.clipboard.writeText(draft.message);
      this.statusMessage = `Generated and copied conventional commit for ${(draft.sha || sha).slice(0, 7)}.`;
      this.render();
    }, { errorActions: ["View Activity", "Change Model"] });
    if (draft) {
      await handleNotificationAction(
        await showSuccessNotification("Generated conventional commit.", "Open Verlato", "View Activity"),
      );
    }
  }

  private async applySuggestion(itemId: string, suggestionIndex: number): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    const suggestionItem = this.suggestions?.items.find((item) => item.item_id === itemId);
    const summary = suggestionItem?.suggestions[suggestionIndex];
    if (!summary) {
      return;
    }
    const detail = suggestionItem?.suggestion_details?.find((entry) => entry.summary === summary);
    const feedbackMetadata = this.suggestionFeedbackMetadata(itemId, suggestionIndex);
    await this.runBusy(async () => {
      await this.client.reviewDraftItem(this.range, itemId, {
        summary,
        summaryProvenance: {
          source: "model-suggestion",
          provider: this.suggestions?.provider,
          model: suggestionItem?.provider,
          prompt_sha: suggestionItem?.prompt_sha,
          prompt_profile: this.suggestions?.prompt_profile ?? suggestionItem?.prompt_budget?.profile,
          cache_hit: suggestionItem?.cache?.hit,
          tone: suggestionItem?.tone ?? this.suggestions?.tone ?? this.client.getModelSettings().suggestionTone,
          style_profile: suggestionItem?.style_profile ?? this.suggestions?.style_profile,
          suggestion_index: suggestionIndex,
          similarity_to_current: detail?.similarity_to_current,
          specificity_score: detail?.specificity_score,
          response_metadata: suggestionItem?.provider_metadata,
          source_evidence_count: suggestionItem?.source_evidence?.length ?? 0,
          reasoning_exposed: Boolean(suggestionItem?.provider_reasoning),
        },
      });
      this.skipSuggestion(itemId, false);
      const document = await this.client.getDraft(this.range);
      this.statusMessage = `Applied suggestion for ${itemId}.`;
      this.refresh(document, undefined, this.client.getModelSettings());
      this.previewPanel.show(document);
    });
  }

  private skipSuggestion(itemId: string, render = true): void {
    if (!this.suggestions) {
      return;
    }
    this.suggestions = {
      ...this.suggestions,
      items: this.suggestions.items.filter((item) => item.item_id !== itemId),
    };
    if (render) {
      this.render();
    }
    this.onDidRefresh?.(this.document, this.overview, this.modelSettings, this.suggestions);
  }

  private async regenerateSuggestion(itemId: string): Promise<void> {
    this.recordFeedback("suggestion_regenerated", itemId, {
      scope: "item",
      ...this.suggestionFeedbackMetadata(itemId),
    });
    await this.suggest(itemId, "refresh-cache");
  }

  private suggestionFeedbackMetadata(itemId: string, suggestionIndex = 0): Record<string, unknown> {
    const suggestionItem = this.suggestions?.items.find((item) => item.item_id === itemId);
    if (!suggestionItem) {
      return {};
    }
    const summary = suggestionItem.suggestions[suggestionIndex] ?? suggestionItem.suggestions[0];
    const detail = suggestionItem.suggestion_details?.find((entry) => entry.summary === summary)
      ?? suggestionItem.suggestion_details?.[0];
    const metadata: Record<string, unknown> = {
      source: "model-suggestion",
      provider: this.suggestions?.provider,
      model: suggestionItem.provider,
      tone: suggestionItem.tone ?? this.suggestions?.tone ?? this.client.getModelSettings().suggestionTone,
      prompt_profile: this.suggestions?.prompt_profile ?? suggestionItem.prompt_budget?.profile,
      cache_hit: suggestionItem.cache?.hit,
      similarity_to_current: detail?.similarity_to_current,
      specificity_score: detail?.specificity_score,
      source_evidence_count: suggestionItem.source_evidence?.length ?? 0,
    };
    return Object.fromEntries(Object.entries(metadata).filter(([, value]) => value !== undefined));
  }

  private recordFeedback(event: string, itemId: string, metadata: Record<string, unknown> = {}): void {
    this.client.recordFeedback(event, itemId, metadata).catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      vscode.window.showWarningMessage(`Verlato feedback was not recorded: ${message}`);
    });
  }

  private async setTone(tone: string): Promise<void> {
    this.cancelPendingSuggestionWarm();
    await this.client.updateModelSettings({ suggestionTone: tone });
    this.modelSettings = this.client.getModelSettings();
    this.statusMessage = `Suggestion tone set to ${tone}.`;
    this.render();
    this.scheduleSuggestionCacheWarm();
  }

  private async markItem(itemId: string, status: "needs-review" | "ready" | "accepted" | "hidden"): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    await this.runBusy(async () => {
      await this.client.reviewDraftItem(this.range, itemId, { status });
      const document = await this.client.getDraft(this.range);
      this.statusMessage = `Updated ${itemId}.`;
      this.refresh(document, undefined, this.client.getModelSettings());
      this.previewPanel.show(document);
    });
  }

  private async openPrompt(itemId: string): Promise<void> {
    if (!this.hasSelectedBase()) {
      this.statusMessage = "No base refs selected.";
      this.render();
      return;
    }
    await this.runBusy(async () => {
      const promptPreview = await this.client.getModelPrompt(this.range, itemId);
      await vscode.env.clipboard.writeText(promptPreview.prompt);
      const document = await vscode.workspace.openTextDocument({
        content: [
          "# Verlato Model Prompt",
          "",
          `Item: ${promptPreview.title}`,
          `Provider Hint: ${promptPreview.provider}`,
          "",
          `Privacy: ${promptPreview.privacy?.mode ?? "metadata-only"}`,
          `Patch chars sent: ${promptPreview.privacy?.raw_patch_chars_sent ?? 0}`,
          "",
          "## AI Request",
          "",
          "```json",
          JSON.stringify(promptPreview.ai_request ?? {}, null, 2),
          "```",
          "",
          "## Prompt",
          "",
          promptPreview.prompt,
        ].join("\n"),
        language: "markdown",
      });
      await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
    });
  }

  private async togglePromptPayloads(): Promise<void> {
    const next = !this.client.getModelSettings().includePromptPayloads;
    await this.client.updateModelSettings({ includePromptPayloads: next });
    this.modelSettings = this.client.getModelSettings();
    this.statusMessage = next
      ? "Prompt and response payloads will be shown for new model requests."
      : "Prompt payloads are hidden for new model requests.";
    this.render();
  }

  private async toggleReasoningPayloads(): Promise<void> {
    const next = !this.client.getModelSettings().includeReasoningPayloads;
    await this.client.updateModelSettings({ includeReasoningPayloads: next });
    this.modelSettings = this.client.getModelSettings();
    this.statusMessage = next
      ? "Provider reasoning payloads will be available in collapsed details for new model requests."
      : "Provider reasoning payloads are hidden for new model requests.";
    this.render();
  }

  private async togglePayloadRedaction(): Promise<void> {
    const next = !this.client.getModelSettings().redactPayloads;
    await this.client.updateModelSettings({ redactPayloads: next });
    this.modelSettings = this.client.getModelSettings();
    this.statusMessage = next
      ? "Model telemetry payload redaction is enabled for new requests."
      : "Model telemetry payload redaction is disabled for new requests.";
    this.render();
  }

  private async copyText(text: string, label?: string): Promise<void> {
    if (!text) {
      return;
    }
    await vscode.env.clipboard.writeText(text);
    this.statusMessage = `Copied ${label || "payload"} to clipboard.`;
    this.render();
  }

  private async runBusy(
    task: () => Promise<void>,
    options: { errorActions?: NotificationAction[] } = {},
  ): Promise<void> {
    let errorNotificationMessage: string | undefined;
    this.busy = true;
    this.errorMessage = undefined;
    this.render();
    try {
      await task();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message === "Verlato command cancelled.") {
        this.statusMessage = "Stopped current Verlato command.";
      } else {
        this.errorMessage = message;
        errorNotificationMessage = message;
      }
    } finally {
      this.busy = false;
      this.render();
    }
    if (errorNotificationMessage) {
      const actions = options.errorActions ?? ["View Activity"];
      await handleNotificationAction(await showErrorNotification(errorNotificationMessage, ...actions));
    }
  }

  private cancelCurrentOperation(): void {
    if (!this.busy) {
      return;
    }
    this.statusMessage = "Stopping current Verlato command...";
    this.client.cancelActiveCommands();
    this.render();
  }

  private scheduleSuggestionCacheWarm(): void {
    const request = this.buildSuggestionWarmRequest();
    if (!request) {
      this.cancelPendingSuggestionWarm();
      return;
    }
    if (
      request.signature === this.lastSuggestionWarmSignature ||
      request.signature === this.activeSuggestionWarm?.signature ||
      request.signature === this.pendingSuggestionWarm?.signature
    ) {
      return;
    }
    this.pendingSuggestionWarm = request;
    if (this.suggestionWarmTimer) {
      clearTimeout(this.suggestionWarmTimer);
    }
    this.suggestionWarmTimer = setTimeout(() => {
      this.suggestionWarmTimer = undefined;
      void this.startPendingSuggestionWarm();
    }, SUGGESTION_CACHE_WARM_DELAY_MS);
  }

  private cancelPendingSuggestionWarm(): void {
    if (this.suggestionWarmTimer) {
      clearTimeout(this.suggestionWarmTimer);
      this.suggestionWarmTimer = undefined;
    }
    this.pendingSuggestionWarm = undefined;
  }

  private async startPendingSuggestionWarm(): Promise<void> {
    const request = this.pendingSuggestionWarm;
    if (!request) {
      return;
    }
    if (this.activeSuggestionWarm) {
      return;
    }
    this.pendingSuggestionWarm = undefined;
    const promise = this.runSuggestionWarm(request);
    this.activeSuggestionWarm = { signature: request.signature, promise };
    await promise;
  }

  private async runSuggestionWarm(request: SuggestionWarmRequest): Promise<void> {
    this.statusMessage = "Warming suggestion cache...";
    this.render();
    try {
      const result = await this.client.warmDraftSuggestionCache(request.range);
      const current = this.buildSuggestionWarmRequest();
      if (current?.signature === request.signature) {
        this.lastSuggestionWarmSignature = request.signature;
        this.statusMessage = `Suggestion cache warmed for ${result.items.length} item(s).`;
        this.render();
      }
    } catch {
      const current = this.buildSuggestionWarmRequest();
      if (current?.signature === request.signature) {
        this.statusMessage = "Suggestion cache warm did not complete; Suggest will retry normally.";
        this.render();
      }
    } finally {
      if (this.activeSuggestionWarm?.signature === request.signature) {
        this.activeSuggestionWarm = undefined;
      }
      if (this.pendingSuggestionWarm) {
        void this.startPendingSuggestionWarm();
      }
    }
  }

  private async waitForMatchingSuggestionWarm(): Promise<void> {
    const request = this.buildSuggestionWarmRequest();
    const active = this.activeSuggestionWarm;
    if (!request || !active || active.signature !== request.signature) {
      return;
    }
    this.statusMessage = "Finishing suggestion cache warm before rendering suggestions...";
    this.render();
    await active.promise;
  }

  private buildSuggestionWarmRequest(): SuggestionWarmRequest | undefined {
    const document = this.document;
    const settings = this.client.getModelSettings();
    this.modelSettings = settings;
    if (
      !document ||
      document.item_count < 1 ||
      settings.provider !== "ollama" ||
      !settings.suggestionAutoWarmCache ||
      !this.hasSelectedBase()
    ) {
      return undefined;
    }
    const range = {
      base: this.range.base,
      head: this.range.head || "HEAD",
      version: this.range.version || document.version,
    };
    const signature = JSON.stringify({
      base: range.base,
      head: range.head,
      provider: settings.provider,
      model: settings.model,
      suggestionModel: settings.suggestionModel,
      endpoint: settings.endpoint,
      count: settings.suggestionCount,
      parallelism: settings.suggestionParallelism,
      batchSize: settings.suggestionBatchSize,
      strategy: settings.suggestionStrategy,
      promptProfile: settings.suggestionPromptProfile,
      tone: settings.suggestionTone,
      styleGuidance: settings.styleGuidance,
      projectVocabulary: settings.projectVocabulary,
      bannedPhrases: settings.bannedPhrases,
      specificityFloor: settings.specificityFloor,
      itemCount: document.item_count,
    });
    return { signature, range, itemCount: document.item_count };
  }

  private cancelRequest(requestId: number): void {
    this.statusMessage = `Stopping request ${requestId}...`;
    this.client.cancelCommand(requestId);
    this.render();
  }

  private hasSelectedBase(): boolean {
    return this.range.base.trim().length > 0;
  }

  private render(): void {
    if (this.pendingRender) {
      clearTimeout(this.pendingRender);
      this.pendingRender = undefined;
    }
    if (!this.view) {
      return;
    }
    this.view.webview.html = buildReleaseControlHtml({
      document: this.document,
      overview: this.overview,
      modelSettings: this.modelSettings,
      range: this.range,
      busy: this.busy,
      suggestions: this.suggestions,
      conventionalCommits: this.conventionalCommits,
      ollamaDoctor: this.ollamaDoctor,
      mcpServerStatus: this.mcpServerStatus,
      requests: this.requests,
      statusMessage: this.statusMessage,
      errorMessage: this.errorMessage,
    }, {
      webviewElementsScriptUri: this.view.webview.asWebviewUri(
        vscode.Uri.joinPath(this.extensionUri, "media", "vendor", "vscode-elements.bundled.js"),
      ).toString(),
    });
  }

  private scheduleRender(): void {
    if (this.pendingRender) {
      return;
    }
    this.pendingRender = setTimeout(() => {
      this.pendingRender = undefined;
      this.render();
    }, 250);
  }
}

type ViewMessage = {
  type: string;
  base?: string;
  head?: string;
  version?: string;
  target?: "changelog" | "github-release";
  itemId?: string;
  status?: "needs-review" | "ready" | "accepted" | "hidden";
  ref?: string;
  sha?: string;
  slot?: "base" | "head";
  suggestionIndex?: number;
  requestId?: number;
  text?: string;
  label?: string;
  tone?: string;
};

interface SuggestionWarmRequest {
  signature: string;
  range: DraftRangeRequest;
  itemCount: number;
}
