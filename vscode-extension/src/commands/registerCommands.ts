import * as vscode from "vscode";

import { ReleasePilotClient } from "../client/releasePilotClient";
import { DraftDocument, DraftItem, DraftRangeRequest, ModelSettings, RepositoryOverview } from "../client/types";
import { PreviewPanel } from "../webview/previewPanel";

interface ReleaseViewState {
  refresh(document?: DraftDocument, overview?: RepositoryOverview, modelSettings?: ModelSettings): void;
  getDocument(): DraftDocument | undefined;
}

type DraftItemNode = { item: DraftItem };

export function registerCommands(
  context: vscode.ExtensionContext,
  client: ReleasePilotClient,
  treeProvider: ReleaseViewState,
  previewPanel: PreviewPanel,
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("releasepilot.scanRepository", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const document = await client.getDraft(request);
      const overview = await client.getRepositoryOverview();
      treeProvider.refresh(document, overview, client.getModelSettings());
      vscode.window.showInformationMessage(`ReleasePilot loaded ${document.item_count} item(s).`);
    }),
    vscode.commands.registerCommand("releasepilot.previewReleaseNotes", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const document = await client.getDraft(request);
      const overview = await client.getRepositoryOverview();
      treeProvider.refresh(document, overview, client.getModelSettings());
      previewPanel.show(document);
    }),
    vscode.commands.registerCommand("releasepilot.refreshRepository", async () => {
      const overview = await client.getRepositoryOverview();
      treeProvider.refresh(undefined, overview, client.getModelSettings());
      vscode.window.showInformationMessage(
        `ReleasePilot found ${overview.branches.length} branch(es), ${overview.tags.length} tag(s), and ${overview.unpublishedCount} unpublished item(s).`,
      );
    }),
    vscode.commands.registerCommand("releasepilot.applyToChangelog", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const result = await client.applyDraft(request, "changelog");
      vscode.window.showInformationMessage(`Updated ${result.path}`);
    }),
    vscode.commands.registerCommand("releasepilot.applyToReleaseFile", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const result = await client.applyDraft(request, "github-release");
      vscode.window.showInformationMessage(`Updated ${result.path}`);
    }),
    vscode.commands.registerCommand("releasepilot.refreshItems", async () => {
      treeProvider.refresh(undefined, undefined, client.getModelSettings());
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
    vscode.commands.registerCommand("releasepilot.suggestItemSummary", async (node?: DraftItemNode) => {
      await suggestItemSummary(client, treeProvider, previewPanel, node);
    }),
    vscode.commands.registerCommand("releasepilot.requestModelInput", async (node?: DraftItemNode) => {
      await requestModelInput(client, treeProvider, previewPanel, node);
    }),
    vscode.commands.registerCommand("releasepilot.acceptItem", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "accepted" });
    }),
    vscode.commands.registerCommand("releasepilot.unhideItem", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "needs-review" });
    }),
    vscode.commands.registerCommand("releasepilot.resetItemSummary", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { summary: "" });
    }),
    vscode.commands.registerCommand("releasepilot.showServiceInfo", async () => {
      void vscode.window.showInformationMessage(
        client.getDiagnosticInfo(),
        { modal: true },
      );
    }),
    vscode.commands.registerCommand("releasepilot.changeModel", async () => {
      await changeModelSettings(client, treeProvider);
    }),
  );
}

async function suggestItemSummary(
  client: ReleasePilotClient,
  treeProvider: ReleaseViewState,
  previewPanel: PreviewPanel,
  node: DraftItemNode | undefined,
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
  try {
    const preview = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `Suggesting descriptions for "${node.item.title}"`,
        cancellable: false,
      },
      async () => client.previewDraftSuggestions(request, node.item.id),
    );
    const suggestions = preview.items[0]?.suggestions ?? [];
    const picked = await vscode.window.showQuickPick(
      suggestions.map((summary, index) => ({ label: summary, description: index === 0 ? "best" : undefined })),
      {
        title: suggestions.length === 1 ? "Review ReleasePilot suggestion" : "Choose ReleasePilot suggestion",
        placeHolder: suggestions.length === 1 ? "Apply the generated description" : "Pick a generated description to apply",
      },
    );
    if (!picked) {
      return;
    }
    await updateItemReviewState(client, treeProvider, previewPanel, node, { summary: picked.label });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    vscode.window.showErrorMessage(message);
    await vscode.commands.executeCommand("releasepilot.changeModel");
  }
}

async function requestModelInput(
  client: ReleasePilotClient,
  treeProvider: ReleaseViewState,
  previewPanel: PreviewPanel,
  node: DraftItemNode | undefined,
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
  try {
    const promptPreview = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `Preparing model prompt for "${node.item.title}"`,
        cancellable: false,
      },
      async () => client.getModelPrompt(request, node.item.id),
    );
    await showPromptPreview(promptPreview);
  } catch (error) {
    vscode.window.showErrorMessage(error instanceof Error ? error.message : String(error));
    return;
  }
  vscode.window.showInformationMessage("ReleasePilot opened the prompt preview and copied the prompt to the clipboard.");
}

async function promptForRange(client: ReleasePilotClient): Promise<DraftRangeRequest | undefined> {
  const overview = await client.getRepositoryOverview();
  const refPick = await vscode.window.showQuickPick(
    [
      ...overview.tags.slice(0, 10).map((tag) => ({ label: tag.name, description: "tag" })),
      ...overview.branches.slice(0, 10).map((branch) => ({ label: branch.name, description: "branch" })),
      ...overview.commits.slice(0, 10).map((commit) => ({
        label: commit.sha,
        description: "commit",
        detail: commit.subject,
      })),
      { label: "HEAD~10", description: "manual default" },
    ],
    {
      title: "ReleasePilot base ref",
      placeHolder: "Pick the previous release tag, branch, or commit to compare from",
    },
  );
  const base = await vscode.window.showInputBox({
    prompt: "ReleasePilot base ref",
    placeHolder: "v1.0.0",
    value: refPick?.label ?? "HEAD~10",
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

async function changeModelSettings(
  client: ReleasePilotClient,
  treeProvider: ReleaseViewState,
): Promise<void> {
  const current = client.getModelSettings();
  const provider = await vscode.window.showQuickPick(
    ["ollama", "configured", "github-copilot", "mock", "stub"],
    {
      title: "ReleasePilot AI provider",
      placeHolder: "Choose the provider used for summaries and classification assists",
    },
  );
  if (!provider) {
    return;
  }

  let model = current.model;
  let endpoint = current.endpoint;
  let ollamaTimeoutSeconds = current.ollamaTimeoutSeconds;
  if (provider === "ollama") {
    const nextModel = await vscode.window.showInputBox({
      prompt: "Ollama model",
      value: current.model || "gemma4:latest",
      placeHolder: "gemma4:latest",
    });
    if (!nextModel) {
      return;
    }
    model = nextModel;
    const nextEndpoint = await vscode.window.showInputBox({
      prompt: "Ollama chat endpoint",
      value: current.endpoint || "http://127.0.0.1:11434/api/chat",
    });
    if (!nextEndpoint) {
      return;
    }
    endpoint = nextEndpoint;
    const timeoutRaw = await vscode.window.showInputBox({
      prompt: "Ollama request timeout in seconds",
      value: String(current.ollamaTimeoutSeconds),
      validateInput: (value) => {
        const parsed = Number(value);
        return Number.isFinite(parsed) && parsed >= 1 ? undefined : "Enter a number of seconds greater than 0.";
      },
    });
    if (timeoutRaw === undefined) {
      return;
    }
    ollamaTimeoutSeconds = Number(timeoutRaw);
  }

  const maxRaw = await vscode.window.showInputBox({
    prompt: "Maximum grouped changes before LLM calls are disabled (-1 disables the guard)",
    value: String(current.maxModelItems),
    validateInput: (value) => Number.isInteger(Number(value)) ? undefined : "Enter an integer.",
  });
  if (maxRaw === undefined) {
    return;
  }

  const suggestionCountRaw = await vscode.window.showInputBox({
    prompt: "Number of suggestions to generate per item (1-5)",
    value: String(current.suggestionCount),
    validateInput: (value) => {
      const parsed = Number(value);
      return Number.isInteger(parsed) && parsed >= 1 && parsed <= 5 ? undefined : "Enter an integer from 1 to 5.";
    },
  });
  if (suggestionCountRaw === undefined) {
    return;
  }
  const suggestionTone = await vscode.window.showQuickPick(
    ["release-note", "concise", "technical", "customer-facing"],
    {
      title: "Suggestion tone",
      placeHolder: "Choose the writing style for generated summaries",
    },
  );
  if (!suggestionTone) {
    return;
  }

  await client.updateModelSettings({
    provider,
    model,
    endpoint,
    ollamaTimeoutSeconds,
    maxModelItems: Number(maxRaw),
    suggestionCount: Number(suggestionCountRaw),
    suggestionTone,
  });
  await client.initialize();
  treeProvider.refresh(undefined, undefined, client.getModelSettings());
  vscode.window.showInformationMessage(`ReleasePilot model set to ${provider}${provider === "ollama" ? ` / ${model}` : ""}.`);
}

async function updateItemReviewState(
  client: ReleasePilotClient,
  treeProvider: ReleaseViewState,
  previewPanel: PreviewPanel,
  node: DraftItemNode | undefined,
  update: { status?: "needs-review" | "ready" | "accepted" | "hidden"; summary?: string },
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

async function showPromptPreview(promptPreview: { title: string; provider: string; prompt: string; item_id: string }): Promise<void> {
  const content = [
    `# ReleasePilot Model Prompt`,
    ``,
    `Item: ${promptPreview.title}`,
    `Item ID: ${promptPreview.item_id}`,
    `Provider Hint: ${promptPreview.provider}`,
    ``,
    `## Prompt`,
    ``,
    promptPreview.prompt,
    ``,
    `ReleasePilot does not send this prompt automatically. Use it manually with your preferred model.`,
  ].join("\n");
  await vscode.env.clipboard.writeText(promptPreview.prompt);
  const document = await vscode.workspace.openTextDocument({
    content,
    language: "markdown",
  });
  await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
}
