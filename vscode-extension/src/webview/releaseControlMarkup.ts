import type { DraftDocument, DraftItem, DraftRangeRequest, DraftSuggestionPreview, ModelSettings, RepositoryOverview } from "../client/types";

export interface ReleaseControlRenderState {
  document?: DraftDocument;
  overview?: RepositoryOverview;
  modelSettings: ModelSettings;
  range: DraftRangeRequest;
  busy: boolean;
  suggestions?: DraftSuggestionPreview;
  statusMessage?: string;
  errorMessage?: string;
}

export function buildReleaseControlHtml(state: ReleaseControlRenderState): string {
  const nonce = String(Date.now());
  const sections = state.document?.sections ?? [];
  const itemCount = state.document?.item_count ?? 0;
  const visibleItemCount = countVisibleItems(state.document);
  const releasedCount = state.overview?.releases.reduce((sum, release) => sum + release.itemCount, 0) ?? 0;
  const unpublishedCount = state.overview?.unpublishedCount ?? itemCount;
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>${styles()}</style>
</head>
<body>
  <main>
    <div class="toolbar">
      <span class="title">RELEASE CONTROL</span>
      <button class="icon" data-action="suggestAll" title="Suggest descriptions">✧</button>
      <button class="icon" data-action="preview" title="Preview release notes">P</button>
      <button class="icon" data-action="refresh" title="Refresh repository activity">R</button>
      <button class="icon" data-action="changeModel" title="Change AI model">...</button>
    </div>

    ${renderNotice(state)}
    ${renderSuggestions(state.suggestions)}

    <section class="section">
      <div class="section-title">DRAFT</div>
      <input id="version" value="${escapeHtml(state.range.version ?? "Unreleased")}" placeholder="Release version">
      <div class="range">
        <input id="base" value="${escapeHtml(state.range.base)}" placeholder="Base tag, branch, or commit">
        <input id="head" value="${escapeHtml(state.range.head ?? "HEAD")}" placeholder="Head">
      </div>
      <button class="primary" data-action="scan">${state.busy ? "Scanning..." : "Draft Notes Between Refs"}</button>
      <div class="split">
        <button data-action="applyChangelog">Apply CHANGELOG</button>
        <button data-action="applyRelease">Apply Release File</button>
      </div>
    </section>

    <section class="meta">
      <div><strong>${visibleItemCount}</strong><span>visible</span></div>
      <div><strong>${unpublishedCount}</strong><span>unreleased</span></div>
      <div><strong>${releasedCount}</strong><span>released</span></div>
    </section>

    <section class="section compact">
      <div class="section-title">MODEL</div>
      <button class="model" data-action="changeModel">
        <span>${escapeHtml(state.modelSettings.provider)}</span>
        <small>${escapeHtml(state.modelSettings.provider === "ollama" ? state.modelSettings.model : `fanout ${state.modelSettings.maxModelItems}`)}</small>
      </button>
    </section>

    <section class="section">
      <div class="section-title">CHANGES</div>
      ${sections.length ? sections.map(renderSection).join("") : `<p class="empty">Draft a release to see unreleased PRs, commits, and release-note items.</p>`}
    </section>

    <section class="section">
      <div class="section-title">SOURCES</div>
      ${renderSources(state.overview)}
    </section>
  </main>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const base = document.getElementById('base');
    const head = document.getElementById('head');
    const version = document.getElementById('version');
    function syncRange() {
      vscode.postMessage({ type: 'setRange', base: base.value, head: head.value, version: version.value });
    }
    [base, head, version].forEach((input) => input.addEventListener('input', syncRange));
    document.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      const action = button.dataset.action;
      syncRange();
      if (action === 'scan') vscode.postMessage({ type: 'scan' });
      if (action === 'suggestAll') vscode.postMessage({ type: 'suggestAll' });
      if (action === 'preview') vscode.postMessage({ type: 'preview' });
      if (action === 'refresh') vscode.postMessage({ type: 'refresh' });
      if (action === 'changeModel') vscode.postMessage({ type: 'changeModel' });
      if (action === 'applyChangelog') vscode.postMessage({ type: 'apply', target: 'changelog' });
      if (action === 'applyRelease') vscode.postMessage({ type: 'apply', target: 'github-release' });
      if (action === 'useBase') vscode.postMessage({ type: 'useRef', ref: button.dataset.ref, slot: 'base' });
      if (action === 'useHead') vscode.postMessage({ type: 'useRef', ref: button.dataset.ref, slot: 'head' });
      if (action === 'ready') vscode.postMessage({ type: 'mark', itemId: button.dataset.item, status: 'ready' });
      if (action === 'accept') vscode.postMessage({ type: 'mark', itemId: button.dataset.item, status: 'accepted' });
      if (action === 'hide') vscode.postMessage({ type: 'mark', itemId: button.dataset.item, status: 'hidden' });
      if (action === 'prompt') vscode.postMessage({ type: 'prompt', itemId: button.dataset.item });
      if (action === 'suggest') vscode.postMessage({ type: 'suggest', itemId: button.dataset.item });
      if (action === 'applySuggestion') vscode.postMessage({ type: 'applySuggestion', itemId: button.dataset.item, suggestionIndex: Number(button.dataset.index) });
      if (action === 'skipSuggestion') vscode.postMessage({ type: 'skipSuggestion', itemId: button.dataset.item });
    });
  </script>
</body>
</html>`;
}

function renderSuggestions(preview: DraftSuggestionPreview | undefined): string {
  if (!preview || !preview.items.length) {
    return "";
  }
  return `<section class="section suggestions">
    <div class="section-title">SUGGESTIONS</div>
    ${preview.items.map((item) => `<article class="suggestion">
      <div class="item-head">
        <span class="name">${escapeHtml(item.title)}</span>
        <span class="badge">${escapeHtml(item.provider)}</span>
      </div>
      <small>${escapeHtml(item.current_summary)}</small>
      <div class="variants">
        ${item.suggestions.map((suggestion, index) => `<button class="variant" data-action="applySuggestion" data-item="${escapeHtml(item.item_id)}" data-index="${index}">
          <span>${escapeHtml(suggestion)}</span>
        </button>`).join("")}
      </div>
      <button class="skip" data-action="skipSuggestion" data-item="${escapeHtml(item.item_id)}">Skip</button>
    </article>`).join("")}
  </section>`;
}

function countVisibleItems(document: DraftDocument | undefined): number {
  return document?.sections.reduce((sum, section) => sum + section.items.filter((item) => !item.hidden).length, 0) ?? 0;
}

function renderNotice(state: ReleaseControlRenderState): string {
  if (state.errorMessage) {
    return `<div class="notice error">${escapeHtml(state.errorMessage)}</div>`;
  }
  if (state.busy) {
    return `<div class="notice">Scanning ${escapeHtml(state.range.base)}..${escapeHtml(state.range.head ?? "HEAD")}...</div>`;
  }
  if (state.statusMessage) {
    return `<div class="notice">${escapeHtml(state.statusMessage)}</div>`;
  }
  return "";
}

function renderSection(section: { title: string; items: DraftItem[] }): string {
  return `<details open>
    <summary>${escapeHtml(section.title)} <span>${section.items.length}</span></summary>
    ${section.items.map(renderItem).join("")}
  </details>`;
}

function renderItem(item: DraftItem): string {
  const pr = item.pr_number ? `#${item.pr_number}` : item.commit_shas?.[0]?.slice(0, 7) ?? "change";
  const hiddenClass = item.hidden ? " hidden" : "";
  return `<article class="item${hiddenClass}">
    <div class="item-head">
      <span class="name">${escapeHtml(item.title)}</span>
      <button class="icon mini" data-action="suggest" data-item="${escapeHtml(item.id)}" title="Suggest descriptions">✧</button>
      <span class="badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
    </div>
    <p>${escapeHtml(item.summary)}</p>
    <div class="item-meta">${escapeHtml(pr)} | ${escapeHtml(item.area)} | ${escapeHtml(item.semver)} | ${item.confidence.toFixed(2)}</div>
    <div class="actions">
      <button data-action="ready" data-item="${escapeHtml(item.id)}">Ready</button>
      <button data-action="accept" data-item="${escapeHtml(item.id)}">Accept</button>
      <button data-action="prompt" data-item="${escapeHtml(item.id)}">Prompt</button>
      <button data-action="hide" data-item="${escapeHtml(item.id)}">Hide</button>
    </div>
  </article>`;
}

function renderSources(overview: RepositoryOverview | undefined): string {
  if (!overview) {
    return `<p class="empty">Refresh to load PRs, tags, branches, commits, and release associations.</p>`;
  }
  const prs = overview.commits
    .filter((commit) => commit.prNumber !== null)
    .slice(0, 6)
    .map((commit) => ({
      label: `PR #${commit.prNumber}`,
      ref: commit.sha,
      detail: commit.release ? `released in ${commit.release}` : commit.subject,
    }));
  const tags = overview.tags.slice(0, 6).map((tag) => ({ label: tag.name, ref: tag.name, detail: tag.detail ?? "tag" }));
  const branches = overview.branches.slice(0, 5).map((branch) => ({ label: branch.name, ref: branch.name, detail: branch.detail ?? "branch" }));
  const commits = overview.commits.slice(0, 8).map((commit) => ({
    label: commit.sha,
    ref: commit.sha,
    detail: commit.release ? `released in ${commit.release}` : commit.subject,
  }));
  const releases = overview.releases.slice(0, 6).map((release) => ({
    label: release.version,
    ref: release.version,
    detail: `${release.itemCount} associated item(s)`,
  }));
  const groups = [
    renderSourceGroup("PRs From Commits", prs),
    renderSourceGroup("Tags", tags),
    renderSourceGroup("Branches", branches),
    renderSourceGroup("Recent Commits", commits),
    renderSourceGroup("Associated Releases", releases),
  ].filter(Boolean);
  return groups.length ? groups.join("") : `<p class="empty">Repository refresh succeeded, but no refs or commits were returned.</p>`;
}

function renderSourceGroup(title: string, refs: { label: string; ref: string; detail: string }[]): string {
  if (!refs.length) {
    return "";
  }
  return `<details class="source-group" open>
    <summary>${escapeHtml(title)} <span>${refs.length}</span></summary>
    ${refs.map((ref) => `<div class="source-row">
      <button class="source-main" data-action="useBase" data-ref="${escapeHtml(ref.ref)}" title="Use as base ref">
        <span>${escapeHtml(ref.label)}</span>
        <small>${escapeHtml(ref.detail)}</small>
      </button>
      <button class="source-slot" data-action="useHead" data-ref="${escapeHtml(ref.ref)}" title="Use as head ref">HEAD</button>
    </div>`).join("")}
  </details>`;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function styles(): string {
  return `
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      font: var(--vscode-font-size) var(--vscode-font-family);
    }
    main { padding: 8px 6px 18px; }
    .toolbar { display: flex; align-items: center; gap: 4px; height: 24px; margin-bottom: 5px; }
    .title { flex: 1; color: var(--vscode-sideBarTitle-foreground); font-size: 11px; text-transform: uppercase; }
    button {
      border: 0;
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
      min-height: 24px;
      border-radius: 2px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { background: var(--vscode-button-secondaryHoverBackground); }
    .icon { width: 24px; min-height: 22px; background: transparent; color: var(--vscode-icon-foreground); }
    .mini { width: 20px; min-height: 18px; }
    .notice {
      margin: 0 0 8px;
      padding: 6px;
      border-left: 2px solid var(--vscode-focusBorder);
      background: var(--vscode-input-background);
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      line-height: 1.35;
    }
    .notice.error { border-left-color: var(--vscode-errorForeground); color: var(--vscode-errorForeground); }
    .section { margin-bottom: 10px; }
    .section-title { margin: 0 0 5px; color: var(--vscode-sideBarSectionHeader-foreground); font-weight: 600; font-size: 11px; text-transform: uppercase; }
    input {
      width: 100%;
      border: 1px solid var(--vscode-input-border, transparent);
      color: var(--vscode-input-foreground);
      background: var(--vscode-input-background);
      padding: 4px 6px;
      outline: none;
      font: inherit;
      border-radius: 2px;
      margin-bottom: 5px;
    }
    .range { display: grid; grid-template-columns: 1fr 76px; gap: 5px; }
    .primary { width: 100%; color: var(--vscode-button-foreground); background: var(--vscode-button-background); margin-bottom: 5px; }
    .primary:hover { background: var(--vscode-button-hoverBackground); }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
    .meta { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; margin: 10px 0; }
    .meta div { border-top: 1px solid var(--vscode-sideBarSectionHeader-border, transparent); padding-top: 5px; }
    .meta strong { display: block; font-size: 15px; }
    .meta span, small, .item-meta, .empty { color: var(--vscode-descriptionForeground); font-size: 11px; }
    .model { width: 100%; display: flex; justify-content: space-between; align-items: center; padding: 4px 6px; text-align: left; }
    details { margin-bottom: 4px; }
    summary { cursor: pointer; padding: 3px 0; font-weight: 600; text-transform: uppercase; font-size: 11px; }
    summary span { color: var(--vscode-descriptionForeground); margin-left: 3px; }
    .item { padding: 6px 0 7px; border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); }
    .item.hidden { opacity: .58; }
    .item-head { display: flex; gap: 6px; align-items: baseline; }
    .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .badge { color: var(--vscode-descriptionForeground); font-size: 10px; }
    .item p { margin: 3px 0; color: var(--vscode-descriptionForeground); line-height: 1.35; }
    .actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-top: 5px; }
    .actions button { min-height: 22px; font-size: 11px; }
    .suggestions { border-left: 2px solid var(--vscode-focusBorder); padding-left: 6px; }
    .suggestion { padding: 6px 0; border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); }
    .variants { display: grid; gap: 4px; margin-top: 5px; }
    .variant { min-height: 0; padding: 5px 6px; text-align: left; line-height: 1.35; }
    .variant span { display: block; white-space: normal; }
    .skip { margin-top: 4px; min-height: 20px; font-size: 11px; }
    .source-group { margin-bottom: 2px; }
    .source-row { display: grid; grid-template-columns: minmax(0, 1fr) 45px; gap: 4px; margin-bottom: 4px; }
    .source-main { overflow: hidden; display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 3px 6px; text-align: left; }
    .source-main span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .source-main small { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 45%; }
    .source-slot { padding: 0 4px; font-size: 10px; }
  `;
}
