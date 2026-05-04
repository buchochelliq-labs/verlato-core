import type {
  CommandRequestSnapshot,
  DraftDocument,
  DraftItem,
  DraftRangeRequest,
  DraftSuggestionItem,
  DraftSuggestionPreview,
  ModelSettings,
  RepositoryOverview,
} from "../client/types";

export interface ReleaseControlRenderState {
  document?: DraftDocument;
  overview?: RepositoryOverview;
  modelSettings: ModelSettings;
  range: DraftRangeRequest;
  busy: boolean;
  suggestions?: DraftSuggestionPreview;
  requests?: CommandRequestSnapshot[];
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
      ${state.busy ? `<button class="stop" data-action="cancel" title="Stop current request">Stop</button>` : ""}
      <button class="icon" data-action="changeModel" title="Change AI model">...</button>
    </div>

    ${renderNotice(state)}
    ${renderRequests(state.requests ?? [])}
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
      <div class="tone-row">
        ${["release-note", "concise", "technical", "customer-facing"].map((tone) => `<button class="${state.modelSettings.suggestionTone === tone ? "selected" : ""}" data-action="setTone" data-tone="${tone}">${escapeHtml(tone)}</button>`).join("")}
      </div>
      <button class="telemetry-toggle" data-action="togglePromptPayloads">
        <span>Prompt payloads</span>
        <small>${state.modelSettings.includePromptPayloads ? "On" : "Off"}</small>
      </button>
      <button class="telemetry-toggle" data-action="toggleReasoningPayloads">
        <span>Reasoning payloads</span>
        <small>${state.modelSettings.includeReasoningPayloads ? "On" : "Off"}</small>
      </button>
      <button class="telemetry-toggle" data-action="togglePayloadRedaction">
        <span>Payload redaction</span>
        <small>${state.modelSettings.redactPayloads ? "On" : "Off"}</small>
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
      if (action === 'cancel') vscode.postMessage({ type: 'cancel' });
      if (action === 'cancelRequest') vscode.postMessage({ type: 'cancelRequest', requestId: Number(button.dataset.request) });
      if (action === 'changeModel') vscode.postMessage({ type: 'changeModel' });
      if (action === 'setTone') vscode.postMessage({ type: 'setTone', tone: button.dataset.tone });
      if (action === 'togglePromptPayloads') vscode.postMessage({ type: 'togglePromptPayloads' });
      if (action === 'toggleReasoningPayloads') vscode.postMessage({ type: 'toggleReasoningPayloads' });
      if (action === 'togglePayloadRedaction') vscode.postMessage({ type: 'togglePayloadRedaction' });
      if (action === 'copyText') {
        const block = button.closest('details')?.querySelector('pre');
        vscode.postMessage({ type: 'copyText', text: block?.textContent || '', label: button.dataset.copyLabel || 'payload' });
      }
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
      if (action === 'regenerateSuggestion') vscode.postMessage({ type: 'regenerateSuggestion', itemId: button.dataset.item });
      if (action === 'regenerateAll') vscode.postMessage({ type: 'regenerateAll' });
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
    <div class="section-title">SUGGESTIONS <button class="inline-action" data-action="regenerateAll">Regenerate</button></div>
    ${preview.items.map((item) => `<article class="suggestion">
      <div class="item-head">
        <span class="name">${escapeHtml(item.title)}</span>
        <span class="badge">${escapeHtml(item.provider)}</span>
        ${item.tone ? `<span class="badge">${escapeHtml(item.tone)}</span>` : ""}
      </div>
      ${renderSuggestionComparison(item)}
      ${renderProviderReasoning(item)}
      ${renderSuggestionEvidence(item)}
      <div class="variants">
        ${item.suggestions.map((suggestion, index) => `<button class="variant" data-action="applySuggestion" data-item="${escapeHtml(item.item_id)}" data-index="${index}">
          <span>${escapeHtml(suggestion)}</span>
          ${renderSimilarity(item, suggestion)}
        </button>`).join("")}
      </div>
      ${renderRejectedSuggestions(item)}
      <div class="suggestion-actions">
        <button data-action="regenerateSuggestion" data-item="${escapeHtml(item.item_id)}">Regenerate</button>
        <button class="skip" data-action="skipSuggestion" data-item="${escapeHtml(item.item_id)}">Skip</button>
      </div>
    </article>`).join("")}
  </section>`;
}

function renderSuggestionComparison(item: DraftSuggestionItem): string {
  const first = item.suggestion_details?.[0];
  const suggested = first?.comparison?.suggested ?? item.suggestions[0] ?? "";
  if (!suggested) {
    return `<small>${escapeHtml(item.current_summary)}</small>`;
  }
  return `<div class="compare">
    <div><strong>Current</strong><span>${escapeHtml(item.current_summary)}</span></div>
    <div><strong>Suggested</strong><span>${escapeHtml(suggested)}</span></div>
  </div>`;
}

function renderProviderReasoning(item: DraftSuggestionItem): string {
  const reasoning = item.provider_reasoning?.trim();
  if (!reasoning) {
    return "";
  }
  return `<details class="reasoning">
    <summary>Provider reasoning <span>${reasoning.length}</span><button class="copy" data-action="copyText" data-copy-label="provider reasoning">Copy</button></summary>
    <pre>${escapeHtml(reasoning)}</pre>
  </details>`;
}

function renderSuggestionEvidence(item: DraftSuggestionItem): string {
  const evidence = item.source_evidence ?? [];
  if (!evidence.length) {
    return "";
  }
  return `<details class="evidence">
    <summary>Evidence <span>${evidence.length}</span></summary>
    ${evidence.slice(0, 8).map((entry) => `<div class="evidence-row">
      <span>${escapeHtml(entry.kind)}</span>
      <strong>${escapeHtml(entry.label)}</strong>
      <small>${escapeHtml(entry.detail)}</small>
    </div>`).join("")}
  </details>`;
}

function renderSimilarity(item: DraftSuggestionItem, suggestion: string): string {
  const detail = item.suggestion_details?.find((entry) => entry.summary === suggestion);
  if (!detail) {
    return "";
  }
  return `<small>${Math.round(detail.similarity_to_current * 100)}% similar to current</small>`;
}

function renderRejectedSuggestions(item: DraftSuggestionItem): string {
  const rejected = item.rejected_candidates ?? [];
  if (!rejected.length) {
    return "";
  }
  return `<details class="rejected">
    <summary>Rejected <span>${rejected.length}</span></summary>
    ${rejected.slice(0, 5).map((entry) => `<div class="rejected-row">
      <strong>${escapeHtml(entry.reason)}</strong>
      <span>${Math.round(entry.similarity_to_current * 100)}%</span>
      <small>${escapeHtml(entry.summary)}</small>
    </div>`).join("")}
  </details>`;
}

function renderRequests(requests: CommandRequestSnapshot[]): string {
  if (!requests.length) {
    return "";
  }
  const runningCount = requests.filter((request) => request.status === "running").length;
  return `<section class="section requests">
    <div class="section-title">REQUESTS ${runningCount ? `<span>${runningCount} running</span>` : ""}</div>
    ${requests.slice(0, 8).map(renderRequest).join("")}
  </section>`;
}

function renderRequest(request: CommandRequestSnapshot): string {
  const command = [request.command, ...request.args].join(" ");
  const output = request.stderr || request.stdout || request.error || "";
  const activity = request.activity?.length ? request.activity : parseActivityFromOutput(output);
  return `<details class="request" ${request.status === "running" ? "open" : ""}>
    <summary>
      <span class="request-label">${escapeHtml(request.label || request.command)}</span>
      <span class="badge ${escapeHtml(request.status)}">${escapeHtml(request.status)}</span>
      ${request.status === "running" ? `<button class="request-stop" data-action="cancelRequest" data-request="${request.id}" title="Stop this request">Stop</button>` : ""}
    </summary>
    <code>${escapeHtml(command)}</code>
    ${activity.length ? renderActivity(activity) : `<small>No model activity yet.</small>`}
    ${output ? `<details class="raw"><summary>Raw output</summary><pre>${escapeHtml(output)}</pre></details>` : ""}
  </details>`;
}

function renderActivity(activity: NonNullable<CommandRequestSnapshot["activity"]>): string {
  return `<ol class="activity">
    ${activity.slice(-20).map((entry) => `<li class="${escapeHtml(entry.category)}">
      <time>${escapeHtml(formatActivityTime(entry.receivedAt))}</time>
      <span>
        ${escapeHtml(formatActivityMessage(entry))}
        ${entry.payload ? `<details class="payload"><summary>${escapeHtml(formatPayloadLabel(entry))}<button class="copy" data-action="copyText" data-copy-label="${escapeHtml(formatPayloadLabel(entry).toLowerCase())}">Copy</button></summary><pre>${escapeHtml(entry.payload)}</pre></details>` : ""}
      </span>
    </li>`).join("")}
  </ol>`;
}

function parseActivityFromOutput(output: string): NonNullable<CommandRequestSnapshot["activity"]> {
  const entries: NonNullable<CommandRequestSnapshot["activity"]> = [];
  output.split(/\r?\n/).forEach((line, index) => {
    const trimmed = line.trim();
    if (trimmed) {
      const parsed = parseModelActivityLine(trimmed);
      entries.push({
        id: index,
        stream: "stderr",
        category: trimmed.startsWith("[releasepilot:model]") ? "model" : "process",
        ...parsed,
        receivedAt: "",
      });
    }
  });
  return entries;
}

function formatActivityMessage(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  if (entry.event === "prompt_budget") {
    return formatPromptBudget(entry);
  }
  if (entry.event === "stream_chunk") {
    const kind = streamKindLabel(String(entry.attributes?.kind ?? "model"));
    const chunks = typeof entry.attributes?.chunks === "number" ? entry.attributes.chunks : 1;
    return `${capitalize(kind)} streaming... ${chunks} chunk${chunks === 1 ? "" : "s"}`;
  }
  if (entry.event && entry.payload) {
    return entry.event.replace(/_/g, " ");
  }
  return entry.message.replace(/^\[releasepilot:model\]\s*/, "");
}

function formatPromptBudget(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  const attrs = entry.attributes ?? {};
  return [
    "prompt budget",
    `files=${numberAttr(attrs, "files_included")}/${numberAttr(attrs, "files_total")}`,
    `commits=${numberAttr(attrs, "commits_included")}/${numberAttr(attrs, "commits_total")}`,
    `hunks=${numberAttr(attrs, "hunks_included")}/${numberAttr(attrs, "hunks_total")}`,
    `chars=${numberAttr(attrs, "prompt_chars")}`,
  ].join(" ");
}

function numberAttr(attrs: Record<string, unknown>, key: string): number {
  return typeof attrs[key] === "number" ? attrs[key] : 0;
}

function formatPayloadLabel(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  if (entry.event === "stream_chunk") {
    const kind = streamKindLabel(String(entry.attributes?.kind ?? "stream"));
    const chars = typeof entry.attributes?.chars === "number" ? ` ${entry.attributes.chars} chars` : "";
    return `${capitalize(kind)} stream${chars}${entry.attributes?.redacted ? " redacted" : ""}`;
  }
  const chars = typeof entry.attributes?.chars === "number" ? ` ${entry.attributes.chars} chars` : "";
  return `Payload${chars}${entry.attributes?.redacted ? " redacted" : ""}`;
}

function parseModelActivityLine(line: string): {
  message: string;
  event?: string;
  attributes?: Record<string, unknown>;
  payload?: string;
} {
  const jsonPrefix = "[releasepilot:model] json ";
  if (!line.startsWith(jsonPrefix)) {
    const streamChunk = parseStreamChunkLine(line);
    if (streamChunk) {
      return streamChunk;
    }
    return { message: line };
  }
  try {
    const parsed = JSON.parse(line.slice(jsonPrefix.length)) as {
      event?: unknown;
      message?: unknown;
      attributes?: unknown;
      payload?: unknown;
    };
    return {
      message: typeof parsed.message === "string" ? parsed.message : line,
      event: typeof parsed.event === "string" ? parsed.event : undefined,
      attributes: isRecord(parsed.attributes) ? parsed.attributes : undefined,
      payload: typeof parsed.payload === "string" ? parsed.payload : undefined,
    };
  } catch {
    return { message: line };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseStreamChunkLine(line: string): {
  message: string;
  event: "stream_chunk";
  attributes: Record<string, unknown>;
  payload: string;
} | undefined {
  const match = line.match(/^\[releasepilot:model\]\s+stream-chunk\s+provider=(\S+)\s+task=(\S+)\s+kind=(\S+)\s+chars=(\d+)(?:\s+redacted=(\S+))?\s+text=(.*)$/);
  if (!match) {
    return undefined;
  }
  const [, provider, task, kind, chars, redacted, text] = match;
  return {
    message: line,
    event: "stream_chunk",
    attributes: {
      provider,
      task,
      kind,
      chars: Number(chars),
      chunks: 1,
      redacted: redacted === "true",
    },
    payload: text,
  };
}

function capitalize(value: string): string {
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : value;
}

function streamKindLabel(kind: string): string {
  return kind === "content" ? "response" : kind;
}

function formatActivityTime(value: string): string {
  if (!value) {
    return "--:--:--";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "--:--:--";
  }
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
    .stop {
      min-height: 22px;
      padding: 0 7px;
      color: var(--vscode-button-foreground);
      background: var(--vscode-errorForeground);
      font-size: 11px;
    }
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
    .section-title span { float: right; color: var(--vscode-descriptionForeground); font-weight: 400; }
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
    .model, .telemetry-toggle { width: 100%; display: flex; justify-content: space-between; align-items: center; padding: 4px 6px; text-align: left; }
    .telemetry-toggle { margin-top: 4px; }
    .tone-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; margin-top: 5px; }
    .tone-row button { min-height: 22px; padding: 3px 4px; font-size: 10px; overflow-wrap: anywhere; }
    .tone-row .selected { outline: 1px solid var(--vscode-focusBorder); }
    details { margin-bottom: 4px; }
    summary { cursor: pointer; padding: 3px 0; font-weight: 600; text-transform: uppercase; font-size: 11px; }
    summary span { color: var(--vscode-descriptionForeground); margin-left: 3px; }
    summary .copy {
      float: right;
      min-height: 16px;
      padding: 0 5px;
      font-size: 10px;
      text-transform: none;
    }
    .inline-action {
      float: right;
      min-height: 18px;
      padding: 1px 5px;
      font-size: 10px;
      text-transform: none;
    }
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
    .requests { border-left: 2px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.28)); padding-left: 6px; }
    .request { border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); padding: 4px 0; }
    .request summary { display: flex; align-items: center; gap: 5px; text-transform: none; }
    .request-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .request-stop { min-height: 18px; padding: 0 5px; color: var(--vscode-button-foreground); background: var(--vscode-errorForeground); font-size: 10px; }
    .request code { display: block; color: var(--vscode-descriptionForeground); white-space: pre-wrap; word-break: break-word; font-size: 10px; margin: 2px 0; }
    .activity {
      list-style: none;
      display: grid;
      gap: 3px;
      margin: 5px 0 0;
      padding: 0;
      font-size: 10px;
      line-height: 1.35;
    }
    .activity li {
      display: grid;
      grid-template-columns: 54px minmax(0, 1fr);
      gap: 5px;
      padding: 3px 4px;
      border-left: 2px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.24));
      background: var(--vscode-input-background);
    }
    .activity li.model { border-left-color: var(--vscode-focusBorder); }
    .activity time { color: var(--vscode-descriptionForeground); font-variant-numeric: tabular-nums; }
    .activity span { min-width: 0; overflow-wrap: anywhere; }
    .payload { margin-top: 3px; }
    .payload summary { color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 400; text-transform: none; }
    .raw { margin-top: 4px; }
    .raw summary { color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 400; text-transform: none; }
    .request pre {
      max-height: 130px;
      overflow: auto;
      margin: 4px 0 0;
      padding: 5px;
      color: var(--vscode-editor-foreground);
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-input-border, transparent);
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 10px;
      line-height: 1.35;
    }
    .variants { display: grid; gap: 4px; margin-top: 5px; }
    .variant { min-height: 0; padding: 5px 6px; text-align: left; line-height: 1.35; }
    .variant span { display: block; white-space: normal; }
    .variant small { display: block; margin-top: 2px; }
    .compare { display: grid; gap: 4px; margin: 5px 0; }
    .compare div {
      display: grid;
      gap: 2px;
      padding: 4px 5px;
      background: var(--vscode-input-background);
      border-left: 2px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.24));
    }
    .compare strong { color: var(--vscode-descriptionForeground); font-size: 10px; text-transform: uppercase; }
    .compare span { line-height: 1.35; overflow-wrap: anywhere; }
    .evidence, .rejected, .reasoning { margin-top: 4px; }
    .evidence summary, .rejected summary, .reasoning summary { color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 400; text-transform: none; }
    .reasoning pre, .payload pre {
      max-height: 180px;
      overflow: auto;
      margin: 4px 0 0;
      padding: 5px;
      color: var(--vscode-editor-foreground);
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-input-border, transparent);
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 10px;
      line-height: 1.35;
    }
    .evidence-row, .rejected-row {
      display: grid;
      grid-template-columns: 52px minmax(0, 1fr);
      gap: 2px 5px;
      padding: 3px 0;
      border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.14));
      font-size: 10px;
    }
    .evidence-row span, .rejected-row span { color: var(--vscode-descriptionForeground); }
    .evidence-row strong, .rejected-row strong { min-width: 0; overflow-wrap: anywhere; font-weight: 600; }
    .evidence-row small, .rejected-row small { grid-column: 2; min-width: 0; overflow-wrap: anywhere; }
    .skip { margin-top: 4px; min-height: 20px; font-size: 11px; }
    .suggestion-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-top: 4px; }
    .suggestion-actions button { min-height: 20px; font-size: 11px; }
    .source-group { margin-bottom: 2px; }
    .source-row { display: grid; grid-template-columns: minmax(0, 1fr) 45px; gap: 4px; margin-bottom: 4px; }
    .source-main { overflow: hidden; display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 3px 6px; text-align: left; }
    .source-main span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .source-main small { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 45%; }
    .source-slot { padding: 0 4px; font-size: 10px; }
  `;
}
