import * as vscode from "vscode";

import { registerCommands } from "./commands/registerCommands";
import { ReleasePilotClient } from "./client/releasePilotClient";
import { PreviewPanel } from "./webview/previewPanel";
import { ReleaseControlViewProvider } from "./webview/releaseControlView";

export function activate(context: vscode.ExtensionContext): void {
  const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
  if (!workspaceFolder) {
    return;
  }

  const output = vscode.window.createOutputChannel("ReleasePilot");
  const client = new ReleasePilotClient(workspaceFolder, context.extensionPath, output);
  const previewPanel = new PreviewPanel();
  const releaseControlView = new ReleaseControlViewProvider(client, previewPanel);
  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = "releasepilot.showServiceInfo";
  statusBarItem.text = `ReleasePilot: ${client.getModelSettings().provider}`;
  statusBarItem.tooltip = "ReleasePilot model and service information";
  statusBarItem.show();

  context.subscriptions.push(
    output,
    client,
    statusBarItem,
    vscode.window.registerWebviewViewProvider("releasepilot.items", releaseControlView),
  );

  client.onDidChangeStatus((status) => {
    statusBarItem.text = status.label;
    statusBarItem.tooltip = status.detail;
  });

  registerCommands(context, client, releaseControlView, previewPanel);
  releaseControlView.refresh(undefined, undefined, client.getModelSettings());
  void client.initialize();
}

export function deactivate(): void {}
