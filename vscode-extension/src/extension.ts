import * as vscode from "vscode";

import { registerCommands } from "./commands/registerCommands";
import { ReleasePilotClient } from "./client/releasePilotClient";
import { ReleasePilotTreeProvider } from "./tree/releasePilotTreeProvider";
import { PreviewPanel } from "./webview/previewPanel";

export function activate(context: vscode.ExtensionContext): void {
  const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
  if (!workspaceFolder) {
    return;
  }

  const output = vscode.window.createOutputChannel("ReleasePilot");
  const client = new ReleasePilotClient(workspaceFolder, context.extensionPath, output);
  const treeProvider = new ReleasePilotTreeProvider();
  const previewPanel = new PreviewPanel();
  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = "releasepilot.showServiceInfo";
  statusBarItem.text = `ReleasePilot: ${client.getModelSettings().provider}`;
  statusBarItem.tooltip = "ReleasePilot model and service information";
  statusBarItem.show();

  context.subscriptions.push(
    output,
    client,
    statusBarItem,
    vscode.window.registerTreeDataProvider("releasepilot.items", treeProvider),
  );

  client.onDidChangeStatus((status) => {
    statusBarItem.text = status.label;
    statusBarItem.tooltip = status.detail;
  });

  registerCommands(context, client, treeProvider, previewPanel);
  treeProvider.refresh(undefined, undefined, client.getModelSettings());
  void client.initialize();
}

export function deactivate(): void {}
