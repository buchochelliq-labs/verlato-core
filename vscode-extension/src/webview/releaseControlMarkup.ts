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

export interface ReleaseActivityRenderState {
  requests: CommandRequestSnapshot[];
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
      ${renderModelBadge(state.modelSettings)}
    </div>

    ${renderNotice(state)}
    <div class="layout">
      <aside class="operations">
        ${renderDraftPanel(state)}
        ${renderModelPanel(state)}
      </aside>
      <div class="workspace">
        <section class="meta" aria-label="Release item filters">
          <button class="meta-card" data-action="filterItems" data-filter="visible" title="Show visible draft items">
            <strong>${visibleItemCount}</strong><span>visible</span>
          </button>
          <button class="meta-card" data-action="filterItems" data-filter="unreleased" title="Show unreleased draft data">
            <strong>${unpublishedCount}</strong><span>unreleased</span>
          </button>
          <button class="meta-card" data-action="filterItems" data-filter="released" title="Show released source data">
            <strong>${releasedCount}</strong><span>released</span>
          </button>
        </section>
        ${renderReleasesPanel(state)}
        <section class="section changes-panel" id="changes-panel">
          <div class="section-title">CHANGES</div>
          ${sections.length ? sections.map(renderSection).join("") : `<p class="empty">Draft a release to see unreleased PRs, commits, and release-note items.</p>`}
        </section>
        <section class="section tree-section sources-panel" id="sources-panel">
          <div class="section-title">SOURCES <button class="inline-action" data-action="openSources">Open View</button></div>
          ${renderSources(state.overview)}
        </section>
      </div>
    </div>
  </main>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    ${scrollStateScript()}
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
      if (action === 'filterItems') {
        const filter = button.dataset.filter || 'all';
        document.body.dataset.filter = filter;
        document.querySelectorAll('.meta-card').forEach((card) => card.classList.toggle('active', card.dataset.filter === filter));
        const target = filter === 'released' || filter === 'unreleased' ? document.getElementById('releases-panel') : document.getElementById('changes-panel');
        target?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        vscode.setState({ ...getStoredState(), releaseFilter: filter });
      }
      if (action === 'scan') vscode.postMessage({ type: 'scan' });
      if (action === 'suggestAll') vscode.postMessage({ type: 'suggestAll' });
      if (action === 'preview') vscode.postMessage({ type: 'preview' });
      if (action === 'refresh') vscode.postMessage({ type: 'refresh' });
      if (action === 'openSources') vscode.postMessage({ type: 'openSources' });
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
      if (action === 'feedbackPositive') vscode.postMessage({ type: 'feedbackPositive', itemId: button.dataset.item });
      if (action === 'feedbackNegative') vscode.postMessage({ type: 'feedbackNegative', itemId: button.dataset.item });
    });
    const restoredFilter = getStoredState().releaseFilter;
    if (restoredFilter) {
      document.body.dataset.filter = restoredFilter;
      document.querySelectorAll('.meta-card').forEach((card) => card.classList.toggle('active', card.dataset.filter === restoredFilter));
    }
  </script>
</body>
</html>`;
}

export function buildReleaseActivityHtml(state: ReleaseActivityRenderState): string {
  const nonce = String(Date.now());
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
      <span class="title">ACTIVITY</span>
    </div>
    ${state.requests.length ? renderRequests(state.requests, true) : `<p class="empty">Run a ReleasePilot command to see request and model activity.</p>`}
  </main>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    ${scrollStateScript()}
    document.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      const action = button.dataset.action;
      if (action === 'cancelRequest') vscode.postMessage({ type: 'cancelRequest', requestId: Number(button.dataset.request) });
      if (action === 'copyText') {
        const block = button.closest('details')?.querySelector('pre');
        vscode.postMessage({ type: 'copyText', text: block?.textContent || '', label: button.dataset.copyLabel || 'payload' });
      }
    });
  </script>
</body>
</html>`;
}

function renderModelBadge(settings: ModelSettings): string {
  const detail = modelDetail(settings);
  return `<button class="model-badge" data-action="changeModel" title="${escapeHtml(detail)}" data-tooltip="${escapeHtml(detail)}">
    <span class="model-mark">AI</span>
    <span class="model-name">${escapeHtml(modelBadgeLabel(settings))}</span>
  </button>`;
}

function renderDraftPanel(state: ReleaseControlRenderState): string {
  return `<details class="section tree-section control-panel" open>
    <summary class="section-title">DRAFT <span>${escapeHtml(state.range.version ?? "Unreleased")}</span></summary>
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
  </details>`;
}

function renderModelPanel(state: ReleaseControlRenderState): string {
  return `<details class="section tree-section control-panel">
    <summary class="section-title">MODEL <span>${escapeHtml(state.modelSettings.provider)}</span></summary>
    <button class="model" data-action="changeModel" title="${escapeHtml(modelDetail(state.modelSettings))}">
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
  </details>`;
}

function modelBadgeLabel(settings: ModelSettings): string {
  if (settings.provider === "ollama") {
    return settings.model || "Ollama";
  }
  if (settings.provider === "github-copilot") {
    return "GitHub model";
  }
  if (settings.provider === "configured") {
    return "Configured";
  }
  return settings.provider;
}

function modelDetail(settings: ModelSettings): string {
  const providerDetail = settings.provider === "ollama"
    ? `${settings.model} at ${settings.endpoint}`
    : `fanout guard ${settings.maxModelItems}`;
  return [
    `Provider: ${settings.provider}`,
    `Model: ${providerDetail}`,
    `Tone: ${settings.suggestionTone}`,
    `Suggestions: ${settings.suggestionCount}`,
    `Streaming: ${settings.ollamaStreamResponses ? "on" : "off"}`,
    `Prompt payloads: ${settings.includePromptPayloads ? "on" : "off"}`,
    `Reasoning payloads: ${settings.includeReasoningPayloads ? "on" : "off"}`,
    `Redaction: ${settings.redactPayloads ? "on" : "off"}`,
  ].join("\n");
}

function renderSuggestions(preview: DraftSuggestionPreview | undefined): string {
  if (!preview || !preview.items.length) {
    return "";
  }
  return `<section class="sub-panel suggestions">
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
        <button data-action="feedbackPositive" data-item="${escapeHtml(item.item_id)}">Useful</button>
        <button data-action="feedbackNegative" data-item="${escapeHtml(item.item_id)}">Not useful</button>
      </div>
    </article>`).join("")}
  </section>`;
}

function renderReleasesPanel(state: ReleaseControlRenderState): string {
  const released = state.overview?.releases ?? [];
  const releaseCount = released.length + 1;
  return `<section class="section tree-section releases-panel" id="releases-panel">
    <div class="section-title">RELEASES <span>${releaseCount}</span></div>
    ${renderUnreleasedRelease(state.document, state.suggestions, state.overview)}
    ${renderReleasedReleases(state.overview)}
  </section>`;
}

function renderUnreleasedRelease(
  document: DraftDocument | undefined,
  suggestions: DraftSuggestionPreview | undefined,
  overview: RepositoryOverview | undefined,
): string {
  const version = document?.version ?? "Unreleased";
  const items = document?.sections.flatMap((section) => section.items) ?? [];
  const visibleItems = items.filter((item) => !item.hidden);
  const commits = overview?.commits.filter((commit) => commit.release === null) ?? [];
  const suggestionCount = suggestions?.items.reduce((sum, item) => sum + item.suggestions.length, 0) ?? 0;
  return `<details class="release-card" open data-release-state="unreleased">
    <summary>
      <span>${escapeHtml(version)}</span>
      <small>${visibleItems.length} visible item(s) | ${commits.length} unreleased commit(s) | ${suggestionCount} suggestion(s)</small>
    </summary>
    <div class="sub-panel release-note-panel">
      <div class="sub-panel-title">Release Note</div>
      ${renderDraftReleaseNote(document)}
    </div>
    ${renderSuggestions(suggestions)}
    <div class="sub-panel">
      <div class="sub-panel-title">Draft Items</div>
      ${visibleItems.length ? visibleItems.map(renderReleaseDraftRow).join("") : `<p class="empty">Draft a release to review unreleased release-note items.</p>`}
    </div>
    ${renderReleaseCommits(commits, "Unreleased commits")}
  </details>`;
}

function renderReleasedReleases(overview: RepositoryOverview | undefined): string {
  if (!overview) {
    return `<div class="sub-panel"><p class="empty">Refresh repository activity to load released versions and their commits.</p></div>`;
  }
  if (!overview.releases.length) {
    return `<div class="sub-panel"><p class="empty">No published release associations were found in local ReleasePilot state.</p></div>`;
  }
  return overview.releases.map((release) => {
    const commits = overview.commits.filter((commit) => commit.release === release.version);
    return `<details class="release-card" data-release-state="released">
      <summary>
        <span>${escapeHtml(release.version)}</span>
        <small>${release.itemCount} release-note item(s) | ${commits.length} commit(s)</small>
      </summary>
      <div class="sub-panel release-note-panel">
        <div class="sub-panel-title">Release Note</div>
        ${renderPublishedReleaseNotes(release.items ?? [])}
      </div>
      ${renderReleaseCommits(commits, "Commits in release")}
    </details>`;
  }).join("");
}

function renderDraftReleaseNote(document: DraftDocument | undefined): string {
  const rendered = document?.rendered["github-release"] ?? document?.rendered.changelog;
  if (rendered?.trim()) {
    return `<pre class="release-note" tabindex="0" data-scroll-key="release-note-unreleased">${escapeHtml(rendered.trim())}</pre>`;
  }
  const items = document?.sections.flatMap((section) => section.items).filter((item) => !item.hidden) ?? [];
  if (!items.length) {
    return `<p class="empty">Draft a release to generate the unreleased release note preview.</p>`;
  }
  return `<div class="release-note-list">${items.map((item) => `<p><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.summary)}</span></p>`).join("")}</div>`;
}

function renderReleaseDraftRow(item: DraftItem): string {
  const suggestionSource = item.summary_source === "model-suggestion" || item.summary_provenance?.source === "model-suggestion"
    ? "model suggestion"
    : item.summary_source ?? "draft";
  return `<div class="release-row">
    <span>${escapeHtml(item.title)}</span>
    <small>${escapeHtml(item.status)} | ${escapeHtml(suggestionSource)}</small>
  </div>`;
}

function renderPublishedReleaseNotes(items: { id: string; title: string; summary: string; status?: string; source?: string }[]): string {
  if (!items.length) {
    return `<p class="empty">No release-note entries are available for this version.</p>`;
  }
  return `<div class="release-note-list">${items.map((item) => `<p>
    <strong>${escapeHtml(item.title)}</strong>
    <span>${escapeHtml(item.summary)}</span>
    <small>${escapeHtml([item.status, item.source].filter(Boolean).join(" | ") || item.id)}</small>
  </p>`).join("")}</div>`;
}

function renderReleaseCommits(commits: { sha: string; subject: string; prNumber: number | null }[], title: string): string {
  return `<div class="sub-panel">
    <div class="sub-panel-title">${escapeHtml(title)} <span>${commits.length}</span></div>
    ${commits.length ? commits.slice(0, 12).map((commit) => `<div class="source-row release-commit">
      <button class="source-main" data-action="useBase" data-ref="${escapeHtml(commit.sha)}" title="Use as base ref">
        <span>${escapeHtml(commit.prNumber ? `PR #${commit.prNumber}` : commit.sha)}</span>
        <small>${escapeHtml(commit.subject)}</small>
      </button>
      <button class="source-slot" data-action="useHead" data-ref="${escapeHtml(commit.sha)}" title="Use as head ref">HEAD</button>
    </div>`).join("") : `<p class="empty">No recent commits are currently mapped to this release.</p>`}
  </div>`;
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
  const specificity = typeof detail.specificity_score === "number"
    ? ` · ${Math.round(detail.specificity_score * 100)}% specific`
    : "";
  return `<small>${Math.round(detail.similarity_to_current * 100)}% similar to current${specificity}</small>`;
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
      <span>${Math.round((entry.specificity_score ?? entry.similarity_to_current) * 100)}%</span>
      <small>${escapeHtml(entry.summary)}</small>
    </div>`).join("")}
  </details>`;
}

function renderRequests(requests: CommandRequestSnapshot[], openWhenIdle = false): string {
  if (!requests.length) {
    return "";
  }
  const runningCount = requests.filter((request) => request.status === "running").length;
  return `<details class="section tree-section requests" ${runningCount || openWhenIdle ? "open" : ""}>
    <summary class="section-title">REQUESTS <span>${runningCount ? `${runningCount} running` : `${requests.length} recent`}</span></summary>
    ${requests.slice(0, 8).map(renderRequest).join("")}
  </details>`;
}

function renderRequest(request: CommandRequestSnapshot): string {
  const command = [request.command, ...request.args].join(" ");
  const output = request.stderr || request.stdout || request.error || "";
  const activity = request.activity?.length ? request.activity : parseActivityFromOutput(output);
  return `<details class="request" ${request.status === "running" ? "open" : ""}>
    <summary>
      <span class="request-label">${escapeHtml(request.label || request.command)}</span>
      ${renderRequestStatus(request.status)}
      ${request.status === "running" ? `<button class="request-stop" data-action="cancelRequest" data-request="${request.id}" title="Stop this request">Stop</button>` : ""}
    </summary>
    <div class="sub-panel command-panel"><code>${escapeHtml(command)}</code></div>
    ${activity.length ? renderActivity(request.id, activity) : `<div class="sub-panel"><small>No model activity yet.</small></div>`}
    ${output ? `<details class="sub-panel raw"><summary>Raw output</summary><pre tabindex="0" data-scroll-key="request-${request.id}-raw">${escapeHtml(output)}</pre></details>` : ""}
  </details>`;
}

function renderRequestStatus(status: CommandRequestSnapshot["status"]): string {
  const spinner = status === "running" ? `<span class="spinner" aria-hidden="true"></span>` : "";
  return `<span class="badge request-status ${escapeHtml(status)}">${spinner}<span>${escapeHtml(status)}</span></span>`;
}

function renderActivity(requestId: number, activity: NonNullable<CommandRequestSnapshot["activity"]>): string {
  const latest = activity.slice(-20).reverse();
  return `<section class="sub-panel activity-panel">
    <div class="sub-panel-title">Latest activity</div>
    <ol class="activity">
    ${latest.map((entry) => `<li class="${escapeHtml(entry.category)}">
      <time>${escapeHtml(formatActivityTime(entry.receivedAt))}</time>
      <span>
        ${escapeHtml(formatActivityMessage(entry))}
        ${entry.payload ? renderActivityPayload(requestId, entry) : ""}
      </span>
    </li>`).join("")}
    </ol>
  </section>`;
}

function renderActivityPayload(requestId: number, entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  const label = formatPayloadLabel(entry);
  const open = entry.event === "stream_chunk" ? " open" : "";
  const streamClass = entry.event === "stream_chunk" ? " stream-payload" : "";
  const scrollKey = activityPayloadScrollKey(requestId, entry);
  const scrollClass = entry.event === "stream_chunk" ? " class=\"stream-scroll\"" : "";
  return `<details class="payload${streamClass}"${open}><summary>${escapeHtml(label)}<button class="copy" data-action="copyText" data-copy-label="${escapeHtml(label.toLowerCase())}">Copy</button></summary><pre${scrollClass} tabindex="0" data-scroll-key="${escapeHtml(scrollKey)}">${escapeHtml(entry.payload ?? "")}</pre></details>`;
}

function activityPayloadScrollKey(requestId: number, entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  if (entry.event === "stream_chunk") {
    const task = String(entry.attributes?.task ?? "task");
    const kind = String(entry.attributes?.kind ?? "stream");
    return `request-${requestId}-stream-${task}-${kind}`;
  }
  return `request-${requestId}-payload-${entry.id}`;
}

function scrollStateScript(): string {
  return `
    const SCROLL_THRESHOLD = 12;
    let restoringScroll = false;
    function getStoredState() {
      return vscode.getState() || {};
    }
    function getScrollState() {
      const state = getStoredState();
      return state.scrollState || { windowY: 0, panes: {} };
    }
    function isPinnedToBottom(element) {
      return element.scrollHeight - element.scrollTop - element.clientHeight <= SCROLL_THRESHOLD;
    }
    function saveScrollState() {
      if (restoringScroll) return;
      const scrollState = { windowY: window.scrollY, panes: {} };
      document.querySelectorAll('[data-scroll-key]').forEach((element) => {
        const key = element.dataset.scrollKey;
        if (!key) return;
        scrollState.panes[key] = {
          top: element.scrollTop,
          pinned: isPinnedToBottom(element),
        };
      });
      vscode.setState({ ...getStoredState(), scrollState });
    }
    function restoreScrollState() {
      restoringScroll = true;
      const scrollState = getScrollState();
      document.querySelectorAll('[data-scroll-key]').forEach((element) => {
        const key = element.dataset.scrollKey;
        const saved = key ? scrollState.panes[key] : undefined;
        if (element.classList.contains('stream-scroll') && (!saved || saved.pinned)) {
          element.scrollTop = element.scrollHeight;
          return;
        }
        if (saved) {
          element.scrollTop = saved.top;
        }
      });
      if (typeof scrollState.windowY === 'number') {
        window.scrollTo(0, scrollState.windowY);
      }
      requestAnimationFrame(() => {
        restoringScroll = false;
      });
    }
    window.addEventListener('scroll', saveScrollState, { passive: true });
    document.addEventListener('scroll', saveScrollState, { capture: true, passive: true });
    window.addEventListener('beforeunload', saveScrollState);
    requestAnimationFrame(restoreScrollState);
  `;
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
  return mergeStreamActivity(entries);
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

function mergeStreamActivity(entries: CommandRequestSnapshot["activity"]): NonNullable<CommandRequestSnapshot["activity"]> {
  const merged: NonNullable<CommandRequestSnapshot["activity"]> = [];
  for (const entry of entries ?? []) {
    if (entry.event !== "stream_chunk") {
      merged.push(entry);
      continue;
    }
    const kind = entry.attributes?.kind;
    const task = entry.attributes?.task;
    const prior = [...merged].reverse().find(
      (candidate) =>
        candidate.event === "stream_chunk" &&
        candidate.attributes?.kind === kind &&
        candidate.attributes?.task === task,
    );
    if (!prior) {
      merged.push(entry);
      continue;
    }
    const priorChars = typeof prior.attributes?.chars === "number" ? prior.attributes.chars : 0;
    const nextChars = typeof entry.attributes?.chars === "number" ? entry.attributes.chars : 0;
    const priorChunks = typeof prior.attributes?.chunks === "number" ? prior.attributes.chunks : 1;
    const nextChunks = typeof entry.attributes?.chunks === "number" ? entry.attributes.chunks : 1;
    prior.attributes = {
      ...(prior.attributes ?? {}),
      chars: priorChars + nextChars,
      chunks: priorChunks + nextChunks,
      redacted: Boolean(prior.attributes?.redacted) || Boolean(entry.attributes?.redacted),
    };
    prior.payload = trimStreamPayload(`${prior.payload ?? ""}${entry.payload ?? ""}`);
    prior.message = `[releasepilot:model] stream ${String(kind)} chunks=${priorChunks + nextChunks} chars=${priorChars + nextChars}`;
    prior.receivedAt = entry.receivedAt;
  }
  return merged;
}

function trimStreamPayload(value: string): string {
  return value.length > 6000 ? value.slice(value.length - 6000) : value;
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
  const visibility = item.hidden ? "hidden" : "visible";
  return `<article class="item${hiddenClass}" data-visibility="${visibility}">
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
  const groups = [
    renderSourceGroup("PRs From Commits", prs),
    renderSourceGroup("Tags", tags),
    renderSourceGroup("Branches", branches),
    renderSourceGroup("Recent Commits", commits),
  ].filter(Boolean);
  return groups.length ? groups.join("") : `<p class="empty">Repository refresh succeeded, but no refs or commits were returned.</p>`;
}

function renderSourceGroup(title: string, refs: { label: string; ref: string; detail: string }[]): string {
  if (!refs.length) {
    return "";
  }
  const kind = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `<details class="source-group" open>
    <summary data-source-kind="${escapeHtml(kind)}">${escapeHtml(title)} <span>${refs.length}</span></summary>
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
    .layout { display: grid; gap: 8px; }
    .operations { display: grid; gap: 6px; align-content: start; }
    .workspace { min-width: 0; }
    @media (min-width: 620px) {
      main { padding: 10px 10px 20px; }
      .layout { grid-template-columns: minmax(220px, 285px) minmax(0, 1fr); align-items: start; }
      .operations { position: sticky; top: 8px; }
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 4px;
      min-height: 28px;
      margin-bottom: 8px;
      padding: 3px 4px;
      border-bottom: 1px solid color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 45%, transparent);
      background: linear-gradient(90deg, color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 15%, transparent), transparent);
    }
    .title { flex: 1; color: var(--vscode-sideBarTitle-foreground); font-size: 11px; font-weight: 700; text-transform: uppercase; }
    .model-badge {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-width: 0;
      max-width: 42%;
      min-height: 22px;
      padding: 2px 6px 2px 3px;
      color: var(--vscode-foreground);
      background: color-mix(in srgb, var(--vscode-charts-blue, #3794ff) 16%, var(--vscode-button-secondaryBackground, #2a2d2e));
      border: 1px solid color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 38%, transparent);
      border-radius: 999px;
    }
    .model-badge:hover {
      background: color-mix(in srgb, var(--vscode-charts-green, #89d185) 20%, var(--vscode-button-secondaryHoverBackground, #3a3d41));
    }
    .model-badge::after {
      content: attr(data-tooltip);
      position: absolute;
      z-index: 3;
      top: calc(100% + 6px);
      right: 0;
      display: none;
      width: min(280px, 78vw);
      padding: 7px 8px;
      color: var(--vscode-editor-foreground);
      background: var(--vscode-editorWidget-background, var(--vscode-editor-background));
      border: 1px solid var(--vscode-editorWidget-border, var(--vscode-focusBorder, #3794ff));
      box-shadow: 0 4px 16px rgba(0, 0, 0, .28);
      white-space: pre-wrap;
      text-align: left;
      font-size: 11px;
      line-height: 1.35;
      pointer-events: none;
    }
    .model-badge:hover::after,
    .model-badge:focus-visible::after {
      display: block;
    }
    .model-mark {
      display: inline-grid;
      place-items: center;
      width: 20px;
      min-width: 20px;
      height: 18px;
      border-radius: 999px;
      color: var(--vscode-button-foreground);
      background: linear-gradient(135deg, var(--vscode-focusBorder, #3794ff), var(--vscode-charts-green, #89d185));
      font-size: 9px;
      font-weight: 700;
    }
    .model-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 10px;
    }
    button {
      border: 0;
      color: var(--vscode-button-secondaryForeground);
      background: color-mix(in srgb, var(--vscode-button-secondaryBackground, #2a2d2e) 88%, var(--vscode-focusBorder, #3794ff) 12%);
      min-height: 24px;
      border-radius: 4px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { background: var(--vscode-button-secondaryHoverBackground); }
    .icon { width: 24px; min-height: 22px; background: transparent; color: var(--vscode-icon-foreground); }
    .icon:hover { color: var(--vscode-focusBorder, #3794ff); background: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 18%, transparent); }
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
    .tree-section {
      padding: 4px 5px 6px;
      border-left: 2px solid color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 70%, var(--vscode-sideBarSectionHeader-border, transparent));
      background: color-mix(in srgb, var(--vscode-input-background, #1f1f1f) 70%, transparent);
    }
    .control-panel {
      border-left-color: color-mix(in srgb, var(--vscode-charts-green, #89d185) 68%, var(--vscode-focusBorder, #3794ff));
    }
    .sources-panel {
      border-left-color: color-mix(in srgb, var(--vscode-charts-yellow, #cca700) 72%, var(--vscode-focusBorder, #3794ff));
    }
    .releases-panel {
      border-left-color: color-mix(in srgb, var(--vscode-charts-green, #89d185) 62%, var(--vscode-focusBorder, #3794ff));
    }
    .section-title {
      margin: 0 0 5px;
      color: var(--vscode-sideBarSectionHeader-foreground);
      font-weight: 700;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .section-title span { float: right; color: var(--vscode-descriptionForeground); font-weight: 400; text-transform: none; }
    summary.section-title {
      display: flex;
      align-items: center;
      gap: 5px;
      margin: 0 0 5px;
      padding: 3px 2px;
      border-radius: 4px;
      list-style-position: inside;
    }
    summary.section-title:hover { background: color-mix(in srgb, var(--vscode-list-hoverBackground, #2a2d2e) 70%, transparent); }
    summary.section-title span { float: none; margin-left: auto; }
    summary::marker { color: var(--vscode-focusBorder, #3794ff); }
    input {
      width: 100%;
      border: 1px solid var(--vscode-input-border, transparent);
      color: var(--vscode-input-foreground);
      background: var(--vscode-input-background);
      padding: 4px 6px;
      outline: none;
      font: inherit;
      border-radius: 4px;
      margin-bottom: 5px;
    }
    .range { display: grid; grid-template-columns: 1fr 76px; gap: 5px; }
    .primary { width: 100%; color: var(--vscode-button-foreground); background: var(--vscode-button-background); margin-bottom: 5px; box-shadow: inset 3px 0 0 color-mix(in srgb, var(--vscode-charts-green, #89d185) 75%, transparent); }
    .primary:hover { background: var(--vscode-button-hoverBackground); }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
    .meta { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; margin: 0 0 10px; }
    .meta-card {
      border-top: 2px solid color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 45%, var(--vscode-sideBarSectionHeader-border, transparent));
      padding: 6px 5px 5px;
      background: color-mix(in srgb, var(--vscode-input-background, #1f1f1f) 65%, transparent);
      color: var(--vscode-foreground);
      text-align: left;
      min-height: 48px;
    }
    .meta-card:hover,
    .meta-card.active {
      background: color-mix(in srgb, var(--vscode-list-hoverBackground, #2a2d2e) 75%, var(--vscode-focusBorder, #3794ff) 10%);
      outline: 1px solid color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 65%, transparent);
    }
    .meta-card:nth-child(2) { border-top-color: color-mix(in srgb, var(--vscode-charts-green, #89d185) 70%, var(--vscode-focusBorder, #3794ff)); }
    .meta-card:nth-child(3) { border-top-color: color-mix(in srgb, var(--vscode-charts-yellow, #cca700) 75%, var(--vscode-focusBorder, #3794ff)); }
    .meta strong { display: block; font-size: 15px; color: var(--vscode-foreground); }
    .meta span, small, .item-meta, .empty { color: var(--vscode-descriptionForeground); font-size: 11px; }
    body[data-filter="visible"] .item[data-visibility="hidden"] { display: none; }
    body[data-filter="released"] .changes-panel { display: none; }
    body[data-filter="released"] .release-card[data-release-state="unreleased"] { display: none; }
    body[data-filter="unreleased"] .release-card[data-release-state="released"] { display: none; }
    body[data-filter="visible"] .sources-panel,
    body[data-filter="unreleased"] .sources-panel { opacity: .72; }
    .model, .telemetry-toggle { width: 100%; display: flex; justify-content: space-between; align-items: center; padding: 4px 6px; text-align: left; }
    .telemetry-toggle { margin-top: 4px; }
    .tone-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; margin-top: 5px; }
    .tone-row button { min-height: 22px; padding: 3px 4px; font-size: 10px; overflow-wrap: anywhere; }
    .tone-row .selected { outline: 1px solid var(--vscode-focusBorder, #3794ff); background: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 22%, var(--vscode-button-secondaryBackground, #2a2d2e)); }
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
    .item .badge {
      padding: 1px 5px;
      border-radius: 999px;
      background: var(--vscode-badge-background, var(--vscode-button-secondaryBackground));
    }
    .item .badge.ready,
    .item .badge.accepted {
      color: var(--vscode-editor-background);
      background: var(--vscode-charts-green, #89d185);
    }
    .item .badge.needs-review,
    .item .badge.edited {
      color: var(--vscode-editor-background);
      background: var(--vscode-charts-yellow, #cca700);
    }
    .item .badge.hidden {
      color: var(--vscode-descriptionForeground);
      background: var(--vscode-button-secondaryBackground, #2a2d2e);
    }
    .request .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 1px 5px;
      border-radius: 999px;
      color: var(--vscode-descriptionForeground);
      background: var(--vscode-badge-background, var(--vscode-button-secondaryBackground));
    }
    .request .badge.running {
      color: var(--vscode-editor-background);
      background: var(--vscode-charts-yellow, #cca700);
    }
    .request .badge.completed {
      color: var(--vscode-editor-background);
      background: var(--vscode-charts-green, #89d185);
    }
    .request .badge.failed {
      color: var(--vscode-button-foreground);
      background: var(--vscode-errorForeground);
    }
    .request .badge.cancelled {
      color: var(--vscode-editor-background);
      background: var(--vscode-charts-yellow, #cca700);
    }
    .spinner {
      width: 9px;
      height: 9px;
      border: 1.5px solid currentColor;
      border-top-color: transparent;
      border-radius: 999px;
      animation: releasepilot-spin .8s linear infinite;
    }
    @keyframes releasepilot-spin {
      to { transform: rotate(360deg); }
    }
    .item p { margin: 3px 0; color: var(--vscode-descriptionForeground); line-height: 1.35; }
    .actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-top: 5px; }
    .actions button { min-height: 22px; font-size: 11px; }
    .suggestions { border-left: 2px solid var(--vscode-focusBorder, #3794ff); padding-left: 6px; background: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 8%, transparent); }
    .suggestion { padding: 6px 0; border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); }
    .release-card {
      margin: 5px 0;
      border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18));
    }
    .release-card > summary {
      display: flex;
      align-items: baseline;
      gap: 6px;
      text-transform: none;
    }
    .release-card > summary span { color: var(--vscode-foreground); margin-left: 0; }
    .release-card > summary small {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .release-note {
      max-height: 220px;
      overflow: auto;
      margin: 0;
      padding: 5px;
      color: var(--vscode-editor-foreground);
      background: var(--vscode-editor-background);
      border: 1px solid var(--vscode-input-border, transparent);
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 10px;
      line-height: 1.4;
    }
    .release-note-list {
      display: grid;
      gap: 4px;
    }
    .release-note-list p {
      margin: 0;
      display: grid;
      gap: 2px;
    }
    .release-note-list span {
      color: var(--vscode-descriptionForeground);
      line-height: 1.35;
    }
    .release-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) max-content;
      gap: 6px;
      padding: 3px 0;
      border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.12));
    }
    .release-row span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .release-commit .source-main { grid-template-columns: 72px minmax(0, 1fr); }
    .requests { border-left-color: color-mix(in srgb, var(--vscode-charts-blue, #3794ff) 65%, var(--vscode-focusBorder, #3794ff)); }
    .request { border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); padding: 4px 0; }
    .request summary { display: flex; align-items: center; gap: 5px; text-transform: none; }
    .request-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .request-stop { min-height: 18px; padding: 0 5px; color: var(--vscode-button-foreground); background: var(--vscode-errorForeground); font-size: 10px; }
    .sub-panel {
      margin: 4px 0;
      padding: 5px;
      border-left: 2px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.24));
      background: color-mix(in srgb, var(--vscode-input-background, #1f1f1f) 72%, transparent);
    }
    .command-panel { padding-top: 3px; padding-bottom: 3px; }
    .activity-panel {
      border-left-color: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 72%, var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.24)));
    }
    .sub-panel-title {
      margin-bottom: 3px;
      color: var(--vscode-descriptionForeground);
      text-transform: uppercase;
      font-weight: 700;
      font-size: 10px;
    }
    .request code { display: block; color: var(--vscode-descriptionForeground); white-space: pre-wrap; word-break: break-word; font-size: 10px; }
    .activity {
      list-style: none;
      display: grid;
      gap: 3px;
      margin: 0;
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
      background: color-mix(in srgb, var(--vscode-input-background, #1f1f1f) 88%, var(--vscode-focusBorder, #3794ff) 12%);
    }
    .activity li.model { border-left-color: var(--vscode-focusBorder); }
    .activity time { color: var(--vscode-descriptionForeground); font-variant-numeric: tabular-nums; }
    .activity span { min-width: 0; overflow-wrap: anywhere; }
    .payload { margin-top: 3px; }
    .payload summary { color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 400; text-transform: none; }
    .request .stream-payload pre {
      max-height: 260px;
      font-size: 11px;
      line-height: 1.45;
      overscroll-behavior: contain;
    }
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
