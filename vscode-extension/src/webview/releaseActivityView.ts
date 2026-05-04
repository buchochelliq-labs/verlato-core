import * as vscode from "vscode";

import { ReleasePilotClient } from "../client/releasePilotClient";
import { CommandRequestSnapshot } from "../client/types";
import { buildReleaseActivityHtml } from "./releaseControlMarkup";

export class ReleaseActivityViewProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private requests: CommandRequestSnapshot[] = [];

  constructor(private readonly client: ReleasePilotClient) {
    this.requests = client.getCommandRequests();
    this.client.onDidChangeRequests((requests) => {
      this.requests = requests;
      this.updateBadge();
      this.render();
    });
  }

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.onDidReceiveMessage((message) => {
      void this.handleMessage(message as ViewMessage);
    });
    this.updateBadge();
    this.render();
  }

  reveal(): void {
    void vscode.commands.executeCommand("releasepilot.activity.focus");
  }

  private async handleMessage(message: ViewMessage): Promise<void> {
    if (message.type === "cancelRequest" && message.requestId !== undefined) {
      this.client.cancelCommand(message.requestId);
      return;
    }
    if (message.type === "copyText" && message.text !== undefined) {
      await vscode.env.clipboard.writeText(message.text);
    }
  }

  private updateBadge(): void {
    if (!this.view) {
      return;
    }
    const runningCount = this.requests.filter((request) => request.status === "running").length;
    const failedCount = this.requests.filter((request) => request.status === "failed").length;
    if (runningCount) {
      this.view.badge = { value: runningCount, tooltip: `${runningCount} running ReleasePilot request(s)` };
      return;
    }
    if (failedCount) {
      this.view.badge = { value: failedCount, tooltip: `${failedCount} failed ReleasePilot request(s)` };
      return;
    }
    this.view.badge = undefined;
  }

  private render(): void {
    if (!this.view) {
      return;
    }
    this.view.webview.html = buildReleaseActivityHtml({ requests: this.requests });
  }
}

type ViewMessage = {
  type: string;
  requestId?: number;
  text?: string;
};
