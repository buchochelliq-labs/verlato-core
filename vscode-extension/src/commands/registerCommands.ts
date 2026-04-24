import * as vscode from "vscode";

import { ReleasePilotClient } from "../client/releasePilotClient";
import { DraftRangeRequest } from "../client/types";
import { DraftItemNode, ReleasePilotTreeProvider } from "../tree/releasePilotTreeProvider";
import { PreviewPanel } from "../webview/previewPanel";

export function registerCommands(
  context: vscode.ExtensionContext,
  client: ReleasePilotClient,
  treeProvider: ReleasePilotTreeProvider,
  previewPanel: PreviewPanel,
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("releasepilot.scanRepository", async () => {
      const request = await promptForRange();
      if (!request) {
        return;
      }
      const document = await client.getDraft(request);
      treeProvider.refresh(document);
      vscode.window.showInformationMessage(`ReleasePilot loaded ${document.item_count} item(s).`);
    }),
    vscode.commands.registerCommand("releasepilot.previewReleaseNotes", async () => {
      const request = await promptForRange();
      if (!request) {
        return;
      }
      const document = await client.getDraft(request);
      treeProvider.refresh(document);
      previewPanel.show(document);
    }),
    vscode.commands.registerCommand("releasepilot.applyToChangelog", async () => {
      const request = await promptForRange();
      if (!request) {
        return;
      }
      const result = await client.applyDraft(request, "changelog");
      vscode.window.showInformationMessage(`Updated ${result.path}`);
    }),
    vscode.commands.registerCommand("releasepilot.applyToReleaseFile", async () => {
      const request = await promptForRange();
      if (!request) {
        return;
      }
      const result = await client.applyDraft(request, "github-release");
      vscode.window.showInformationMessage(`Updated ${result.path}`);
    }),
    vscode.commands.registerCommand("releasepilot.refreshItems", async () => {
      treeProvider.refresh();
    }),
    vscode.commands.registerCommand("releasepilot.markItemReady", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "ready" });
    }),
    vscode.commands.registerCommand("releasepilot.markItemNeedsReview", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "needs-review" });
    }),
    vscode.commands.registerCommand("releasepilot.hideItem", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "hidden" });
    }),
    vscode.commands.registerCommand("releasepilot.editItemSummary", async (node?: DraftItemNode) => {
      if (!node) {
        return;
      }
      const summary = await vscode.window.showInputBox({
        prompt: `Edited summary for ${node.item.title}`,
        value: node.item.edited_summary ?? node.item.summary,
      });
      if (summary === undefined) {
        return;
      }
      await updateItemReviewState(client, treeProvider, previewPanel, node, { summary });
    }),
  );
}

async function promptForRange(): Promise<DraftRangeRequest | undefined> {
  const base = await vscode.window.showInputBox({
    prompt: "ReleasePilot base ref",
    placeHolder: "v1.0.0",
    value: "HEAD~10",
  });
  if (!base) {
    return undefined;
  }
  const head = await vscode.window.showInputBox({
    prompt: "ReleasePilot head ref",
    placeHolder: "HEAD",
    value: "HEAD",
  });
  const version = await vscode.window.showInputBox({
    prompt: "Release note version",
    placeHolder: "Unreleased",
    value: "Unreleased",
  });
  return {
    base,
    head: head || "HEAD",
    version: version || "Unreleased",
  };
}

async function updateItemReviewState(
  client: ReleasePilotClient,
  treeProvider: ReleasePilotTreeProvider,
  previewPanel: PreviewPanel,
  node: DraftItemNode | undefined,
  update: { status?: "needs-review" | "ready" | "hidden"; summary?: string },
): Promise<void> {
  if (!node) {
    return;
  }
  const document = treeProvider.getDocument();
  if (!document) {
    return;
  }
  const request: DraftRangeRequest = {
    base: document.base_ref,
    head: document.head_ref,
    version: document.version,
  };
  await client.reviewDraftItem(request, node.item.id, update);
  const nextDocument = await client.getDraft(request);
  treeProvider.refresh(nextDocument);
  previewPanel.show(nextDocument);
}
