import * as vscode from "vscode";

import { RepositoryCommit, RepositoryOverview, RepositoryRef } from "../client/types";

type SourceNode = SourceGroupNode | RefNode | CommitNode | ReleaseNode | EmptyNode;

export class ReleaseSourcesProvider implements vscode.TreeDataProvider<SourceNode> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<SourceNode | undefined>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;
  private overview: RepositoryOverview | undefined;

  refresh(overview?: RepositoryOverview): void {
    if (overview) {
      this.overview = overview;
    }
    this.onDidChangeTreeDataEmitter.fire(undefined);
  }

  getTreeItem(element: SourceNode): vscode.TreeItem {
    return element;
  }

  getChildren(element?: SourceNode): SourceNode[] {
    if (!this.overview) {
      return [new EmptyNode("Refresh repository activity to load sources.")];
    }
    if (!element) {
      return [
        new SourceGroupNode("PRs From Commits", "prs", this.prCommits().length),
        new SourceGroupNode("Tags", "tags", this.overview.tags.length),
        new SourceGroupNode("Branches", "branches", this.overview.branches.length),
        new SourceGroupNode("Recent Commits", "commits", this.overview.commits.length),
        new SourceGroupNode("Associated Releases", "releases", this.overview.releases.length + 1),
      ].filter((node) => node.count > 0);
    }
    if (element instanceof SourceGroupNode && element.kind === "prs") {
      return this.prCommits().map((commit) => new CommitNode(commit, "pull-request"));
    }
    if (element instanceof SourceGroupNode && element.kind === "tags") {
      return this.overview.tags.map((tag) => new RefNode(tag, "tag"));
    }
    if (element instanceof SourceGroupNode && element.kind === "branches") {
      return this.overview.branches.map((branch) => new RefNode(branch, "branch"));
    }
    if (element instanceof SourceGroupNode && element.kind === "commits") {
      return this.overview.commits.map((commit) => new CommitNode(commit, "commit"));
    }
    if (element instanceof SourceGroupNode && element.kind === "releases") {
      return [
        new ReleaseNode("Unpublished", this.overview.unpublishedCount),
        ...this.overview.releases.map((release) => new ReleaseNode(release.version, release.itemCount)),
      ];
    }
    return [];
  }

  private prCommits(): RepositoryCommit[] {
    return (this.overview?.commits ?? []).filter((commit) => commit.prNumber !== null);
  }
}

class SourceGroupNode extends vscode.TreeItem {
  constructor(readonly labelText: string, readonly kind: string, readonly count: number) {
    super(labelText, vscode.TreeItemCollapsibleState.Expanded);
    this.description = String(count);
    this.contextValue = `releasepilot.source.${kind}`;
    this.iconPath = new vscode.ThemeIcon(groupIcon(kind));
  }
}

class RefNode extends vscode.TreeItem {
  constructor(ref: RepositoryRef, kind: "branch" | "tag") {
    super(ref.name, vscode.TreeItemCollapsibleState.None);
    this.description = ref.detail;
    this.tooltip = `${kind}: ${ref.name}${ref.detail ? `\n${ref.detail}` : ""}`;
    this.contextValue = `releasepilot.source.${kind}`;
    this.iconPath = new vscode.ThemeIcon(kind === "tag" ? "tag" : "git-branch");
  }
}

class CommitNode extends vscode.TreeItem {
  constructor(commit: RepositoryCommit, kind: "commit" | "pull-request") {
    super(kind === "pull-request" ? `PR #${commit.prNumber}` : commit.sha, vscode.TreeItemCollapsibleState.None);
    this.description = commit.release ? `released in ${commit.release}` : commit.subject;
    this.tooltip = [
      commit.subject,
      commit.author,
      `Commit: ${commit.sha}`,
      commit.prNumber ? `PR #${commit.prNumber}` : undefined,
      commit.release ? `Release: ${commit.release}` : "Unreleased",
    ].filter(Boolean).join("\n");
    this.contextValue = `releasepilot.source.${kind}`;
    this.iconPath = new vscode.ThemeIcon(kind === "pull-request" ? "git-pull-request" : "git-commit");
  }
}

class ReleaseNode extends vscode.TreeItem {
  constructor(version: string, itemCount: number) {
    super(version, vscode.TreeItemCollapsibleState.None);
    this.description = `${itemCount} item(s)`;
    this.contextValue = "releasepilot.source.release";
    this.iconPath = new vscode.ThemeIcon(version === "Unpublished" ? "circle-slash" : "package");
  }
}

class EmptyNode extends vscode.TreeItem {
  constructor(label: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.contextValue = "releasepilot.source.empty";
    this.iconPath = new vscode.ThemeIcon("info");
  }
}

function groupIcon(kind: string): string {
  return {
    prs: "git-pull-request",
    tags: "tag",
    branches: "git-branch",
    commits: "git-commit",
    releases: "package",
  }[kind] ?? "list-tree";
}
