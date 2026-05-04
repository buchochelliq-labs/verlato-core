import * as vscode from "vscode";

import { DraftDocument, DraftSuggestionPreview, RepositoryOverview } from "../client/types";
import { buildReleaseReleasesHtml } from "./releaseControlMarkup";

export class ReleaseReleasesViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private document: DraftDocument | undefined;
  private overview: RepositoryOverview | undefined;
  private suggestions: DraftSuggestionPreview | undefined;

  constructor(
    private readonly onUseRef: (ref: string, slot: "base" | "head") => void,
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.onDidReceiveMessage((message) => {
      void this.handleMessage(message as ViewMessage);
    });
    this.render();
  }

  refresh(
    document?: DraftDocument,
    overview?: RepositoryOverview,
    suggestions?: DraftSuggestionPreview,
  ): void {
    if (document !== undefined) {
      this.document = document;
    }
    if (overview !== undefined) {
      this.overview = overview;
    }
    if (suggestions !== undefined) {
      this.suggestions = suggestions;
    }
    this.render();
  }

  reveal(): void {
    void vscode.commands.executeCommand("releasepilot.releases.focus");
  }

  private async handleMessage(message: ViewMessage): Promise<void> {
    if (message.type === "useRef" && message.ref && message.slot) {
      this.onUseRef(message.ref, message.slot);
      this.revealControl();
      return;
    }
    if (message.type === "copyText" && message.text !== undefined) {
      await vscode.env.clipboard.writeText(message.text);
      return;
    }
    this.revealControl();
  }

  private revealControl(): void {
    void vscode.commands.executeCommand("releasepilot.control.focus");
  }

  private render(): void {
    if (!this.view) {
      return;
    }
    this.view.webview.html = buildReleaseReleasesHtml({
      document: this.document,
      overview: this.overview,
      suggestions: this.suggestions,
    });
  }
}

type ViewMessage = {
  type: string;
  ref?: string;
  slot?: "base" | "head";
  text?: string;
};
