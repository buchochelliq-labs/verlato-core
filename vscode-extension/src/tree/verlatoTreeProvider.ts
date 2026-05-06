import * as vscode from "vscode";

import { DraftDocument, DraftItem, DraftSection, ModelSettings, RepositoryCommit, RepositoryOverview, RepositoryRef } from "../client/types";

type VerlatoNode =
  | RootNode
  | StatusGroupNode
  | DraftItemNode
  | RepositoryRefNode
  | RepositoryCommitNode
  | ReleaseNode
  | ModelNode
  | EmptyNode;
const STATUS_ORDER = ["needs-review", "ready", "accepted", "edited", "hidden"];

export class VerlatoTreeProvider implements vscode.TreeDataProvider<VerlatoNode> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<VerlatoNode | undefined>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;
  private document: DraftDocument | undefined;
  private overview: RepositoryOverview | undefined;
  private modelSettings: ModelSettings | undefined;

  refresh(document?: DraftDocument, overview?: RepositoryOverview, modelSettings?: ModelSettings): void {
    if (document) {
      this.document = document;
    }
    if (overview) {
      this.overview = overview;
    }
    if (modelSettings) {
      this.modelSettings = modelSettings;
    }
    this.onDidChangeTreeDataEmitter.fire(undefined);
  }

  getTreeItem(element: VerlatoNode): vscode.TreeItem {
    return element;
  }

  getChildren(element?: VerlatoNode): VerlatoNode[] {
    if (!element) {
      return [
        new RootNode("Draft Items", "drafts", this.document?.item_count ?? 0),
        new RootNode("Repository Activity", "activity", this.overview?.commits.length ?? 0),
        new RootNode("Release Associations", "releases", this.overview?.releases.length ?? 0),
        new RootNode("AI Model", "model", undefined),
      ];
    }
    if (element instanceof RootNode && element.kind === "drafts") {
      if (!this.document) {
        return [new EmptyNode("Scan the repository to load release-note items.")];
      }
      return this.groupItemsByStatus(this.document.sections);
    }
    if (element instanceof RootNode && element.kind === "activity") {
      if (!this.overview) {
        return [new EmptyNode("Refresh repository activity to load branches, tags, and commits.")];
      }
      return [
        new RootNode("Branches", "branches", this.overview.branches.length),
        new RootNode("Tags", "tags", this.overview.tags.length),
        new RootNode("Recent Commits", "commits", this.overview.commits.length),
      ];
    }
    if (element instanceof RootNode && element.kind === "branches") {
      return (this.overview?.branches ?? []).map((branch) => new RepositoryRefNode(branch, "branch"));
    }
    if (element instanceof RootNode && element.kind === "tags") {
      return (this.overview?.tags ?? []).map((tag) => new RepositoryRefNode(tag, "tag"));
    }
    if (element instanceof RootNode && element.kind === "commits") {
      return (this.overview?.commits ?? []).map((commit) => new RepositoryCommitNode(commit));
    }
    if (element instanceof RootNode && element.kind === "releases") {
      if (!this.overview) {
        return [new EmptyNode("Refresh repository activity to load release associations.")];
      }
      const releases = this.overview.releases.map((release) => new ReleaseNode(
        release.version,
        release.itemCount,
        this.commitsForRelease(release.version).length,
      ));
      return [
        new ReleaseNode("Unpublished", this.overview.unpublishedCount, this.commitsForRelease(null).length),
        ...releases,
      ];
    }
    if (element instanceof ReleaseNode) {
      const commits = this.commitsForRelease(element.version === "Unpublished" ? null : element.version);
      if (!commits.length) {
        return [new EmptyNode("No recent commits found for this release association.")];
      }
      return commits.map((commit) => new RepositoryCommitNode(commit));
    }
    if (element instanceof RootNode && element.kind === "model") {
      if (!this.modelSettings) {
        return [new EmptyNode("Model settings have not loaded yet.")];
      }
      return [new ModelNode(this.modelSettings)];
    }
    if (element instanceof StatusGroupNode) {
      return element.items.map((item) => new DraftItemNode(item));
    }
    return [];
  }

  getDocument(): DraftDocument | undefined {
    return this.document;
  }

  private groupItemsByStatus(sections: DraftSection[]): VerlatoNode[] {
    const grouped = new Map<string, DraftItem[]>();
    for (const section of sections) {
      for (const item of section.items) {
        const items = grouped.get(item.status) ?? [];
        items.push(item);
        grouped.set(item.status, items);
      }
    }
    return STATUS_ORDER.filter((status) => (grouped.get(status)?.length ?? 0) > 0).map(
      (status) => new StatusGroupNode(status, grouped.get(status) ?? []),
    );
  }

  private commitsForRelease(version: string | null): RepositoryCommit[] {
    return (this.overview?.commits ?? []).filter((commit) => commit.release === version);
  }
}

class RootNode extends vscode.TreeItem {
  constructor(readonly labelText: string, readonly kind: string, count: number | undefined) {
    super(labelText, vscode.TreeItemCollapsibleState.Expanded);
    this.description = count === undefined ? undefined : String(count);
    this.contextValue = `verlato.${kind}`;
  }
}

class StatusGroupNode extends vscode.TreeItem {
  constructor(readonly status: string, readonly items: DraftItem[]) {
    super(`${toStatusLabel(status)} (${items.length})`, vscode.TreeItemCollapsibleState.Expanded);
    this.description = status;
    this.contextValue = "verlato.status";
  }
}

class RepositoryRefNode extends vscode.TreeItem {
  constructor(ref: RepositoryRef, kind: "branch" | "tag") {
    super(ref.name, vscode.TreeItemCollapsibleState.None);
    this.description = ref.detail;
    this.contextValue = `verlato.${kind}`;
  }
}

class RepositoryCommitNode extends vscode.TreeItem {
  constructor(commit: RepositoryCommit) {
    super(commit.subject, vscode.TreeItemCollapsibleState.None);
    this.description = [
      commit.sha,
      commit.prNumber ? `PR #${commit.prNumber}` : undefined,
      commit.release ? `released in ${commit.release}` : "unreleased",
    ].filter(Boolean).join(" | ");
    this.tooltip = `${commit.subject}\n${commit.author}`;
    this.contextValue = "verlato.commit";
    this.command = {
      command: "verlato.openCommit",
      title: "Open Commit",
      arguments: [commit.sha],
    };
  }
}

class ReleaseNode extends vscode.TreeItem {
  constructor(readonly version: string, itemCount: number, commitCount: number) {
    super(version, commitCount > 0 ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.description = `${itemCount} item(s)`;
    this.contextValue = "verlato.release";
    this.tooltip = [
      `${version}: ${itemCount} release-note item(s)`,
      commitCount ? `${commitCount} recent commit(s) available to open.` : "No recent commits are currently mapped here.",
    ].join("\n");
  }
}

class ModelNode extends vscode.TreeItem {
  constructor(settings: ModelSettings) {
    super(settings.provider, vscode.TreeItemCollapsibleState.None);
    this.description = settings.provider === "ollama" ? settings.model : undefined;
    this.tooltip = [
      `Provider: ${settings.provider}`,
      `Model: ${settings.model}`,
      `Suggestion model: ${settings.suggestionModel || "same as main"}`,
      `Endpoint: ${settings.endpoint}`,
      `Timeout: ${settings.ollamaTimeoutSeconds}s`,
      `Fanout guard: ${settings.maxModelItems}`,
      `Suggestion cache warm: ${settings.suggestionAutoWarmCache ? "enabled" : "disabled"}`,
      `Instant provisional suggestions: ${settings.suggestionInstantProvisional ? "enabled" : "disabled"}`,
      `Suggestion prompt profile: ${settings.suggestionPromptProfile}`,
      `Adaptive suggestions: ${settings.suggestionAdaptiveRecommendations ? "enabled" : "disabled"}`,
      `Ollama streaming: ${settings.ollamaStreamResponses ? "enabled" : "disabled"}`,
      `Prompt payloads: ${settings.includePromptPayloads ? "enabled" : "disabled"}`,
      `Reasoning payloads: ${settings.includeReasoningPayloads ? "enabled" : "disabled"}`,
      `Payload redaction: ${settings.redactPayloads ? "enabled" : "disabled"}`,
      `Debug log: ${settings.debugModelLogPath || "disabled"}`,
    ].join("\n");
    this.contextValue = "verlato.model";
  }
}

export class DraftItemNode extends vscode.TreeItem {
  constructor(readonly item: DraftItem) {
    super(item.title, vscode.TreeItemCollapsibleState.None);
    this.description = `${item.kind} | ${item.area} | ${toStatusLabel(item.status)} | ${item.confidence.toFixed(2)}`;
    this.tooltip = [
      item.summary,
      `${item.base_ref}..${item.head_ref}`,
      `Signals: ${item.commit_signals.join(", ") || "none"}`,
      `Files: ${item.files.join(", ") || "none"}`,
      `Reviewer: ${item.reviewed_by || "unassigned"}`,
    ].join("\n");
    this.contextValue = "verlato.item";
  }
}

class EmptyNode extends vscode.TreeItem {
  constructor(label: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.contextValue = "verlato.empty";
  }
}

function toStatusLabel(status: string): string {
  return {
    "needs-review": "Needs Review",
    ready: "Ready",
    accepted: "Accepted",
    edited: "Edited",
    hidden: "Hidden",
  }[status] ?? status;
}
