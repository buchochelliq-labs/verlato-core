import * as vscode from "vscode";

import { DraftDocument, DraftItem, DraftSection } from "../client/types";

type ReleasePilotNode = StatusGroupNode | DraftItemNode | EmptyNode;
const STATUS_ORDER = ["needs-review", "ready", "accepted", "edited", "hidden"];

export class ReleasePilotTreeProvider implements vscode.TreeDataProvider<ReleasePilotNode> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<ReleasePilotNode | undefined>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;
  private document: DraftDocument | undefined;

  refresh(document?: DraftDocument): void {
    this.document = document;
    this.onDidChangeTreeDataEmitter.fire(undefined);
  }

  getTreeItem(element: ReleasePilotNode): vscode.TreeItem {
    return element;
  }

  getChildren(element?: ReleasePilotNode): ReleasePilotNode[] {
    if (!this.document) {
      return [new EmptyNode("Scan the repository to load release-note items.")];
    }
    if (!element) {
      return this.groupItemsByStatus(this.document.sections);
    }
    if (element instanceof StatusGroupNode) {
      return element.items.map((item) => new DraftItemNode(item));
    }
    return [];
  }

  getDocument(): DraftDocument | undefined {
    return this.document;
  }

  private groupItemsByStatus(sections: DraftSection[]): ReleasePilotNode[] {
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
}

class StatusGroupNode extends vscode.TreeItem {
  constructor(readonly status: string, readonly items: DraftItem[]) {
    super(`${toStatusLabel(status)} (${items.length})`, vscode.TreeItemCollapsibleState.Expanded);
    this.description = status;
    this.contextValue = "releasepilot.status";
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
    this.contextValue = "releasepilot.item";
  }
}

class EmptyNode extends vscode.TreeItem {
  constructor(label: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.contextValue = "releasepilot.empty";
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
