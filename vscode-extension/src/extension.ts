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
  const client = new ReleasePilotClient(workspaceFolder, output);
  const treeProvider = new ReleasePilotTreeProvider();
  const previewPanel = new PreviewPanel();

  context.subscriptions.push(
    output,
    client,
    vscode.window.registerTreeDataProvider("releasepilot.items", treeProvider),
  );

  registerCommands(context, client, treeProvider, previewPanel);
}

export function deactivate(): void {}
