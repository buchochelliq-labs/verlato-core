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
    if (message.type === "applySuggestion" && message.itemId && message.suggestionIndex !== undefined) {
      await this.applySuggestion(message.itemId, message.suggestionIndex);
    }
    if (message.type === "skipSuggestion" && message.itemId) {
      this.skipSuggestion(message.itemId);
    }
    if (message.type === "refresh") {
      await this.refreshRepository(true);
    }
    if (message.type === "changeModel") {
      await vscode.commands.executeCommand("releasepilot.changeModel");
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
        vscode.window.showInformationMessage(
          `ReleasePilot found ${overview.branches.length} branch(es), ${overview.tags.length} tag(s), and ${overview.unpublishedCount} unpublished item(s).`,
        );
      }
    });
  }

  private async apply(target: "changelog" | "github-release"): Promise<void> {
    await this.runBusy(async () => {
      const result = await this.client.applyDraft(this.range, target);
      vscode.window.showInformationMessage(`Updated ${result.path}`);
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
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        this.errorMessage = message;
        vscode.window.showErrorMessage(message);
        await vscode.commands.executeCommand("releasepilot.changeModel");
      }
    });
  }

  private async applySuggestion(itemId: string, suggestionIndex: number): Promise<void> {
    const suggestionItem = this.suggestions?.items.find((item) => item.item_id === itemId);
    const summary = suggestionItem?.suggestions[suggestionIndex];
    if (!summary) {
      return;
    }
    await this.runBusy(async () => {
      await this.client.reviewDraftItem(this.range, itemId, { summary });
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
        vscode.window.showErrorMessage(this.errorMessage);
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
};
