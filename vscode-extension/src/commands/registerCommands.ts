import * as vscode from "vscode";

import { VerlatoClient } from "../client/verlatoClient";
import { DraftDocument, DraftItem, DraftRangeRequest, ModelSettings, RepositoryOverview } from "../client/types";
import { handleNotificationAction, showErrorNotification, showSuccessNotification } from "../notifications";
import { PreviewPanel } from "../webview/previewPanel";

interface ReleaseViewState {
  refresh(document?: DraftDocument, overview?: RepositoryOverview, modelSettings?: ModelSettings): void;
  getDocument(): DraftDocument | undefined;
}

type DraftItemNode = { item: DraftItem };

interface GitExtension {
  getAPI(version: 1): GitAPI;
}

interface GitAPI {
  repositories: GitRepository[];
  getRepository(uri: vscode.Uri): GitRepository | null;
  toGitUri(uri: vscode.Uri, ref: string): vscode.Uri;
}

interface GitRepository {
  getCommit(ref: string): Promise<GitCommit>;
  diffBetweenWithStats(ref1: string, ref2: string): Promise<GitDiffChange[]>;
}

interface GitCommit {
  hash: string;
  message: string;
  parents: string[];
  authorDate?: Date;
  authorName?: string;
  authorEmail?: string;
}

interface GitDiffChange {
  uri: vscode.Uri;
  originalUri?: vscode.Uri;
}

export function registerCommands(
  context: vscode.ExtensionContext,
  client: VerlatoClient,
  treeProvider: ReleaseViewState,
  previewPanel: PreviewPanel,
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("verlato.scanRepository", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const document = await client.getDraft(request);
      const overview = await client.getRepositoryOverview();
      treeProvider.refresh(document, overview, client.getModelSettings());
      await handleNotificationAction(
        await showSuccessNotification(`Verlato loaded ${document.item_count} item(s).`, "Open Verlato", "View Activity"),
      );
    }),
    vscode.commands.registerCommand("verlato.previewReleaseNotes", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const document = await client.getDraft(request);
      const overview = await client.getRepositoryOverview();
      treeProvider.refresh(document, overview, client.getModelSettings());
      previewPanel.show(document);
      await handleNotificationAction(
        await showSuccessNotification(`Verlato previewed ${document.item_count} item(s).`, "Open Verlato", "View Activity"),
      );
    }),
    vscode.commands.registerCommand("verlato.refreshRepository", async () => {
      const overview = await client.getRepositoryOverview();
      treeProvider.refresh(undefined, overview, client.getModelSettings());
      await handleNotificationAction(
        await showSuccessNotification(
          `Verlato found ${overview.branches.length} branch(es), ${overview.tags.length} tag(s), and ${overview.unpublishedCount} unpublished item(s).`,
          "Open Verlato",
          "View Activity",
        ),
      );
    }),
    vscode.commands.registerCommand("verlato.applyToChangelog", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const result = await client.applyDraft(request, "changelog");
      await showApplyNotification(client, request, previewPanel, result.path);
    }),
    vscode.commands.registerCommand("verlato.applyToReleaseFile", async () => {
      const request = await promptForRange(client);
      if (!request) {
        return;
      }
      const result = await client.applyDraft(request, "github-release");
      await showApplyNotification(client, request, previewPanel, result.path);
    }),
    vscode.commands.registerCommand("verlato.refreshItems", async () => {
      treeProvider.refresh(undefined, undefined, client.getModelSettings());
    }),
    vscode.commands.registerCommand("verlato.openCommit", async (sha?: string) => {
      await openCommit(sha);
    }),
    vscode.commands.registerCommand("verlato.markItemReady", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "ready" });
    }),
    vscode.commands.registerCommand("verlato.markItemNeedsReview", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "needs-review" });
    }),
    vscode.commands.registerCommand("verlato.hideItem", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "hidden" });
    }),
    vscode.commands.registerCommand("verlato.editItemSummary", async (node?: DraftItemNode) => {
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
    vscode.commands.registerCommand("verlato.suggestItemSummary", async (node?: DraftItemNode) => {
      await suggestItemSummary(client, treeProvider, previewPanel, node);
    }),
    vscode.commands.registerCommand("verlato.requestModelInput", async (node?: DraftItemNode) => {
      await requestModelInput(client, treeProvider, previewPanel, node);
    }),
    vscode.commands.registerCommand("verlato.acceptItem", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "accepted" });
    }),
    vscode.commands.registerCommand("verlato.unhideItem", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { status: "needs-review" });
    }),
    vscode.commands.registerCommand("verlato.resetItemSummary", async (node?: DraftItemNode) => {
      await updateItemReviewState(client, treeProvider, previewPanel, node, { summary: "" });
    }),
    vscode.commands.registerCommand("verlato.showServiceInfo", async () => {
      void vscode.window.showInformationMessage(
        client.getDiagnosticInfo(),
        { modal: true },
      );
    }),
    vscode.commands.registerCommand("verlato.changeModel", async () => {
      await changeModelSettings(client, treeProvider);
    }),
  );
}

async function openCommit(sha: string | undefined): Promise<void> {
  if (!sha) {
    return;
  }
  try {
    await vscode.commands.executeCommand("git.showCommit", sha);
    return;
  } catch {
    // Older VS Code builds do not expose a commit-detail command. Fall back to the built-in Git API.
  }

  try {
    const git = await getWorkspaceGitRepository();
    if (!git) {
      await vscode.window.showWarningMessage("VS Code Git is not available for this workspace.");
      return;
    }
    const { api, repository } = git;
    const commit = await repository.getCommit(sha);
    if (await openCommitDiff(api, repository, commit)) {
      return;
    }
    await showCommitDetails(commit);
  } catch (error) {
    await vscode.window.showErrorMessage(`Unable to open commit ${sha}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function getWorkspaceGitRepository(): Promise<{ api: GitAPI; repository: GitRepository } | undefined> {
  const extension = vscode.extensions.getExtension<GitExtension>("vscode.git");
  const git = await extension?.activate();
  const api = git?.getAPI(1);
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!api || !folder) {
    return undefined;
  }
  const repository = api.getRepository(folder.uri) ?? api.repositories[0];
  return repository ? { api, repository } : undefined;
}

async function openCommitDiff(api: GitAPI, repository: GitRepository, commit: GitCommit): Promise<boolean> {
  const parent = commit.parents[0];
  if (!parent) {
    return false;
  }
  const changes = await repository.diffBetweenWithStats(parent, commit.hash);
  if (!changes.length) {
    return false;
  }
  await vscode.commands.executeCommand("_workbench.openMultiDiffEditor", {
    title: commit.message.split(/\r?\n/, 1)[0] || commit.hash,
    resources: changes.map((change) => ({
      originalUri: api.toGitUri(change.originalUri ?? change.uri, parent),
      modifiedUri: api.toGitUri(change.uri, commit.hash),
    })),
  });
  return true;
}

async function showCommitDetails(commit: GitCommit): Promise<void> {
  const content = [
    `# Commit ${commit.hash}`,
    ``,
    commit.message,
    ``,
    `Author: ${formatAuthor(commit)}`,
    `Date: ${commit.authorDate?.toLocaleString() ?? "unknown"}`,
    `Parents: ${commit.parents.join(", ") || "none"}`,
  ].join("\n");
  const document = await vscode.workspace.openTextDocument({ content, language: "markdown" });
  await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
}

function formatAuthor(commit: GitCommit): string {
  if (commit.authorName && commit.authorEmail) {
    return `${commit.authorName} <${commit.authorEmail}>`;
  }
  return commit.authorName ?? commit.authorEmail ?? "unknown";
}

async function suggestItemSummary(
  client: VerlatoClient,
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
        title: suggestions.length === 1 ? "Review Verlato suggestion" : "Choose Verlato suggestion",
        placeHolder: suggestions.length === 1 ? "Apply the generated description" : "Pick a generated description to apply",
      },
    );
    if (!picked) {
      return;
    }
    await updateItemReviewState(client, treeProvider, previewPanel, node, { summary: picked.label });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await handleNotificationAction(await showErrorNotification(message, "View Activity", "Change Model"));
  }
}

async function requestModelInput(
  client: VerlatoClient,
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
    await handleNotificationAction(
      await showErrorNotification(error instanceof Error ? error.message : String(error), "View Activity"),
    );
    return;
  }
  await handleNotificationAction(
    await showSuccessNotification("Verlato opened the prompt preview and copied it to the clipboard.", "View Activity"),
  );
}

async function promptForRange(client: VerlatoClient): Promise<DraftRangeRequest | undefined> {
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
      title: "Verlato base ref",
      placeHolder: "Pick the previous release tag, branch, or commit to compare from",
    },
  );
  const base = await vscode.window.showInputBox({
    prompt: "Verlato base ref",
    placeHolder: "v1.0.0",
    value: refPick?.label ?? "HEAD~10",
  });
  if (!base) {
    return undefined;
  }
  const head = await vscode.window.showInputBox({
    prompt: "Verlato head ref",
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
  client: VerlatoClient,
  treeProvider: ReleaseViewState,
): Promise<void> {
  const current = client.getModelSettings();
  const provider = await vscode.window.showQuickPick(
    ["ollama", "litellm", "plugin", "configured", "github-copilot", "codex-cli", "claude-code", "mock", "stub"],
    {
      title: "Verlato AI provider",
      placeHolder: "Choose the provider used for summaries and classification assists",
    },
  );
  if (!provider) {
    return;
  }

  let model = current.model;
  let suggestionModel = current.suggestionModel;
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
    const nextSuggestionModel = await vscode.window.showInputBox({
      prompt: "Ollama suggestion model (empty uses the main model)",
      value: current.suggestionModel,
      placeHolder: nextModel,
    });
    if (nextSuggestionModel === undefined) {
      return;
    }
    suggestionModel = nextSuggestionModel.trim();
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
  const suggestionParallelismRaw = await vscode.window.showInputBox({
    prompt: "Maximum suggestions to generate in parallel (1-8)",
    value: String(current.suggestionParallelism),
    validateInput: (value) => {
      const parsed = Number(value);
      return Number.isInteger(parsed) && parsed >= 1 && parsed <= 8 ? undefined : "Enter an integer from 1 to 8.";
    },
  });
  if (suggestionParallelismRaw === undefined) {
    return;
  }
  const suggestionBatchSizeRaw = await vscode.window.showInputBox({
    prompt: "Maximum uncached Ollama suggestion items per request (1-5)",
    value: String(current.suggestionBatchSize),
    validateInput: (value) => {
      const parsed = Number(value);
      return Number.isInteger(parsed) && parsed >= 1 && parsed <= 5 ? undefined : "Enter an integer from 1 to 5.";
    },
  });
  if (suggestionBatchSizeRaw === undefined) {
    return;
  }
  const suggestionStrategy = await vscode.window.showQuickPick(
    ["auto", "single", "paired", "full-diff-json"],
    {
      title: "Suggestion strategy",
      placeHolder: "Choose how Verlato sends suggestion evidence to the model",
    },
  );
  if (!suggestionStrategy) {
    return;
  }
  const suggestionPromptProfile = await vscode.window.showQuickPick(
    ["conservative", "lean", "minimal"],
    {
      title: "Suggestion prompt profile",
      placeHolder: "Choose how much evidence to send for suggestion prompts",
    },
  );
  if (!suggestionPromptProfile) {
    return;
  }
  const adaptiveChoice = await vscode.window.showQuickPick(
    ["disabled", "enabled"],
    {
      title: "Adaptive suggestion recommendations",
      placeHolder: "Show opt-in performance recommendations without applying them",
    },
  );
  if (!adaptiveChoice) {
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
    suggestionModel,
    endpoint,
    ollamaTimeoutSeconds,
    maxModelItems: Number(maxRaw),
    suggestionCount: Number(suggestionCountRaw),
    suggestionParallelism: Number(suggestionParallelismRaw),
    suggestionBatchSize: Number(suggestionBatchSizeRaw),
    suggestionStrategy,
    suggestionPromptProfile,
    suggestionAdaptiveRecommendations: adaptiveChoice === "enabled",
    suggestionTone,
  });
  await client.initialize();
  treeProvider.refresh(undefined, undefined, client.getModelSettings());
  await handleNotificationAction(
    await showSuccessNotification(
      `Verlato model set to ${provider}${provider === "ollama" ? ` / ${model}` : ""}.`,
      "Open Verlato",
    ),
  );
}

async function showApplyNotification(
  client: VerlatoClient,
  request: DraftRangeRequest,
  previewPanel: PreviewPanel,
  resultPath: string,
): Promise<void> {
  await handleNotificationAction(await showSuccessNotification(`Updated ${resultPath}`, "Open File", "Preview", "View Activity"), {
    "Open File": async () => {
      const folder = vscode.workspace.workspaceFolders?.[0];
      const uri = folder ? vscode.Uri.joinPath(folder.uri, resultPath) : vscode.Uri.file(resultPath);
      const document = await vscode.workspace.openTextDocument(uri);
      await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
    },
    Preview: async () => {
      previewPanel.show(await client.getDraft(request));
    },
  });
}

async function updateItemReviewState(
  client: VerlatoClient,
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
    `# Verlato Model Prompt`,
    ``,
    `Item: ${promptPreview.title}`,
    `Item ID: ${promptPreview.item_id}`,
    `Provider Hint: ${promptPreview.provider}`,
    ``,
    `## Prompt`,
    ``,
    promptPreview.prompt,
    ``,
    `Verlato does not send this prompt automatically. Use it manually with your preferred model.`,
  ].join("\n");
  await vscode.env.clipboard.writeText(promptPreview.prompt);
  const document = await vscode.workspace.openTextDocument({
    content,
    language: "markdown",
  });
  await vscode.window.showTextDocument(document, vscode.ViewColumn.Beside, true);
}
