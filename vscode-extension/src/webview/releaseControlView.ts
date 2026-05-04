import * as vscode from "vscode";

import { ReleasePilotClient } from "../client/releasePilotClient";
import {
  CommandRequestSnapshot,
  DraftDocument,
  DraftRangeRequest,
  DraftSuggestionPreview,
  ModelSettings,
  RepositoryOverview,
} from "../client/types";
import { handleNotificationAction, showErrorNotification, showSuccessNotification } from "../notifications";
import { buildReleaseControlHtml } from "./releaseControlMarkup";
import { PreviewPanel } from "./previewPanel";

export class ReleaseControlViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private document: DraftDocument | undefined;
  private overview: RepositoryOverview | undefined;
  private suggestions: DraftSuggestionPreview | undefined;
  private requests: CommandRequestSnapshot[] = [];
  private modelSettings: ModelSettings;
  private range: DraftRangeRequest = { base: "HEAD~10", head: "HEAD", version: "Unreleased" };
  private busy = false;
  private statusMessage: string | undefined;
  private errorMessage: string | undefined;

  constructor(
    private readonly client: ReleasePilotClient,
    private readonly previewPanel: PreviewPanel,
    private readonly onDidRefresh?: (
      document?: DraftDocument,
      overview?: RepositoryOverview,
      modelSettings?: ModelSettings,
    ) => void,
  ) {
    this.modelSettings = client.getModelSettings();
    this.requests = client.getCommandRequests();
    this.client.onDidChangeRequests((requests) => {
      this.requests = requests;
      this.render();
    });
  }

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
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
    this.onDidRefresh?.(document, overview, modelSettings);
  }

  getDocument(): DraftDocument | undefined {
    return this.document;
  }

  private async handleMessage(message: ViewMessage): Promise<void> {
    if (message.type === "setRange") {
      this.range = {
        base: message.base || "HEAD~10",
        head: message.head || "HEAD",
        version: message.version || "Unreleased",
      };
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
      this.suggestions?.items.forEach((item) => this.recordFeedback("suggestion_regenerated", item.item_id, { scope: "all-visible" }));
      await this.suggest();
    }
    if (message.type === "feedbackPositive" && message.itemId) {
      this.recordFeedback("suggestion_marked_useful", message.itemId);
      this.statusMessage = `Recorded useful feedback for ${message.itemId}.`;
      this.render();
    }
    if (message.type === "feedbackNegative" && message.itemId) {
      this.recordFeedback("suggestion_marked_not_useful", message.itemId);
      this.statusMessage = `Recorded not useful feedback for ${message.itemId}.`;
      this.render();
    }
    if (message.type === "applySuggestion" && message.itemId && message.suggestionIndex !== undefined) {
      await this.applySuggestion(message.itemId, message.suggestionIndex);
    }
    if (message.type === "skipSuggestion" && message.itemId) {
      this.recordFeedback("suggestion_skipped", message.itemId);
      this.skipSuggestion(message.itemId);
    }
    if (message.type === "refresh") {
      await this.refreshRepository(true);
    }
    if (message.type === "changeModel") {
      await vscode.commands.executeCommand("releasepilot.changeModel");
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
    await this.runBusy(async () => {
      const document = await this.client.getDraft(this.range);
      const overview = await this.client.getRepositoryOverview();
      this.statusMessage = `Loaded ${document.item_count} draft item(s) from ${document.base_ref}..${document.head_ref}.`;
      this.suggestions = undefined;
      this.refresh(document, overview, this.client.getModelSettings());
      if (showPreview) {
        this.previewPanel.show(document);
      }
      await handleNotificationAction(
        await showSuccessNotification(
          `ReleasePilot loaded ${document.item_count} draft item(s).`,
          showPreview ? "View Activity" : "Open ReleasePilot",
          showPreview ? "Open ReleasePilot" : "View Activity",
        ),
      );
    });
  }

  private async refreshRepository(showToast: boolean): Promise<void> {
    await this.runBusy(async () => {
      const overview = await this.client.getRepositoryOverview();
      if (this.range.base === "HEAD~10" && overview.tags[0]?.name) {
        this.range = { ...this.range, base: overview.tags[0].name };
      }
      this.statusMessage = `Repository activity loaded: ${overview.commits.length} recent commit(s), ${overview.branches.length} branch(es), ${overview.tags.length} tag(s).`;
      this.refresh(undefined, overview, this.client.getModelSettings());
      if (showToast) {
        await handleNotificationAction(
          await showSuccessNotification(
            `ReleasePilot found ${overview.branches.length} branch(es), ${overview.tags.length} tag(s), and ${overview.unpublishedCount} unpublished item(s).`,
            "Open ReleasePilot",
            "View Activity",
          ),
        );
      }
    });
  }

  private async apply(target: "changelog" | "github-release"): Promise<void> {
    await this.runBusy(async () => {
      const result = await this.client.applyDraft(this.range, target);
      await handleNotificationAction(
        await showSuccessNotification(`Updated ${result.path}`, "Open File", "Preview", "View Activity"),
        {
          "Open File": async () => {
            const folder = vscode.workspace.workspaceFolders?.[0];
            const uri = folder ? vscode.Uri.joinPath(folder.uri, result.path) : vscode.Uri.file(result.path);
            const document = await vscode.workspace.openTextDocument(uri);
            await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
          },
          Preview: async () => {
            const document = await this.client.getDraft(this.range);
            this.previewPanel.show(document);
          },
        },
      );
    });
  }

  private async suggest(itemId?: string): Promise<void> {
    await this.runBusy(async () => {
      try {
        const suggestions = await this.client.previewDraftSuggestions(this.range, itemId);
        this.suggestions = suggestions;
        const generatedCount = suggestions.items[0]?.suggestions.length ?? 0;
        this.statusMessage = itemId
          ? generatedCount === 1 ? "Generated one suggested description." : `Generated ${generatedCount} suggestions.`
          : `Generated suggestions for ${suggestions.items.length} item(s).`;
        await handleNotificationAction(
          await showSuccessNotification(this.statusMessage, "Review Suggestions", "View Activity"),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        this.errorMessage = message;
        await handleNotificationAction(await showErrorNotification(message, "View Activity", "Change Model"));
      }
    });
  }

  private async applySuggestion(itemId: string, suggestionIndex: number): Promise<void> {
    const suggestionItem = this.suggestions?.items.find((item) => item.item_id === itemId);
    const summary = suggestionItem?.suggestions[suggestionIndex];
    if (!summary) {
      return;
    }
    const detail = suggestionItem?.suggestion_details?.find((entry) => entry.summary === summary);
    await this.runBusy(async () => {
      await this.client.reviewDraftItem(this.range, itemId, {
        summary,
        summaryProvenance: {
          source: "model-suggestion",
          provider: this.suggestions?.provider,
          model: suggestionItem?.provider,
          prompt_sha: suggestionItem?.prompt_sha,
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
  }

  private async regenerateSuggestion(itemId: string): Promise<void> {
    this.recordFeedback("suggestion_regenerated", itemId, { scope: "item" });
    await this.suggest(itemId);
  }

  private recordFeedback(event: string, itemId: string, metadata: Record<string, unknown> = {}): void {
    this.client.recordFeedback(event, itemId, metadata).catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      vscode.window.showWarningMessage(`ReleasePilot feedback was not recorded: ${message}`);
    });
  }

  private async setTone(tone: string): Promise<void> {
    await this.client.updateModelSettings({ suggestionTone: tone });
    this.modelSettings = this.client.getModelSettings();
    this.statusMessage = `Suggestion tone set to ${tone}.`;
    this.render();
  }

  private async markItem(itemId: string, status: "needs-review" | "ready" | "accepted" | "hidden"): Promise<void> {
    await this.runBusy(async () => {
      await this.client.reviewDraftItem(this.range, itemId, { status });
      const document = await this.client.getDraft(this.range);
      this.statusMessage = `Updated ${itemId}.`;
      this.refresh(document, undefined, this.client.getModelSettings());
      this.previewPanel.show(document);
    });
  }

  private async openPrompt(itemId: string): Promise<void> {
    await this.runBusy(async () => {
      const promptPreview = await this.client.getModelPrompt(this.range, itemId);
      await vscode.env.clipboard.writeText(promptPreview.prompt);
      const document = await vscode.workspace.openTextDocument({
        content: [
          "# ReleasePilot Model Prompt",
          "",
          `Item: ${promptPreview.title}`,
          `Provider Hint: ${promptPreview.provider}`,
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

  private async runBusy(task: () => Promise<void>): Promise<void> {
    this.busy = true;
    this.errorMessage = undefined;
    this.render();
    try {
      await task();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message === "ReleasePilot command cancelled.") {
        this.statusMessage = "Stopped current ReleasePilot command.";
      } else {
        this.errorMessage = message;
        await handleNotificationAction(await showErrorNotification(this.errorMessage, "View Activity"));
      }
    } finally {
      this.busy = false;
      this.render();
    }
  }

  private cancelCurrentOperation(): void {
    if (!this.busy) {
      return;
    }
    this.statusMessage = "Stopping current ReleasePilot command...";
    this.client.cancelActiveCommands();
    this.render();
  }

  private cancelRequest(requestId: number): void {
    this.statusMessage = `Stopping request ${requestId}...`;
    this.client.cancelCommand(requestId);
    this.render();
  }

  private render(): void {
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
      requests: this.requests,
      statusMessage: this.statusMessage,
      errorMessage: this.errorMessage,
    });
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
  slot?: "base" | "head";
  suggestionIndex?: number;
  requestId?: number;
  text?: string;
  label?: string;
  tone?: string;
};
