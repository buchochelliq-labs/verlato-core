import * as vscode from "vscode";

import { DraftDocument } from "../client/types";

export class PreviewPanel {
  private panel: vscode.WebviewPanel | undefined;

  show(document: DraftDocument): void {
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel(
        "releasepilot.preview",
        "ReleasePilot Preview",
        vscode.ViewColumn.Beside,
        {
          enableScripts: false,
        },
      );
      this.panel.onDidDispose(() => {
        this.panel = undefined;
      });
    }

    this.panel.title = `ReleasePilot Preview: ${document.version}`;
    this.panel.webview.html = this.renderHtml(document);
    this.panel.reveal(vscode.ViewColumn.Beside);
  }

  private renderHtml(document: DraftDocument): string {
    const sections = document.sections
      .map(
        (section) => `
          <section>
            <h2>${escapeHtml(section.title)}</h2>
            <ul>
              ${section.items
                .map(
                  (item) => `
                    <li>
                      <strong>${escapeHtml(item.title)}</strong>
                      <div class="summary">${escapeHtml(item.summary)}</div>
                      ${item.edited_summary ? `<div class="original-summary">${escapeHtml(`Original generated summary: ${item.original_summary ?? item.summary}`)}</div>` : ""}
                      <small>${escapeHtml(`${item.kind} | ${item.area} | ${item.status} | confidence ${item.confidence.toFixed(2)}`)}</small>
                      <div class="source-row">
                        <span class="source-label">Summary source</span>
                        <span>${escapeHtml(item.summary_source ?? inferSummarySource(item))}</span>
                      </div>
                      <div class="source-row">
                        <span class="source-label">Classification source</span>
                        <span>${escapeHtml(item.classification_source ?? inferClassificationSource(item))}</span>
                      </div>
                      <div class="source-row">
                        <span class="source-label">Changed-file evidence</span>
                        <span>${escapeHtml(item.evidence_summary ?? "No changed-file evidence recorded")}</span>
                      </div>
                      <div>PR: ${renderPullRequestLink(item.pr_number, document.repository_url)}</div>
                      <div>Commits: ${renderCommitLinks(item.commit_shas ?? [], document.repository_url)}</div>
                      <div>${escapeHtml(`Signals: ${item.commit_signals.join(", ") || "none"}`)}</div>
                      <div class="files">Files: ${renderFileLinks(item.files, document.repository_url, item.commit_shas ?? [], item.head_ref)}</div>
                      <div>${escapeHtml(`Reviewer: ${item.reviewed_by || "unassigned"} | Reviewed: ${item.reviewed_at || "not yet"}`)}</div>
                    </li>
                  `,
                )
                .join("")}
            </ul>
          </section>
        `,
      )
      .join("");

    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
      body { font-family: var(--vscode-font-family); padding: 16px; color: var(--vscode-editor-foreground); }
      h1, h2 { font-weight: 600; }
      pre { white-space: pre-wrap; background: var(--vscode-textBlockQuote-background); padding: 12px; border-radius: 6px; }
      ul { padding-left: 20px; }
      li { margin-bottom: 16px; }
      small { opacity: 0.8; }
      section { margin-bottom: 24px; }
      li div { margin-top: 4px; }
      a { color: var(--vscode-textLink-foreground); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .summary { margin-top: 6px; }
      .original-summary { color: var(--vscode-descriptionForeground); }
      .source-row { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
      .source-label {
        color: var(--vscode-badge-foreground);
        background: var(--vscode-badge-background);
        border-radius: 3px;
        padding: 1px 6px;
        font-size: 0.9em;
      }
      .files a { margin-right: 8px; }
    </style>
  </head>
  <body>
    <h1>${escapeHtml(document.version)}</h1>
    <p>${escapeHtml(`${document.base_ref}..${document.head_ref} | ${document.item_count} items`)}</p>
    <p>${escapeHtml("Hidden items stay in the draft data but are excluded from rendered output.")}</p>
    ${sections}
    <h2>Rendered GitHub Release</h2>
    <pre>${escapeHtml(document.rendered["github-release"])}</pre>
  </body>
</html>`;
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderPullRequestLink(prNumber: number | null, repositoryUrl?: string | null): string {
  if (!prNumber) {
    return "No PR mapping";
  }
  const label = `PR #${prNumber}`;
  if (!repositoryUrl) {
    return escapeHtml(label);
  }
  return `<a href="${escapeHtml(`${repositoryUrl}/pull/${prNumber}`)}">${escapeHtml(label)}</a>`;
}

function renderCommitLinks(commitShas: string[], repositoryUrl?: string | null): string {
  if (commitShas.length === 0) {
    return "none";
  }
  return commitShas
    .map((sha) => {
      const label = shortSha(sha);
      if (!repositoryUrl) {
        return escapeHtml(label);
      }
      return `<a href="${escapeHtml(`${repositoryUrl}/commit/${encodeURIComponent(sha)}`)}">${escapeHtml(label)}</a>`;
    })
    .join(", ");
}

function renderFileLinks(files: string[], repositoryUrl: string | null | undefined, commitShas: string[], headRef: string): string {
  if (files.length === 0) {
    return "none";
  }
  const ref = commitShas[commitShas.length - 1] ?? headRef;
  return files.map((file) => renderFileLink(file, repositoryUrl, ref)).join(", ");
}

function renderFileLink(file: string, repositoryUrl: string | null | undefined, ref: string): string {
  if (!repositoryUrl) {
    return escapeHtml(file);
  }
  const encodedRef = encodeURIComponent(ref || "HEAD");
  const encodedPath = file.split(/[\\/]+/).map(encodeURIComponent).join("/");
  return `<a href="${escapeHtml(`${repositoryUrl}/blob/${encodedRef}/${encodedPath}`)}" title="${escapeHtml(`Open ${file} on remote`)}">${escapeHtml(file)}</a>`;
}

function shortSha(sha: string): string {
  return sha.length > 7 ? sha.slice(0, 7) : sha;
}

function inferSummarySource(item: { edited_summary: string | null; commit_signals: string[] }): string {
  const providerSignal = item.commit_signals.find((signal) => signal.startsWith("provider-summary:"));
  if (item.edited_summary) {
    if (providerSignal) {
      return `Human edited summary, originally generated by LLM (${providerSignal.split(":", 2)[1]})`;
    }
    return "Human edited summary, originally generated from deterministic diff evidence";
  }
  if (providerSignal) {
    return `LLM summary generated from diff evidence (${providerSignal.split(":", 2)[1]})`;
  }
  return "Deterministic summary generated from changed-file evidence";
}

function inferClassificationSource(item: { commit_signals: string[] }): string {
  const providerSignal = item.commit_signals.find((signal) => signal.startsWith("provider-classification:"));
  if (providerSignal) {
    return `LLM classification assist (${providerSignal.split(":", 2)[1]})`;
  }
  if (item.commit_signals.some((signal) => signal.startsWith("conventional:"))) {
    return "Deterministic classification from conventional commit signal";
  }
  if (item.commit_signals.some((signal) => signal.startsWith("label:"))) {
    return "Deterministic classification from labels and changed files";
  }
  return "Deterministic classification from changed-file evidence";
}
