import * as vscode from "vscode";

import { registerCommands } from "./commands/registerCommands";
import { ReleasePilotClient } from "./client/releasePilotClient";
import { ReleaseSourcesProvider } from "./tree/releaseSourcesProvider";
import { ReleaseActivityViewProvider } from "./webview/releaseActivityView";
import { PreviewPanel } from "./webview/previewPanel";
import { ReleaseControlViewProvider } from "./webview/releaseControlView";
import { ReleaseReleasesViewProvider } from "./webview/releaseReleasesView";

export function activate(context: vscode.ExtensionContext): void {
  const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
  if (!workspaceFolder) {
    return;
  }

  const output = vscode.window.createOutputChannel("ReleasePilot");
  const client = new ReleasePilotClient(workspaceFolder, context.extensionPath, output);
  const previewPanel = new PreviewPanel();
  const releaseSourcesProvider = new ReleaseSourcesProvider();
  let releaseReleasesView: ReleaseReleasesViewProvider;
  const releaseControlView = new ReleaseControlViewProvider(context.extensionUri, client, previewPanel, (document, overview, _modelSettings, suggestions) => {
    releaseSourcesProvider.refresh(overview);
    releaseReleasesView.refresh(document, overview, suggestions);
  });
  releaseReleasesView = new ReleaseReleasesViewProvider((ref, slot) => {
    releaseControlView.useRef(ref, slot);
  });
  const releaseActivityView = new ReleaseActivityViewProvider(client);
  const releaseViewState = {
    refresh: (...args: Parameters<ReleaseControlViewProvider["refresh"]>) => {
      const [_document, overview] = args;
      releaseControlView.refresh(...args);
      releaseSourcesProvider.refresh(overview);
    },
    getDocument: () => releaseControlView.getDocument(),
  };
  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = "releasepilot.showServiceInfo";
  statusBarItem.text = `ReleasePilot: ${client.getModelSettings().provider}`;
  statusBarItem.tooltip = "ReleasePilot model and service information";
  statusBarItem.show();

  context.subscriptions.push(
    output,
    client,
    statusBarItem,
    vscode.window.registerWebviewViewProvider("releasepilot.control", releaseControlView),
    vscode.window.registerWebviewViewProvider("releasepilot.releases", releaseReleasesView),
    vscode.window.registerWebviewViewProvider("releasepilot.activity", releaseActivityView),
    vscode.window.createTreeView("releasepilot.sources", { treeDataProvider: releaseSourcesProvider }),
    vscode.commands.registerCommand("releasepilot.openControl", () => {
      void vscode.commands.executeCommand("releasepilot.control.focus");
    }),
    vscode.commands.registerCommand("releasepilot.openActivity", () => {
      releaseActivityView.reveal();
    }),
  );

  client.onDidChangeStatus((status) => {
    statusBarItem.text = status.label;
    statusBarItem.tooltip = status.detail;
  });

  registerCommands(context, client, releaseViewState, previewPanel);
  releaseViewState.refresh(undefined, undefined, client.getModelSettings());
  void client.initialize();
}

export function deactivate(): void {}
