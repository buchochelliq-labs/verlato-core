import type {
  CommandRequestSnapshot,
  DraftDocument,
  DraftItem,
  DraftRangeRequest,
  DraftSuggestionItem,
  DraftSuggestionPreview,
  ModelSettings,
  OllamaDoctorResult,
  RepositoryCommit,
  RepositoryOverview,
  RepositoryPullRequest,
} from "../client/types";

export interface ReleaseControlRenderState {
  document?: DraftDocument;
  overview?: RepositoryOverview;
  modelSettings: ModelSettings;
  range: DraftRangeRequest;
  busy: boolean;
  suggestions?: DraftSuggestionPreview;
  ollamaDoctor?: OllamaDoctorResult;
  requests?: CommandRequestSnapshot[];
  statusMessage?: string;
  errorMessage?: string;
}

export interface ReleaseActivityRenderState {
  requests: CommandRequestSnapshot[];
}

export interface ReleaseReleasesRenderState {
  document?: DraftDocument;
  overview?: RepositoryOverview;
  suggestions?: DraftSuggestionPreview;
}

export interface ReleaseControlAssets {
  webviewElementsScriptUri?: string;
}

interface ActiveOperationProgress {
  kind: "scan" | "suggest" | "apply" | "refresh" | "command";
  label: string;
  detail: string;
  subheading?: string;
  progressText?: string;
  progressValue?: number;
  progressMax?: number;
  requestId?: number;
  isModelWork: boolean;
}

export function buildReleaseControlHtml(state: ReleaseControlRenderState, assets: ReleaseControlAssets = {}): string {
  const nonce = String(Date.now());
  const sections = state.document?.sections ?? [];
  const itemCount = state.document?.item_count ?? 0;
  const visibleItemCount = countVisibleItems(state.document);
  const hiddenItemCount = countHiddenItems(state.document);
  const draftCommitCount = countDraftCommits(state.document);
  const hasRunningRequest = (state.requests ?? []).some((request) => request.status === "running");
  const progress = activeOperationProgress(state);
  const modelBusy = Boolean(progress?.isModelWork);
  const commitsBySha = commitLookup(state.overview);
  const webviewElementsScript = assets.webviewElementsScriptUri
    ? `<script type="module" nonce="${nonce}" src="${escapeHtml(assets.webviewElementsScriptUri)}"></script>`
    : "";
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  ${webviewElementsScript}
  <style>${styles()}</style>
</head>
<body${modelBusy ? ` data-model-busy="true"` : ""}>
  <main>
    <div class="toolbar">
      <span class="title">${renderBrandMark()}<span>VERLATO</span><small>DIFF-FIRST RELEASE NOTES</small></span>
      <button class="icon" data-action="suggestAll" title="Infer intent for visible diffs" aria-label="Infer intent for visible diffs">${renderIcon("intent")}</button>
      <button class="icon" data-action="preview" title="Preview release notes" aria-label="Preview release notes">${renderIcon("document")}</button>
      <button class="icon" data-action="refresh" title="Analyze repository" aria-label="Analyze repository">${renderIcon("refresh")}</button>
      ${hasRunningRequest ? `<button class="stop" data-action="cancel" title="Stop current request">Stop</button>` : ""}
      ${renderModelBadge(state.modelSettings)}
    </div>

    ${renderProcessRail(progress)}
    ${renderProgressStrip(progress)}
    ${renderNotice(state)}
    <div class="layout">
      <aside class="operations">
        ${renderDraftPanel(state)}
        <section class="meta" aria-label="Release item filters">
          <button class="meta-card" data-action="filterItems" data-filter="unreleased" title="Show the commits represented in the current draft">
            <strong>${draftCommitCount}</strong><span>commits in draft</span>
          </button>
          <button class="meta-card" data-action="filterItems" data-filter="unreleased" title="Show changesets built by grouping those commits">
            <strong>${itemCount}</strong><span>changesets</span>
          </button>
          <button class="meta-card" data-action="filterItems" data-filter="visible" title="Show changesets that will render as release notes">
            <strong>${visibleItemCount}</strong><span>release notes</span>
          </button>
          <button class="meta-card" data-action="filterItems" data-filter="hidden" title="Show hidden draft items that can be unhidden">
            <strong>${hiddenItemCount}</strong><span>hidden</span>
          </button>
        </section>
        <section class="section changes-panel" id="changes-panel">
          <div class="section-title">${renderIcon("diff")}<span>CHANGESETS FROM DIFFS</span><small>${draftCommitCount} commit(s) -> ${itemCount} changeset(s) -> ${visibleItemCount} release note(s)</small></div>
          ${sections.length ? sections.map((section) => renderSection(section, modelBusy, commitsBySha)).join("") : renderEmptyState("diff", "No changes to analyze", "Nothing to report.")}
        </section>
        <details class="section tree-section sources-panel" id="sources-panel">
          <summary class="section-title">${renderIcon("branch")}<span>CONVENTIONAL COMMITS</span><small>Base refs</small></summary>
          <button class="inline-action panel-action" data-action="openSources">Open View</button>
          <div class="selection-summary" id="base-selection-summary">
            <strong>Base selection</strong>
            <span id="base-selection-count">0 selected</span>
          </div>
          <div class="selection-list" id="base-selection-list"></div>
          ${renderSources(state.overview)}
        </details>
        ${renderModelPanel(state)}
        ${renderReleasesPanel(state, modelBusy)}
      </aside>
    </div>
  </main>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    ${scrollStateScript()}
    const base = document.getElementById('base');
    const head = document.getElementById('head');
    const version = document.getElementById('version');
    function optionValue(option) {
      return option.value || option.getAttribute('value') || option.textContent.trim();
    }
    function selectedValues(control) {
      if (Array.isArray(control.value)) return control.value.filter(Boolean);
      if (control.selectedOptions) return Array.from(control.selectedOptions).map((option) => option.value).filter(Boolean);
      const selectedCustomOptions = Array.from(control.querySelectorAll('vscode-option[selected]')).map(optionValue).filter(Boolean);
      if (selectedCustomOptions.length) return selectedCustomOptions;
      return control.value ? [control.value] : [];
    }
    function setSelectedValues(control, values) {
      const refs = values.filter(Boolean);
      const options = Array.from(control.querySelectorAll('option, vscode-option'));
      options.forEach((option) => {
        const selected = refs.includes(optionValue(option));
        option.selected = selected;
        option.toggleAttribute('selected', selected);
      });
      if (control.tagName === 'VSCODE-MULTI-SELECT') control.value = refs;
      else if (control.tagName === 'VSCODE-SINGLE-SELECT') control.value = refs[refs.length - 1] || '';
      else if (control.multiple) {
        Array.from(control.options).forEach((option) => { option.selected = refs.includes(option.value); });
      } else if (refs.length) {
        control.value = refs[refs.length - 1];
      }
    }
    function hasOption(control, ref) {
      return Array.from(control.querySelectorAll('option, vscode-option')).some((option) => optionValue(option) === ref);
    }
    function relatedRefsFor(control, ref) {
      const option = Array.from(control.querySelectorAll('option, vscode-option')).find((candidate) => optionValue(candidate) === ref);
      const relatedRefs = option?.dataset.relatedRefs || '';
      return relatedRefs.split(',').filter(Boolean);
    }
    function expandRelatedRefs(control, refs) {
      const expanded = [];
      const seen = new Set();
      const queue = refs.filter(Boolean);
      while (queue.length) {
        const ref = queue.shift();
        if (!ref || seen.has(ref)) continue;
        seen.add(ref);
        expanded.push(ref);
        relatedRefsFor(control, ref).forEach((relatedRef) => {
          if (!seen.has(relatedRef)) queue.push(relatedRef);
        });
      }
      return expanded;
    }
    function selectedRangeBase() {
      const selected = selectedValues(base);
      const currentValue = Array.isArray(base.value) ? base.value[base.value.length - 1] : base.value;
      return selected[selected.length - 1] || currentValue || '';
    }
    function syncRange() {
      vscode.postMessage({ type: 'setRange', base: selectedRangeBase(), head: head.value, version: version.value });
    }
    [base, head, version].forEach((input) => input.addEventListener('input', syncRange));
    document.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      const action = button.dataset.action;
      if (document.body.dataset.modelBusy === 'true' && ['suggestAll', 'suggest', 'regenerateSuggestion', 'regenerateAll'].includes(action || '')) return;
      syncRange();
      if (action === 'filterItems') {
        const filter = button.dataset.filter || 'all';
        document.body.dataset.filter = filter;
        document.querySelectorAll('.meta-card').forEach((card) => card.classList.toggle('active', card.dataset.filter === filter));
        const target = filter === 'released' || filter === 'unreleased' ? document.getElementById('releases-panel') : document.getElementById('changes-panel');
        target?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        vscode.setState({ ...getStoredState(), releaseFilter: filter });
      }
      if (action === 'scan') vscode.postMessage({ type: 'scan' });
      if (action === 'suggestAll') vscode.postMessage({ type: 'suggestAll' });
      if (action === 'preview') vscode.postMessage({ type: 'preview' });
      if (action === 'refresh') vscode.postMessage({ type: 'refresh' });
      if (action === 'clearWorkingState') vscode.postMessage({ type: 'clearWorkingState' });
      if (action === 'clearSuggestionCache') vscode.postMessage({ type: 'clearSuggestionCache' });
      if (action === 'openSources') vscode.postMessage({ type: 'openSources' });
      if (action === 'closeSources') {
        const picker = document.getElementById('source-picker');
        if (picker) picker.open = false;
      }
      if (action === 'closeBasePicker') {
        const picker = document.getElementById('base-picker');
        if (picker) picker.open = false;
      }
      if (action === 'cancel') vscode.postMessage({ type: 'cancel' });
      if (action === 'cancelRequest') vscode.postMessage({ type: 'cancelRequest', requestId: Number(button.dataset.request) });
      if (action === 'changeModel') vscode.postMessage({ type: 'changeModel' });
      if (action === 'diagnoseOllama') vscode.postMessage({ type: 'diagnoseOllama' });
      if (action === 'warmupOllama') vscode.postMessage({ type: 'warmupOllama' });
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
      if (action === 'unhide') vscode.postMessage({ type: 'mark', itemId: button.dataset.item, status: 'needs-review' });
      if (action === 'prompt') vscode.postMessage({ type: 'prompt', itemId: button.dataset.item });
      if (action === 'suggest') vscode.postMessage({ type: 'suggest', itemId: button.dataset.item });
      if (action === 'applySuggestion') vscode.postMessage({ type: 'applySuggestion', itemId: button.dataset.item, suggestionIndex: Number(button.dataset.index) });
      if (action === 'skipSuggestion') vscode.postMessage({ type: 'skipSuggestion', itemId: button.dataset.item });
      if (action === 'regenerateSuggestion') vscode.postMessage({ type: 'regenerateSuggestion', itemId: button.dataset.item });
      if (action === 'regenerateAll') vscode.postMessage({ type: 'regenerateAll' });
      if (action === 'feedbackPositive') vscode.postMessage({ type: 'feedbackPositive', itemId: button.dataset.item });
      if (action === 'feedbackNegative') vscode.postMessage({ type: 'feedbackNegative', itemId: button.dataset.item });
    });
    const storedState = getStoredState();
    const storedBaseRefs = storedState.selectedBaseRefs || [];
    const storedManualBaseRefs = storedState.manualBaseRefs || [];
    const hasStoredBaseSelection = storedState.baseSelectionTouched === true;
    const initialBaseRefs = selectedValues(base);
    const selectedBaseRefs = new Set(hasStoredBaseSelection ? storedBaseRefs : expandRelatedRefs(base, initialBaseRefs));
    const manualBaseRefs = new Set(hasStoredBaseSelection ? storedManualBaseRefs : initialBaseRefs);
    function recomputeBaseSelection() {
      selectedBaseRefs.clear();
      expandRelatedRefs(base, Array.from(manualBaseRefs)).forEach((ref) => selectedBaseRefs.add(ref));
    }
    function setBaseSelection(ref, selected, manual) {
      if (!ref) return;
      if (manual) {
        if (selected) manualBaseRefs.add(ref);
        else manualBaseRefs.delete(ref);
      }
      recomputeBaseSelection();
    }
    function syncBaseSelection() {
      document.querySelectorAll('.source-toggle[data-ref]').forEach((input) => {
        input.checked = selectedBaseRefs.has(input.dataset.ref);
      });
      document.querySelectorAll('.pr-toggle[data-refs]').forEach((input) => {
        const refs = input.dataset.refs.split(',').filter(Boolean);
        input.checked = refs.length > 0 && refs.every((ref) => selectedBaseRefs.has(ref));
        input.indeterminate = refs.some((ref) => selectedBaseRefs.has(ref)) && !input.checked;
      });
      document.querySelectorAll('.base-tree-toggle[data-ref]').forEach((input) => {
        input.checked = selectedBaseRefs.has(input.dataset.ref);
      });
      document.querySelectorAll('.base-tree-item[data-ref]').forEach((item) => {
        const isSelected = selectedBaseRefs.has(item.dataset.ref);
        item.selected = isSelected;
        item.toggleAttribute('selected', isSelected);
      });
      document.querySelectorAll('.source-row[data-ref]').forEach((row) => {
        const ref = row.dataset.ref;
        const isSelected = selectedBaseRefs.has(ref);
        const isAutoSelected = isSelected && !manualBaseRefs.has(ref);
        row.classList.toggle('selected-source', isSelected);
        row.classList.toggle('auto-selected-source', isAutoSelected);
        const state = row.querySelector('.source-state');
        if (state) {
          state.textContent = isAutoSelected ? 'auto-selected' : isSelected ? 'selected' : '';
        }
      });
      const refs = Array.from(selectedBaseRefs);
      const autoRefs = refs.filter((ref) => !manualBaseRefs.has(ref));
      const selectedRefs = refs.filter((ref) => hasOption(base, ref));
      setSelectedValues(base, selectedRefs.length ? selectedRefs : refs);
      const count = document.getElementById('base-selection-count');
      const list = document.getElementById('base-selection-list');
      const selectedLabel = refs.length === 1 ? '1 selected source' : refs.length + ' selected sources';
      const autoLabel = autoRefs.length ? ' (' + autoRefs.length + ' auto-selected)' : '';
      if (count) count.textContent = selectedLabel + autoLabel;
      if (list) {
        list.textContent = '';
        refs.slice(0, 8).forEach((ref) => {
          const chip = document.createElement('span');
          chip.textContent = ref;
          chip.classList.toggle('auto-selected-chip', !manualBaseRefs.has(ref));
          list.appendChild(chip);
        });
      }
      const sourcePickerCount = document.getElementById('source-picker-count');
      const sourcePickerLabel = document.getElementById('source-picker-label');
      const basePickerCount = document.getElementById('base-picker-count');
      const basePickerLabel = document.getElementById('base-picker-label');
      const sourceTotal = document.getElementById('source-selection-total');
      const sourceAuto = document.getElementById('source-selection-auto');
      if (sourceTotal) sourceTotal.textContent = String(refs.length);
      if (sourceAuto) sourceAuto.textContent = autoRefs.length ? 'includes ' + autoRefs.length + ' auto-selected source(s)' : 'manual selection only';
      if (sourcePickerCount) sourcePickerCount.textContent = selectedLabel + autoLabel;
      if (sourcePickerLabel) sourcePickerLabel.textContent = refs[refs.length - 1] || 'Select sources';
      if (basePickerCount) basePickerCount.textContent = selectedLabel + autoLabel;
      if (basePickerLabel) basePickerLabel.textContent = refs[refs.length - 1] || 'Select base refs';
      vscode.setState({ ...getStoredState(), selectedBaseRefs: refs, manualBaseRefs: Array.from(manualBaseRefs), baseSelectionTouched: true });
      syncRange();
    }
    document.addEventListener('change', (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement)) return;
      if (input.classList.contains('source-toggle')) {
        setBaseSelection(input.dataset.ref, input.checked, true);
        syncBaseSelection();
      }
      if (input.classList.contains('pr-toggle')) {
        setBaseSelection(input.dataset.ref, input.checked, true);
        syncBaseSelection();
      }
      if (input.classList.contains('base-tree-toggle')) {
        setBaseSelection(input.dataset.ref, input.checked, true);
        syncBaseSelection();
      }
    });
    base.addEventListener('change', () => {
      selectedBaseRefs.clear();
      manualBaseRefs.clear();
      const manualRefs = selectedValues(base);
      const manualRefSet = new Set(manualRefs);
      expandRelatedRefs(base, manualRefs).forEach((ref) => setBaseSelection(ref, true, manualRefSet.has(ref)));
      syncBaseSelection();
    });
    const restoredFilter = getStoredState().releaseFilter;
    if (restoredFilter) {
      document.body.dataset.filter = restoredFilter;
      document.querySelectorAll('.meta-card').forEach((card) => card.classList.toggle('active', card.dataset.filter === restoredFilter));
    }
    syncBaseSelection();
  </script>
</body>
</html>`;
}

export function buildReleaseReleasesHtml(state: ReleaseReleasesRenderState): string {
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
    <div class="toolbar view-toolbar">
      <span class="title"><span class="brand-mark">V</span><span>RELEASE NOTES</span><small>READY TO SHIP</small></span>
    </div>
    ${renderReleasesPanel(state)}
  </main>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    ${scrollStateScript()}
    document.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      const action = button.dataset.action;
      if (action === 'useBase') vscode.postMessage({ type: 'useRef', ref: button.dataset.ref, slot: 'base' });
      else if (action === 'useHead') vscode.postMessage({ type: 'useRef', ref: button.dataset.ref, slot: 'head' });
      else if (action === 'copyText') {
        const block = button.closest('details')?.querySelector('pre');
        vscode.postMessage({ type: 'copyText', text: block?.textContent || '', label: button.dataset.copyLabel || 'payload' });
      } else {
        vscode.postMessage({ type: 'openControl' });
      }
    });
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
    <div class="toolbar view-toolbar">
      <span class="title">${renderIcon("terminal")}<span>ACTIVITY</span><small>REQUESTS AND MODEL OUTPUT</small></span>
    </div>
    ${state.requests.length ? renderRequests(state.requests, true) : renderEmptyState("terminal", "No activity yet", "Run a Verlato command to see request and model activity.")}
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
    <span class="model-mark">${renderIcon("intent")}</span>
    <span class="model-name">${escapeHtml(modelBadgeLabel(settings))}</span>
  </button>`;
}

type VerlatoIconName =
  | "apply"
  | "branch"
  | "copy"
  | "diff"
  | "document"
  | "intent"
  | "refresh"
  | "stop"
  | "terminal"
  | "warning";

function renderIcon(name: VerlatoIconName): string {
  const common = `class="verlato-icon ${name}" viewBox="0 0 24 24" aria-hidden="true"`;
  switch (name) {
    case "diff":
      return `<svg ${common}><path class="plus" d="M5 5v6M2 8h6"/><path class="line add" d="M11 5h9M11 9h6"/><path class="minus" d="M2 18h6"/><path class="line remove" d="M11 15h8M11 19h5"/></svg>`;
    case "branch":
      return `<svg ${common}><circle cx="6" cy="5" r="2.5"/><circle cx="18" cy="12" r="2.5"/><circle cx="6" cy="19" r="2.5"/><path d="M8.3 6.2c5 1.2 5.1 4.6 7.3 5.4M8.3 17.8c5-1.2 5.1-4.6 7.3-5.4M6 7.5v9"/></svg>`;
    case "intent":
      return `<svg ${common}><circle cx="5" cy="6" r="2"/><circle cx="5" cy="12" r="2"/><circle cx="5" cy="18" r="2"/><path class="line intent-line" d="M10 6h9M10 12h6M10 18h10"/></svg>`;
    case "document":
      return `<svg ${common}><path d="M6 3.5h8l4 4V20.5H6z"/><path d="M14 3.5v4h4M9 12h6M9 16h6"/></svg>`;
    case "refresh":
      return `<svg ${common}><path d="M18.5 8.5A7 7 0 0 0 6 6l-1.5 2.5M5 4v4.5h4.5M5.5 15.5A7 7 0 0 0 18 18l1.5-2.5M19 20v-4.5h-4.5"/></svg>`;
    case "stop":
      return `<svg ${common}><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`;
    case "terminal":
      return `<svg ${common}><path d="M4 6h16v12H4z"/><path d="m7 10 3 2-3 2M12 15h5"/></svg>`;
    case "warning":
      return `<svg ${common}><path d="M12 4 21 20H3z"/><path d="M12 9v5M12 17h.01"/></svg>`;
    case "copy":
      return `<svg ${common}><rect x="8" y="8" width="10" height="12" rx="1.5"/><path d="M6 16H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>`;
    case "apply":
      return `<svg ${common}><path d="m5 12 4 4L19 6"/><path class="line add" d="M5 6h6M5 18h14"/></svg>`;
  }
}

function renderBrandMark(): string {
  return `<svg class="brand-mark" viewBox="0 0 128 128" aria-hidden="true">
    <path class="brand-v-green" d="M22 24h20l22 58 22-58h20L72 104H56L22 24z"/>
    <path class="brand-v-blue" d="M64 82 86 24h20L72 104H56l8-22z"/>
    <path class="brand-line brand-line-add" d="M84 48h24"/>
    <path class="brand-line brand-line-neutral" d="M80 64h28"/>
    <path class="brand-line brand-line-remove" d="M76 80h20"/>
  </svg>`;
}

function renderProcessRail(progress: ActiveOperationProgress | undefined): string {
  const active = activeProcessStep(progress);
  const steps: { id: string; icon: VerlatoIconName; label: string; detail: string }[] = [
    { id: "diffs", icon: "diff", label: "Diffs", detail: "Source of truth" },
    { id: "commits", icon: "branch", label: "Conventional commits", detail: "Enhance when available" },
    { id: "intent", icon: "intent", label: "Intent inference", detail: "Accurate and grouped" },
    { id: "notes", icon: "document", label: "Release notes", detail: "Ready to ship" },
  ];
  return `<nav class="process-rail" aria-label="Diff-first release flow">
    ${steps.map((step, index) => `<span class="process-step${active === step.id ? " active" : ""}" data-step="${step.id}">
      ${renderIcon(step.icon)}
      <span><strong>${escapeHtml(step.label)}</strong><small>${escapeHtml(step.detail)}</small></span>
    </span>${index < steps.length - 1 ? `<span class="process-arrow" aria-hidden="true"></span>` : ""}`).join("")}
  </nav>`;
}

function activeProcessStep(progress: ActiveOperationProgress | undefined): string {
  if (!progress) {
    return "diffs";
  }
  if (progress.kind === "suggest") {
    return "intent";
  }
  if (progress.kind === "apply") {
    return "notes";
  }
  if (progress.kind === "scan" || progress.kind === "refresh") {
    return "diffs";
  }
  return "diffs";
}

function renderEmptyState(icon: VerlatoIconName, title: string, detail: string): string {
  return `<div class="empty-state">
    ${renderIcon(icon)}
    <strong>${escapeHtml(title)}</strong>
    <span>${escapeHtml(detail)}</span>
  </div>`;
}

function activeOperationProgress(state: ReleaseControlRenderState): ActiveOperationProgress | undefined {
  const running = (state.requests ?? []).find((request) => request.status === "running");
  if (!running) {
    return undefined;
  }
  const args = running?.args ?? [];
  const commandKey = args.slice(0, 2).join(" ");
  const itemIndex = args.indexOf("--id");
  const itemId = itemIndex >= 0 ? args[itemIndex + 1] : undefined;
  const isSuggest = commandKey === "draft suggest";
  const isPreview = commandKey === "draft preview";
  const isDraftJson = commandKey === "draft json";
  const isApply = commandKey === "draft apply";
  const isRefresh = commandKey.startsWith("state") || running?.command === "git" || running?.command === "gh";
  const liveProgress = running ? activeRequestProgress(running) : {};
  if (isSuggest) {
    const scope = itemId ? `item ${itemId}` : "all rendered release-note items";
    return {
      kind: "suggest",
      label: itemId ? "Generating suggestion" : "Generating suggestions",
      detail: `Inferring intent from ${scope} with ${modelBadgeLabel(state.modelSettings)} | ${state.range.base}..${state.range.head ?? "HEAD"}`,
      requestId: running?.id,
      isModelWork: true,
      ...liveProgress,
    };
  }
  if (isApply) {
    return {
      kind: "apply",
      label: "Writing release notes",
      detail: `${state.range.version ?? "Unreleased"} | ${state.range.base}..${state.range.head ?? "HEAD"}`,
      requestId: running?.id,
      isModelWork: false,
      ...liveProgress,
    };
  }
  if (isPreview || isDraftJson) {
    return {
      kind: "scan",
      label: isPreview ? "Generating release notes" : "Processing diffs",
      detail: `${state.range.base}..${state.range.head ?? "HEAD"}`,
      requestId: running?.id,
      isModelWork: false,
      ...liveProgress,
    };
  }
  return {
    kind: isRefresh ? "refresh" : "command",
    label: isRefresh ? "Analyzing repository" : "Running Verlato",
    detail: running?.label || `${state.range.base}..${state.range.head ?? "HEAD"}`,
    requestId: running?.id,
    isModelWork: false,
    ...liveProgress,
  };
}

function activeRequestProgress(request: CommandRequestSnapshot): Partial<ActiveOperationProgress> {
  const output = request.stderr || request.stdout || request.error || "";
  const activity = request.activity?.length ? request.activity : parseActivityFromOutput(output);
  const progressIndex = latestActivityIndex(activity, (entry) => entry.category === "progress");
  const modelStreamIndex = latestActivityIndex(
    activity,
    (entry) => entry.category === "model" && entry.event === "stream_chunk",
  );
  const modelSubheading = modelStreamIndex >= 0 ? formatActivityMessage(activity[modelStreamIndex]) : undefined;

  if (progressIndex < 0) {
    return modelSubheading ? { subheading: modelSubheading } : {};
  }

  const progressEntry = activity[progressIndex];
  const progressRange = progressValue(progressEntry);
  return {
    subheading: modelStreamIndex > progressIndex && modelSubheading
      ? modelSubheading
      : formatProgressSubheading(progressEntry),
    progressText: formatProgressText(progressEntry),
    progressValue: progressRange?.value,
    progressMax: progressRange?.max,
  };
}

function latestActivityIndex(
  activity: NonNullable<CommandRequestSnapshot["activity"]>,
  predicate: (entry: NonNullable<CommandRequestSnapshot["activity"]>[number]) => boolean,
): number {
  for (let index = activity.length - 1; index >= 0; index -= 1) {
    if (predicate(activity[index])) {
      return index;
    }
  }
  return -1;
}

function formatProgressSubheading(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  const attrs = entry.attributes ?? {};
  const label = stringAttr(attrs, "label") ?? formatActivityMessage(entry);
  const detail = stringAttr(attrs, "detail");
  if (!detail || label.includes(detail)) {
    return label;
  }
  return `${label} - ${detail}`;
}

function formatProgressText(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string | undefined {
  const attrs = entry.attributes ?? {};
  const current = optionalNumberAttr(attrs, "current");
  const total = optionalNumberAttr(attrs, "total");
  const unit = stringAttr(attrs, "unit") ?? "step";
  const batch = optionalNumberAttr(attrs, "batch");
  const batchTotal = optionalNumberAttr(attrs, "batch_total");
  const elapsed = optionalNumberAttr(attrs, "elapsed_ms");
  const model = optionalNumberAttr(attrs, "model_ms");
  const queue = optionalNumberAttr(attrs, "queue_ms");
  const promptEval = optionalNumberAttr(attrs, "prompt_eval_ms");
  const evalMs = optionalNumberAttr(attrs, "eval_ms");
  const parts: string[] = [];
  if (current !== undefined && total !== undefined) {
    parts.push(`${unit} ${current}/${total}`);
  }
  if (unit !== "batch" && batch !== undefined && batchTotal !== undefined) {
    parts.push(`batch ${batch}/${batchTotal}`);
  }
  if (elapsed !== undefined) {
    parts.push(`elapsed ${formatDurationMs(elapsed)}`);
  }
  if (model !== undefined) {
    parts.push(`model ${formatDurationMs(model)}`);
  }
  if (queue !== undefined && queue > 0) {
    parts.push(`queue ${formatDurationMs(queue)}`);
  }
  if (promptEval !== undefined) {
    parts.push(`prompt ${formatDurationMs(promptEval)}`);
  }
  if (evalMs !== undefined) {
    parts.push(`gen ${formatDurationMs(evalMs)}`);
  }
  return parts.length ? parts.join(" | ") : undefined;
}

function formatDurationMs(value: number): string {
  if (value < 1000) {
    return `${Math.max(0, Math.round(value))}ms`;
  }
  if (value < 60000) {
    return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}s`;
  }
  return `${(value / 60000).toFixed(1)}m`;
}

function formatCacheAge(seconds: number): string {
  const safeSeconds = Math.max(0, Math.round(seconds));
  if (safeSeconds < 60) {
    return `${safeSeconds}s`;
  }
  if (safeSeconds < 3600) {
    return `${Math.round(safeSeconds / 60)}m`;
  }
  if (safeSeconds < 86400) {
    return `${Math.round(safeSeconds / 3600)}h`;
  }
  return `${Math.round(safeSeconds / 86400)}d`;
}

function progressValue(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): { value: number; max: number } | undefined {
  const attrs = entry.attributes ?? {};
  const current = optionalNumberAttr(attrs, "current");
  const total = optionalNumberAttr(attrs, "total");
  if (current === undefined || total === undefined || total <= 0) {
    return undefined;
  }
  return { value: Math.min(Math.max(current, 0), total), max: total };
}

function renderProgressStrip(progress: ActiveOperationProgress | undefined): string {
  if (!progress) {
    return "";
  }
  const progressBar = progress.progressValue !== undefined && progress.progressMax !== undefined
    ? `<vscode-progress-bar value="${progress.progressValue}" max="${progress.progressMax}"></vscode-progress-bar>`
    : `<vscode-progress-bar long-running-threshold="1000"></vscode-progress-bar>`;
  const subheading = progress.subheading
    ? `<small class="progress-subheading">${escapeHtml(progress.subheading)}</small>`
    : "";
  const meta = progress.progressText || (!progress.subheading ? progress.detail : "");
  const metaLine = meta ? `<small class="progress-meta">${escapeHtml(meta)}</small>` : "";
  const stopAction = progress.requestId === undefined
    ? `<button class="progress-stop" data-action="cancel" title="Stop current request">${renderIcon("stop")}<span>Stop</span></button>`
    : `<button class="progress-stop" data-action="cancelRequest" data-request="${progress.requestId}" title="Stop this request">${renderIcon("stop")}<span>Stop</span></button>`;
  return `<section class="progress-strip ${escapeHtml(progress.kind)}${progress.isModelWork ? " model-work" : ""}" aria-live="polite">
    ${progressBar}
    <div class="progress-copy">
      <strong>${escapeHtml(progress.label)}</strong>
      ${subheading}
      ${metaLine}
    </div>
    ${stopAction}
  </section>`;
}

function renderDraftPanel(state: ReleaseControlRenderState): string {
  return `<details class="section tree-section control-panel" open>
    <summary class="section-title">${renderIcon("document")}<span>RELEASE NOTES</span><small>${escapeHtml(state.range.version ?? "Unreleased")}</small></summary>
    <input id="version" value="${escapeHtml(state.range.version ?? "Unreleased")}" placeholder="Release version">
    <div class="range">
      <div class="select-field">
        <vscode-label for="base">Base</vscode-label>
        ${renderBaseTreePicker(state)}
      </div>
      <div class="select-field">
        <span>Head</span>
        <select id="head" title="Choose the release head ref">
          ${renderNativeRefOptions(state, "head")}
        </select>
      </div>
    </div>
    <button class="primary" data-action="scan">${state.busy ? "Processing diffs..." : "Generate release notes"}</button>
    <div class="split">
      <button data-action="applyChangelog">${renderIcon("apply")}<span>CHANGELOG</span></button>
      <button data-action="applyRelease">${renderIcon("apply")}<span>Release file</span></button>
    </div>
    <button class="secondary full-width" data-action="clearWorkingState" title="Clear unpublished draft review state while preserving published releases">Clear working state</button>
    ${renderReleaseMath(state)}
  </details>`;
}

function renderReleaseMath(state: ReleaseControlRenderState): string {
  const draftItemCount = state.document?.item_count ?? 0;
  const releaseNoteCount = countVisibleItems(state.document);
  const hiddenItemCount = countHiddenItems(state.document);
  const hiddenNote = hiddenItemCount
    ? `${hiddenItemCount} hidden item(s) are kept out of rendered release notes.`
    : "No draft items are hidden.";
  const detail = draftItemCount
    ? `Commits are grouped into changesets by PR, conventional commit signal, and changed area. ${hiddenNote}`
    : "Generate release notes to see how commits are grouped into changesets.";
  return `<div class="release-math" aria-live="polite">
    <div class="release-math-flow">
      <span><strong id="source-selection-total">0</strong><small>selected sources</small></span>
      <span class="release-math-arrow" aria-hidden="true"></span>
      <span><strong>${draftItemCount}</strong><small>changesets</small></span>
      <span class="release-math-arrow" aria-hidden="true"></span>
      <span><strong>${releaseNoteCount}</strong><small>release notes</small></span>
    </div>
    <small><span id="source-selection-auto">Selection updates as refs change.</span> ${escapeHtml(detail)}</small>
  </div>`;
}

function refOptionsForState(state: ReleaseControlRenderState, slot: "base" | "head"): { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean; selected: boolean }[] {
  const current = slot === "base" ? state.range.base : state.range.head ?? "HEAD";
  const options = releaseRefOptions(state.overview, slot);
  const hasCurrent = options.some((option) => option.value === current);
  return [
    ...(!hasCurrent ? [{ value: current, label: `${current} (current)`, group: "Current", relatedRefs: [current] }] : []),
    ...options,
  ].map((option) => ({ ...option, selected: option.value === current }));
}

function renderNativeRefOptions(state: ReleaseControlRenderState, slot: "base" | "head"): string {
  const rendered = refOptionsForState(state, slot);
  return rendered.map((option, index, all) => {
    const previous = all[index - 1];
    const next = all[index + 1];
    const openGroup = previous?.group !== option.group ? `<optgroup label="${escapeHtml(option.group)}">` : "";
    const closeGroup = next?.group !== option.group ? "</optgroup>" : "";
    const selectAllRoot = option.selectAllRoot ? ` data-release-root="true"` : "";
    return `${openGroup}<option value="${escapeHtml(option.value)}" data-related-refs="${escapeHtml(option.relatedRefs.join(","))}"${selectAllRoot}${option.selected ? " selected" : ""}>${escapeHtml(option.label)}</option>${closeGroup}`;
  }).join("");
}

function renderVscodeRefOptions(state: ReleaseControlRenderState, slot: "base" | "head"): string {
  const rendered = refOptionsForState(state, slot);
  return rendered.map((option) => {
    const description = option.group === "Current" ? "current" : option.group;
    const selectAllRoot = option.selectAllRoot ? ` data-release-root="true"` : "";
    return `<vscode-option value="${escapeHtml(option.value)}" description="${escapeHtml(description)}" data-related-refs="${escapeHtml(option.relatedRefs.join(","))}"${selectAllRoot}${option.selected ? " selected" : ""}>${escapeHtml(option.label)}</vscode-option>`;
  }).join("");
}

function renderBaseTreePicker(state: ReleaseControlRenderState): string {
  const options = refOptionsForState(state, "base");
  const treeItems = state.overview ? renderRepositoryBaseTree(state, options) : renderGroupedBaseTree(options);
  return `<select id="base" class="backing-select" multiple aria-hidden="true" tabindex="-1">
          ${renderNativeRefOptions(state, "base")}
        </select>
        <details class="source-dropdown base-tree-picker" id="base-picker">
          <summary title="Choose one or more release base refs">
            <span id="base-picker-label">Select base refs</span>
            <small id="base-picker-count">0 selected</small>
          </summary>
          <div class="source-dropdown-menu base-tree-menu">
            <vscode-tree id="base-tree" multi-select indent-guides="always">
              ${treeItems}
            </vscode-tree>
            <button class="inline-action source-done" data-action="closeBasePicker">Done</button>
          </div>
        </details>`;
}

function renderRepositoryBaseTree(state: ReleaseControlRenderState, options: ReturnType<typeof refOptionsForState>): string {
  const overview = state.overview;
  if (!overview) {
    return renderGroupedBaseTree(options);
  }
  const byValue = new Map(options.map((option) => [option.value, option]));
  const prsByNumber = pullRequestsByNumber(overview);
  const unreleasedCommits = visibleCommitsForPullRequestState(
    overview.commits.filter((commit) => commit.release === null),
    prsByNumber,
  );
  const currentOptions = options.filter((option) => option.group === "Current");
  const tagOptions = options.filter((option) => option.group === "Tags");
  const branchCommitRefs = overview.branches.map((branch) => (
    branch.detail ? commitAndEarlierRefs(branch.detail, unreleasedCommits) : []
  ));
  const duplicateNonPrShas = duplicatedNonPrCommitShas(branchCommitRefs, unreleasedCommits);
  const branchCoveredShas = new Set(branchCommitRefs.flat());
  const renderedPrCommitShas = new Set<string>();
  const renderedPrNumbers = new Set<number>();
  const branchItems = overview.branches.map((branch, index) => {
    const branchOption = byValue.get(branch.name) ?? {
      value: branch.name,
      label: branch.detail ? `${branch.name} - ${branch.detail}` : branch.name,
      group: "Branches",
      relatedRefs: [branch.name],
      selected: state.range.base === branch.name,
    };
    const branchCommits = commitsForRefs(branchCommitRefs[index] ?? [], unreleasedCommits);
    return renderBaseTreeBranch(branchOption, branchCommits, prsByNumber, renderedPrCommitShas, renderedPrNumbers, duplicateNonPrShas);
  }).join("");
  const unassignedCommits = unreleasedCommits.filter((commit) => (
    !branchCoveredShas.has(commit.sha) && !renderedPrCommitShas.has(commit.sha)
  ));
  const unassignedItems = unassignedCommits.length
    ? renderBaseTreeBranch({
      value: "HEAD",
      label: "Unassigned commits",
      group: "Branches",
      relatedRefs: unassignedCommits.map((commit) => commit.sha),
      selected: false,
    }, unassignedCommits, prsByNumber, renderedPrCommitShas, renderedPrNumbers, duplicateNonPrShas)
    : "";
  return [
    currentOptions.length ? renderBaseTreeGroup("Current", currentOptions.map(renderBaseTreeItem).join(""), currentOptions.length) : "",
    tagOptions.length ? renderBaseTreeGroup("Tags", tagOptions.map(renderBaseTreeItem).join(""), tagOptions.length) : "",
    branchItems ? renderBaseTreeGroup("Branches", branchItems, overview.branches.length) : "",
    unassignedItems ? renderBaseTreeGroup("Unassigned", unassignedItems, unassignedCommits.length) : "",
  ].filter(Boolean).join("");
}

function renderGroupedBaseTree(options: ReturnType<typeof refOptionsForState>): string {
  const groups = new Map<string, typeof options>();
  options.forEach((option) => {
    const group = groups.get(option.group) ?? [];
    group.push(option);
    groups.set(option.group, group);
  });
  return Array.from(groups.entries()).map(([group, groupedOptions]) => (
    renderBaseTreeGroup(group, groupedOptions.map(renderBaseTreeItem).join(""), groupedOptions.length)
  )).join("");
}

function renderBaseTreeGroup(label: string, children: string, count: number): string {
  return `<vscode-tree-item open>
      ${escapeHtml(label)}
      <span slot="description">${count}</span>
      ${children}
    </vscode-tree-item>`;
}

function renderBaseTreeBranch(
  option: { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean; selected: boolean },
  commits: RepositoryCommit[],
  prsByNumber: Map<number, RepositoryPullRequest>,
  renderedPrCommitShas: Set<string>,
  renderedPrNumbers: Set<number>,
  duplicateNonPrShas: Set<string>,
): string {
  const allPrGroups = groupCommitsByPr(commits, prsByNumber);
  const hiddenPrCommitShas = new Set(
    allPrGroups
      .filter((pr) => !shouldShowPullRequest(pr.pullRequest))
      .flatMap((pr) => pr.commits.map((commit) => commit.sha)),
  );
  const visibleCommits = commits.filter((commit) => !hiddenPrCommitShas.has(commit.sha));
  const prGroups = allPrGroups
    .filter((pr) => shouldShowPullRequest(pr.pullRequest))
    .filter((pr) => !renderedPrNumbers.has(pr.prNumber))
    .map((pr) => ({
      ...pr,
      commits: pr.commits.filter((commit) => !renderedPrCommitShas.has(commit.sha)),
    }))
    .filter((pr) => pr.commits.length > 0);
  prGroups.forEach((pr) => {
    renderedPrNumbers.add(pr.prNumber);
    pr.commits.forEach((commit) => renderedPrCommitShas.add(commit.sha));
  });
  const prCommitShas = new Set(prGroups.flatMap((pr) => pr.commits.map((commit) => commit.sha)));
  const prItems = prGroups.map((pr) => {
    const prRef = pr.commits[0].sha;
    const refs = pr.commits.map((commit) => commit.sha);
    const stateLabel = pullRequestStateLabel(pr.pullRequest);
    const countLabel = pr.commits.length === 1 ? "1 commit" : `${pr.commits.length} commits`;
    const prOption = {
      value: prRef,
      label: `PR #${pr.prNumber}`,
      group: "Pull requests",
      relatedRefs: refs,
      selectAllRoot: true,
      selected: option.selected && refs.includes(option.value),
    };
    return `<vscode-tree-item class="base-tree-item" data-ref="${escapeHtml(prOption.value)}" data-related-refs="${escapeHtml(prOption.relatedRefs.join(","))}" data-release-root="true"${prOption.selected ? " selected" : ""}>
      <label class="base-tree-row" title="Select PR #${pr.prNumber} and its commits">
        <input class="base-tree-toggle" type="checkbox" data-ref="${escapeHtml(prOption.value)}">
        <span>${escapeHtml(prOption.label)}</span>
      </label>
      <span slot="description">${escapeHtml(stateLabel ? `${stateLabel} | ${countLabel}` : countLabel)}</span>
      ${pr.commits.map((commit) => renderBaseTreeCommit(commit, visibleCommits)).join("")}
    </vscode-tree-item>`;
  }).join("");
  const ungroupedCommitItems = visibleCommits
    .filter((commit) => !prCommitShas.has(commit.sha) && !renderedPrCommitShas.has(commit.sha))
    .map((commit) => renderBaseTreeCommit(commit, visibleCommits, duplicateNonPrShas.has(commit.sha)))
    .join("");
  return `<vscode-tree-item class="base-tree-item" data-ref="${escapeHtml(option.value)}" data-related-refs="${escapeHtml(option.relatedRefs.join(","))}"${option.selected ? " selected" : ""}>
    <label class="base-tree-row" title="Select branch and reachable commits">
      <input class="base-tree-toggle" type="checkbox" data-ref="${escapeHtml(option.value)}">
      <span>${escapeHtml(option.label)}</span>
    </label>
    <span slot="description">${visibleCommits.length === 1 ? "1 commit" : `${visibleCommits.length} commits`}</span>
    ${prItems}
    ${ungroupedCommitItems}
  </vscode-tree-item>`;
}

function renderBaseTreeCommit(
  commit: { sha: string; subject: string; prNumber?: number | null },
  commits: { sha: string }[],
  duplicated = false,
): string {
  return renderBaseTreeItem({
    value: commit.sha,
    label: `${commit.sha} - ${commit.subject}${duplicated ? " *" : ""}`,
    group: duplicated ? "shared commit" : "Unreleased commits",
    relatedRefs: commitAndEarlierRefs(commit.sha, commits),
    selectAllRoot: commit.prNumber === null,
    selected: false,
    duplicated,
  });
}

function renderBaseTreeItem(option: { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean; selected: boolean; duplicated?: boolean }): string {
  const description = option.group === "Current" ? "current" : option.group;
  const selectAllRoot = option.selectAllRoot ? ` data-release-root="true"` : "";
  const duplicateAttrs = option.duplicated ? ` data-duplicated="true"` : "";
  const duplicateClass = option.duplicated ? " duplicated-ref" : "";
  const title = option.duplicated ? "Select as base ref; this commit is reachable from multiple branches" : "Select as base ref";
  return `<vscode-tree-item class="base-tree-item${duplicateClass}" data-ref="${escapeHtml(option.value)}" data-related-refs="${escapeHtml(option.relatedRefs.join(","))}"${selectAllRoot}${duplicateAttrs}${option.selected ? " selected" : ""}>
    <label class="base-tree-row" title="${escapeHtml(title)}">
      <input class="base-tree-toggle" type="checkbox" data-ref="${escapeHtml(option.value)}">
      <span>${escapeHtml(option.label)}</span>
    </label>
    <span slot="description">${escapeHtml(description)}</span>
  </vscode-tree-item>`;
}

function commitsForRefs<T extends { sha: string }>(refs: string[], commits: T[]): T[] {
  const refSet = new Set(refs);
  return commits.filter((commit) => refSet.has(commit.sha));
}

function duplicatedNonPrCommitShas(
  branchCommitRefs: string[][],
  commits: { sha: string; prNumber: number | null }[],
): Set<string> {
  const nonPrShas = new Set(commits.filter((commit) => commit.prNumber === null).map((commit) => commit.sha));
  const counts = new Map<string, number>();
  branchCommitRefs.forEach((refs) => {
    const branchRefs = new Set(refs);
    branchRefs.forEach((ref) => {
      if (nonPrShas.has(ref)) {
        counts.set(ref, (counts.get(ref) ?? 0) + 1);
      }
    });
  });
  return new Set(Array.from(counts.entries()).filter(([, count]) => count > 1).map(([sha]) => sha));
}

function releaseRefOptions(
  overview: RepositoryOverview | undefined,
  slot: "base" | "head",
): { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean }[] {
  const options: { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean }[] = [];
  if (slot === "head") {
    options.push({ value: "HEAD", label: "HEAD", group: "Heads", relatedRefs: ["HEAD"] });
  }
  if (!overview) {
    if (slot === "base") {
      options.push({ value: "HEAD~10", label: "HEAD~10", group: "Base refs", relatedRefs: ["HEAD~10"] });
    }
    return options;
  }
  const prsByNumber = pullRequestsByNumber(overview);
  const unreleasedCommits = visibleCommitsForPullRequestState(
    overview.commits.filter((commit) => commit.release === null),
    prsByNumber,
  );
  if (slot === "base") {
    options.push(...overview.tags.map((tag) => ({
      value: tag.name,
      label: tag.detail ? `${tag.name} - ${tag.detail}` : tag.name,
      group: "Tags",
      relatedRefs: [tag.name],
    })));
    options.push(...groupCommitsByPr(unreleasedCommits, prsByNumber).filter((pr) => shouldShowPullRequest(pr.pullRequest)).map((pr) => ({
      value: pr.commits[0].sha,
      label: `PR #${pr.prNumber} - ${pr.commits.length === 1 ? pr.commits[0].subject : `${pr.commits.length} commit(s)`}`,
      group: "Pull requests",
      relatedRefs: pr.commits.map((commit) => commit.sha),
      selectAllRoot: true,
    })));
  }
  options.push(...overview.branches.map((branch) => ({
    value: branch.name,
    label: branch.detail ? `${branch.name} - ${branch.detail}` : branch.name,
    group: slot === "head" ? "Branches / heads" : "Branches",
    relatedRefs: [
      branch.name,
      ...commitAndEarlierRefs(branch.detail ?? "", unreleasedCommits),
    ],
  })));
  options.push(...unreleasedCommits.map((commit) => ({
    value: commit.sha,
    label: `${commit.sha} - ${commit.subject}`,
    group: "Unreleased commits",
    relatedRefs: commitAndEarlierRefs(commit.sha, unreleasedCommits),
    selectAllRoot: commit.prNumber === null,
  })));
  return dedupeRefOptions(options);
}

function dedupeRefOptions(
  options: { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean }[],
): { value: string; label: string; group: string; relatedRefs: string[]; selectAllRoot?: boolean }[] {
  const seen = new Set<string>();
  return options.filter((option) => {
    if (seen.has(option.value)) {
      return false;
    }
    seen.add(option.value);
    return true;
  });
}

function renderModelPanel(state: ReleaseControlRenderState): string {
  return `<details class="section tree-section model-panel">
    <summary class="section-title">${renderIcon("intent")}<span>INTENT INFERENCE</span><small>${escapeHtml(state.modelSettings.provider)}</small></summary>
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
    <button class="telemetry-toggle" data-action="clearSuggestionCache">
      <span>Suggestion cache</span>
      <small>Clear</small>
    </button>
    <button class="telemetry-toggle" data-action="diagnoseOllama">
      <span>Model health</span>
      <small>Run</small>
    </button>
    <button class="telemetry-toggle" data-action="warmupOllama">
      <span>Warm model</span>
      <small>Run</small>
    </button>
    ${renderOllamaDoctor(state.ollamaDoctor)}
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
  const suggestionModel = settings.suggestionModel.trim() || "same as main";
  const providerDetail = settings.provider === "ollama"
    ? `${settings.model} at ${settings.endpoint}`
    : `fanout guard ${settings.maxModelItems}`;
  return [
    `Provider: ${settings.provider}`,
    `Model: ${providerDetail}`,
    `Suggestion model: ${suggestionModel}`,
    `Tone: ${settings.suggestionTone}`,
    `Suggestions: ${settings.suggestionCount}`,
    `Parallelism: ${settings.suggestionParallelism}`,
    `Batch size: ${settings.suggestionBatchSize}`,
    `Auto-warm cache: ${settings.suggestionAutoWarmCache ? "on" : "off"}`,
    `Prompt profile: ${settings.suggestionPromptProfile}`,
    `Adaptive advice: ${settings.suggestionAdaptiveRecommendations ? "on" : "off"}`,
    `Streaming: ${settings.ollamaStreamResponses ? "on" : "off"}`,
    `Prompt payloads: ${settings.includePromptPayloads ? "on" : "off"}`,
    `Reasoning payloads: ${settings.includeReasoningPayloads ? "on" : "off"}`,
    `Redaction: ${settings.redactPayloads ? "on" : "off"}`,
  ].join("\n");
}

function renderOllamaDoctor(result: OllamaDoctorResult | undefined): string {
  if (!result) {
    return "";
  }
  const firstProbe = result.probes.find((probe) => probe.ok) ?? result.probes[0];
  const timings = firstProbe?.timings;
  const timingParts = [
    typeof timings?.model_ms === "number" ? `model ${formatDurationMs(timings.model_ms)}` : "",
    typeof timings?.queue_ms === "number" && timings.queue_ms > 0 ? `queue ${formatDurationMs(timings.queue_ms)}` : "",
    typeof timings?.load_ms === "number" && timings.load_ms > 0 ? `load ${formatDurationMs(timings.load_ms)}` : "",
    typeof timings?.prompt_eval_ms === "number" && timings.prompt_eval_ms > 0 ? `prompt ${formatDurationMs(timings.prompt_eval_ms)}` : "",
    typeof timings?.eval_ms === "number" && timings.eval_ms > 0 ? `gen ${formatDurationMs(timings.eval_ms)}` : "",
  ].filter(Boolean).join(" | ");
  const recommendations = result.summary.recommendations.slice(0, 2);
  const settings = renderDoctorRecommendedSettings(result.summary.recommended_settings);
  const diagnostics = result.summary.diagnostics.slice(0, 2);
  const runtime = renderOllamaRuntime(result.runtime);
  const modelLine = renderOllamaDoctorModels(result.config);
  return `<div class="doctor-result ${escapeHtml(result.summary.status)}">
    <div><strong>Ollama health</strong><span>${escapeHtml(result.summary.status)} | ${escapeHtml(result.summary.bottleneck.replace("_", " "))}${result.summary.warmed_up ? " | warmed" : ""}</span></div>
    ${modelLine ? `<small>${escapeHtml(modelLine)}</small>` : ""}
    ${timingParts ? `<small>${escapeHtml(timingParts)}</small>` : ""}
    ${runtime ? `<small>${escapeHtml(runtime)}</small>` : ""}
    ${diagnostics.map((diagnostic) => `<small>${escapeHtml(diagnostic)}</small>`).join("")}
    ${settings ? `<small>${escapeHtml(settings)}</small>` : ""}
    ${renderAdaptiveRecommendations(result.adaptive_recommendations)}
    ${recommendations.length ? `<ul>${recommendations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

function renderOllamaDoctorModels(config: OllamaDoctorResult["config"]): string {
  const model = config.model || "configured model";
  const effective = config.effective_suggest_model || config.suggest_model || model;
  if (effective === model) {
    return `Model: ${model}`;
  }
  return `Model: ${model} | suggestion ${effective}`;
}

function renderOllamaRuntime(runtime: OllamaDoctorResult["runtime"]): string {
  const selected = runtime?.selected_model;
  if (!selected) {
    return "";
  }
  const model = selected.model || selected.name || "configured model";
  if (selected.loaded === false) {
    return `Runtime: ${model} not loaded`;
  }
  if (selected.loaded !== true) {
    return runtime?.api_ps?.ok === false ? "Runtime: /api/ps unavailable" : "";
  }
  const parts = [`Runtime: ${model} loaded`];
  if (typeof selected.context_length === "number") {
    parts.push(`ctx ${selected.context_length}`);
  }
  if (typeof selected.size_vram === "number" && selected.size_vram > 0) {
    parts.push(`vram ${formatBytes(selected.size_vram)}`);
  } else if (typeof selected.size === "number" && selected.size > 0) {
    parts.push(`size ${formatBytes(selected.size)}`);
  }
  if (selected.processor) {
    parts.push(String(selected.processor));
  }
  const expiry = formatRuntimeExpiry(selected.expires_at);
  if (expiry) {
    parts.push(`expires ${expiry}`);
  }
  return parts.join(" | ");
}

function renderDoctorRecommendedSettings(settings: OllamaDoctorResult["summary"]["recommended_settings"]): string {
  if (!settings) {
    return "";
  }
  const parts = [
    typeof settings.suggestion_parallelism === "number" ? `try parallelism ${settings.suggestion_parallelism}` : "",
    typeof settings.suggest_num_predict === "number" ? `try suggest cap ${settings.suggest_num_predict}` : "",
    typeof settings.suggest_model === "string" ? `try suggestion model ${settings.suggest_model}` : "",
  ].filter(Boolean);
  return parts.length ? `Recommendation: ${parts.join(", ")}` : "";
}

function formatBytes(value: number): string {
  const safeValue = Math.max(0, value);
  if (safeValue < 1024) {
    return `${Math.round(safeValue)}B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let amount = safeValue / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)}${unit}`;
}

function formatRuntimeExpiry(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) {
    return "";
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return value;
  }
  const seconds = Math.round((timestamp - Date.now()) / 1000);
  return seconds <= 0 ? "expired" : `in ${formatCacheAge(seconds)}`;
}

function renderSuggestions(preview: DraftSuggestionPreview | undefined, modelBusy = false): string {
  if (!preview || !preview.items.length) {
    return "";
  }
  return `<section class="sub-panel suggestions">
    <div class="section-title">${renderIcon("intent")}<span>INTENT INFERENCE</span><button class="inline-action model-action" data-action="regenerateAll"${modelBusy ? " disabled" : ""}>Regenerate</button></div>
    ${renderOllamaQueueWarning(preview)}
    ${renderSuggestionBatching(preview)}
    ${renderAdaptiveRecommendations(preview.adaptive_recommendations)}
    ${preview.items.map((item) => `<article class="suggestion">
      <div class="item-head">
        <span class="name">${escapeHtml(item.title)}</span>
        <span class="badge">${escapeHtml(item.provider)}</span>
        ${item.tone ? `<span class="badge">${escapeHtml(item.tone)}</span>` : ""}
        ${renderSuggestionCacheBadge(item)}
      </div>
      ${renderSuggestionDiagnostics(item)}
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
        <button class="model-action" data-action="regenerateSuggestion" data-item="${escapeHtml(item.item_id)}"${modelBusy ? " disabled" : ""}>Regenerate</button>
        <button class="skip" data-action="skipSuggestion" data-item="${escapeHtml(item.item_id)}">Skip</button>
        <button data-action="feedbackPositive" data-item="${escapeHtml(item.item_id)}">Useful</button>
        <button data-action="feedbackNegative" data-item="${escapeHtml(item.item_id)}">Not useful</button>
      </div>
    </article>`).join("")}
  </section>`;
}

function renderOllamaQueueWarning(preview: DraftSuggestionPreview): string {
  const parallelism = preview.performance?.parallelism ?? 1;
  if (parallelism <= 1) {
    return "";
  }
  const queued = preview.items.some((item) => {
    const queueMs = item.timings?.queue_ms ?? 0;
    const modelMs = item.timings?.model_ms ?? 0;
    return item.provider.startsWith("ollama:") && queueMs >= 250 && (modelMs <= 0 || queueMs / modelMs >= 0.2);
  });
  if (!queued) {
    return "";
  }
  return `<div class="model-diagnostic">${renderIcon("intent")}<span>Ollama queue time detected with parallelism ${parallelism}; the server may be serializing requests.</span></div>`;
}

function renderSuggestionBatching(preview: DraftSuggestionPreview): string {
  const batchSize = preview.performance?.batch_size ?? 1;
  const saved = preview.performance?.model_calls_saved ?? 0;
  const batchedCalls = preview.performance?.batched_model_calls ?? 0;
  if (batchSize <= 1 && saved <= 0 && batchedCalls <= 0) {
    return "";
  }
  const parts = [
    batchSize > 1 ? `batched ${batchSize}/item` : "",
    saved > 0 ? `saved ${saved} call${saved === 1 ? "" : "s"}` : "",
    batchedCalls > 0 ? `${batchedCalls} batch call${batchedCalls === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  return parts.length ? `<div class="model-diagnostic">${renderIcon("intent")}<span>${escapeHtml(parts.join(" | "))}</span></div>` : "";
}

function renderAdaptiveRecommendations(
  adaptive: DraftSuggestionPreview["adaptive_recommendations"] | OllamaDoctorResult["adaptive_recommendations"],
): string {
  const recommendations = adaptive?.recommendations?.slice(0, 2) ?? [];
  if (!recommendations.length) {
    return "";
  }
  const text = recommendations
    .map((item) => `${item.title}${item.confidence ? ` (${item.confidence})` : ""}: ${item.detail}`)
    .join(" | ");
  return `<div class="model-diagnostic">${renderIcon("intent")}<span>Adaptive advice: ${escapeHtml(text)}</span></div>`;
}

function renderSuggestionCacheBadge(item: DraftSuggestionItem): string {
  if (!item.cache?.hit) {
    return "";
  }
  const age = typeof item.cache.age_seconds === "number" ? ` ${formatCacheAge(item.cache.age_seconds)}` : "";
  return `<span class="badge cached">cached${escapeHtml(age)}</span>`;
}

function renderSuggestionDiagnostics(item: DraftSuggestionItem): string {
  const timings = item.timings;
  const budgetText = formatSuggestionPromptBudget(item.prompt_budget, timings);
  if (!timings || !item.provider.startsWith("ollama:")) {
    return budgetText
      ? `<small class="suggestion-diagnostics">Prompt budget: ${escapeHtml(budgetText)}</small>`
      : "";
  }
  const candidates = suggestionTimingCandidates(timings);
  if (!candidates.length) {
    return budgetText
      ? `<small class="suggestion-diagnostics">Prompt budget: ${escapeHtml(budgetText)}</small>`
      : "";
  }
  const dominant = candidates.reduce((best, entry) => entry.value > best.value ? entry : best, candidates[0]);
  const tokenRate = typeof timings.tokens_per_second === "number" ? ` | ${timings.tokens_per_second.toFixed(1)} tok/s` : "";
  const tokenCounts = typeof timings.prompt_tokens === "number" || typeof timings.eval_tokens === "number"
    ? ` | ${timings.prompt_tokens ?? 0}/${timings.eval_tokens ?? 0} tok`
    : "";
  const promptBudget = budgetText ? ` | prompt ${budgetText}` : "";
  return `<small class="suggestion-diagnostics">Ollama bottleneck: ${escapeHtml(dominant.label)} ${formatDurationMs(dominant.value)}${tokenRate}${tokenCounts}${escapeHtml(promptBudget)}</small>`;
}

function suggestionTimingCandidates(timings: DraftSuggestionItem["timings"]): { label: string; value: number }[] {
  if (!timings) {
    return [];
  }
  return [
    { label: "queue", value: timings.queue_ms },
    { label: "load", value: timings.load_ms },
    { label: "prompt eval", value: timings.prompt_eval_ms },
    { label: "generation", value: timings.eval_ms },
  ].filter((entry): entry is { label: string; value: number } => typeof entry.value === "number" && entry.value > 0);
}

function formatSuggestionPromptBudget(
  budget: DraftSuggestionItem["prompt_budget"],
  timings?: DraftSuggestionItem["timings"],
): string {
  if (!budget) {
    return "";
  }
  const parts: string[] = [];
  if (typeof budget.prompt_chars === "number") {
    parts.push(`${formatCompactCount(budget.prompt_chars)} chars`);
  }
  parts.push(...formatBudgetRatio("files", budget.files_included, budget.files_total));
  parts.push(...formatBudgetRatio("hunks", budget.hunks_included, budget.hunks_total));
  if (isPromptEvalDominant(timings)) {
    const skipped = [
      formatSkippedBudget("files", budget.files_skipped),
      formatSkippedBudget("hunks", budget.hunks_skipped),
    ].filter(Boolean);
    if (skipped.length) {
      parts.push(`skipped ${skipped.join(", ")}`);
    }
  }
  return parts.join(" | ");
}

function formatBudgetRatio(label: string, included?: number, total?: number): string[] {
  if (typeof included !== "number" || typeof total !== "number" || total <= 0) {
    return [];
  }
  return [`${label} ${included}/${total}`];
}

function formatSkippedBudget(label: string, skipped?: number): string {
  return typeof skipped === "number" && skipped > 0 ? `${skipped} ${label}` : "";
}

function isPromptEvalDominant(timings?: DraftSuggestionItem["timings"]): boolean {
  const candidates = suggestionTimingCandidates(timings);
  if (!candidates.length) {
    return false;
  }
  const dominant = candidates.reduce((best, entry) => entry.value > best.value ? entry : best, candidates[0]);
  return dominant.label === "prompt eval";
}

function formatCompactCount(value: number): string {
  if (Math.abs(value) < 1000) {
    return String(Math.round(value));
  }
  return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}k`;
}

function renderReleasesPanel(state: ReleaseReleasesRenderState, modelBusy = false): string {
  const released = state.overview?.releases ?? [];
  const releaseCount = released.length + 1;
  return `<details class="section tree-section releases-panel" id="releases-panel" open>
    <summary class="section-title">${renderIcon("document")}<span>RELEASE NOTES</span><small>${releaseCount}</small></summary>
    ${renderUnreleasedRelease(state.document, state.suggestions, state.overview, modelBusy)}
    ${renderReleasedReleases(state.overview)}
  </details>`;
}

function renderUnreleasedRelease(
  document: DraftDocument | undefined,
  suggestions: DraftSuggestionPreview | undefined,
  overview: RepositoryOverview | undefined,
  modelBusy = false,
): string {
  const version = document?.version ?? "Unreleased";
  const items = document?.sections.flatMap((section) => section.items) ?? [];
  const renderedItems = items.filter((item) => !item.hidden);
  const hiddenItems = items.length - renderedItems.length;
  const commits = overview?.commits.filter((commit) => commit.release === null) ?? [];
  const commitsBySha = commitLookup(overview);
  const draftCommitCount = countDraftCommits(document);
  const suggestionCount = suggestions?.items.reduce((sum, item) => sum + item.suggestions.length, 0) ?? 0;
  const hiddenDetail = hiddenItems ? ` | ${hiddenItems} hidden` : "";
  return `<details class="release-card" open data-release-state="unreleased">
    <summary>
      <span>${escapeHtml(version)}</span>
      <small>${draftCommitCount} draft commit(s) -> ${items.length} changeset(s) -> ${renderedItems.length} release note(s)${hiddenDetail} | ${suggestionCount} suggestion(s)</small>
    </summary>
    <div class="sub-panel release-note-panel">
      <div class="sub-panel-title">${renderIcon("document")}<span>Release note</span></div>
      ${renderDraftReleaseNote(document)}
    </div>
    ${renderSuggestions(suggestions, modelBusy)}
    <div class="sub-panel">
      <div class="sub-panel-title">${renderIcon("diff")}<span>Changesets</span></div>
      ${renderedItems.length ? renderedItems.map((item) => renderReleaseDraftRow(item, commitsBySha)).join("") : renderEmptyState("diff", "No changes to analyze", "Nothing to report.")}
    </div>
    ${renderReleaseCommits(commits, "Unreleased commits")}
  </details>`;
}

function renderReleasedReleases(overview: RepositoryOverview | undefined): string {
  if (!overview) {
    return `<div class="sub-panel">${renderEmptyState("terminal", "No activity yet", "Analyze repository to load released versions and commits.")}</div>`;
  }
  if (!overview.releases.length) {
    return `<div class="sub-panel">${renderEmptyState("document", "No release notes yet", "No published release associations were found in local Verlato state.")}</div>`;
  }
  return overview.releases.map((release) => {
    const commits = overview.commits.filter((commit) => commit.release === release.version);
    return `<details class="release-card" data-release-state="released">
      <summary>
        <span>${escapeHtml(release.version)}</span>
        <small>${release.itemCount} release-note item(s) | ${commits.length} commit(s)</small>
      </summary>
      <div class="sub-panel release-note-panel">
        <div class="sub-panel-title">${renderIcon("document")}<span>Release note</span></div>
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
    return renderEmptyState("document", "No release notes yet", "Generate release notes from the selected diffs.");
  }
  return `<div class="release-note-list">${items.map((item) => `<p><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.summary)}</span></p>`).join("")}</div>`;
}

function renderReleaseDraftRow(item: DraftItem, commitsBySha: Map<string, RepositoryCommit> = new Map()): string {
  const suggestionSource = item.summary_source === "model-suggestion" || item.summary_provenance?.source === "model-suggestion"
    ? "model suggestion"
    : item.summary_source ?? "draft";
  const commitCount = item.commit_shas?.length ?? 0;
  return `<div class="release-row">
    <div class="release-row-main">
      <span>${escapeHtml(item.title)}</span>
      <small>${commitCount} commit(s) in changeset | ${escapeHtml(item.status)} | ${escapeHtml(suggestionSource)}</small>
    </div>
    ${renderChangesetCommits(item, commitsBySha)}
  </div>`;
}

function renderChangesetCommits(item: DraftItem, commitsBySha: Map<string, RepositoryCommit> = new Map()): string {
  const shas = item.commit_shas ?? [];
  if (!shas.length) {
    return `<div class="changeset-commits empty">No commits mapped to this changeset.</div>`;
  }
  const visibleShas = shas.slice(0, 50);
  const hiddenCount = shas.length - visibleShas.length;
  const reason = changesetGroupingReason(item);
  return `<details class="changeset-commits"${shas.length > 1 ? " open" : ""}>
    <summary>Changeset commits <span>${shas.length}</span></summary>
    <div class="changeset-reason">${escapeHtml(reason)}</div>
    <ol>
      ${visibleShas.map((sha) => {
        const commit = commitForSha(commitsBySha, sha);
        const subject = commit?.subject ?? "Commit message not loaded in repository overview.";
        const prLabel = commit?.prNumber ? `PR #${commit.prNumber}` : "No PR";
        return `<li>
          <button class="commit-ref" data-action="useBase" data-ref="${escapeHtml(commit?.sha ?? sha)}" title="Use ${escapeHtml(sha.slice(0, 12))} as base ref">
            <code>${escapeHtml(sha.slice(0, 12))}</code>
          </button>
          <span class="commit-message" title="${escapeHtml(subject)}">${escapeHtml(subject)}</span>
          <small>${escapeHtml(prLabel)}</small>
          <button class="commit-head" data-action="useHead" data-ref="${escapeHtml(commit?.sha ?? sha)}" title="Use as head ref">HEAD</button>
        </li>`;
      }).join("")}
    </ol>
    ${hiddenCount > 0 ? `<small>${hiddenCount} more commit(s) in this changeset.</small>` : ""}
  </details>`;
}

function changesetGroupingReason(item: DraftItem): string {
  if (item.grouping_reason?.trim()) {
    return item.grouping_reason.trim();
  }
  if (item.pr_number) {
    return `Grouped by PR #${item.pr_number}.`;
  }
  if ((item.commit_shas?.length ?? 0) > 1) {
    return "Grouped because the commits share an explicit non-PR conventional commit scope.";
  }
  return "Single-commit changeset.";
}

function renderPublishedReleaseNotes(items: { id: string; title: string; summary: string; status?: string; source?: string }[]): string {
  if (!items.length) {
    return renderEmptyState("document", "No release notes yet", "Nothing to report.");
  }
  return `<div class="release-note-list">${items.map((item) => `<p>
    <strong>${escapeHtml(item.title)}</strong>
    <span>${escapeHtml(item.summary)}</span>
    <small>${escapeHtml([item.status, item.source].filter(Boolean).join(" | ") || item.id)}</small>
  </p>`).join("")}</div>`;
}

function renderReleaseCommits(commits: { sha: string; subject: string; prNumber: number | null }[], title: string): string {
  return `<div class="sub-panel">
    <div class="sub-panel-title">${renderIcon("branch")}<span>${escapeHtml(title)}</span><small>${commits.length}</small></div>
    ${commits.length ? commits.slice(0, 12).map((commit) => `<div class="source-row release-commit">
      <button class="source-main" data-action="useBase" data-ref="${escapeHtml(commit.sha)}" title="Use as base ref">
        <span>${escapeHtml(commit.prNumber ? `PR #${commit.prNumber}` : commit.sha)}</span>
        <small>${escapeHtml(commit.subject)}</small>
      </button>
      <button class="source-slot" data-action="useHead" data-ref="${escapeHtml(commit.sha)}" title="Use as head ref">HEAD</button>
    </div>`).join("") : renderEmptyState("branch", "No commits mapped", "Nothing to report.")}
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
    <summary>Provider reasoning <span>${reasoning.length}</span><button class="copy" data-action="copyText" data-copy-label="provider reasoning" title="Copy provider reasoning">${renderIcon("copy")}<span>Copy</span></button></summary>
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
    ? ` - ${Math.round(detail.specificity_score * 100)}% specific`
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
  const open = "";
  const streamClass = entry.event === "stream_chunk" ? " stream-payload" : "";
  const scrollKey = activityPayloadScrollKey(requestId, entry);
  const scrollClass = entry.event === "stream_chunk" ? " class=\"stream-scroll\"" : "";
  return `<details class="payload${streamClass}"${open}><summary>${escapeHtml(label)}<button class="copy" data-action="copyText" data-copy-label="${escapeHtml(label.toLowerCase())}" title="Copy payload">${renderIcon("copy")}<span>Copy</span></button></summary><pre${scrollClass} tabindex="0" data-scroll-key="${escapeHtml(scrollKey)}">${escapeHtml(entry.payload ?? "")}</pre></details>`;
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
      const parsed = parseActivityLine(trimmed);
      entries.push({
        id: index,
        stream: "stderr",
        category: activityCategory(trimmed),
        ...parsed,
        receivedAt: "",
      });
    }
  });
  return mergeStreamActivity(entries);
}

function formatActivityMessage(entry: NonNullable<CommandRequestSnapshot["activity"]>[number]): string {
  if (entry.category === "progress") {
    const attrs = entry.attributes ?? {};
    const label = stringAttr(attrs, "label") ?? entry.message;
    const detail = stringAttr(attrs, "detail");
    const text = detail && !label.includes(detail) ? `${label} - ${detail}` : label;
    const counts = formatProgressText(entry);
    return counts ? `${text} (${counts})` : text;
  }
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
  return entry.message.replace(/^\[verlato:model\]\s*/, "");
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

function optionalNumberAttr(attrs: Record<string, unknown>, key: string): number | undefined {
  return typeof attrs[key] === "number" && Number.isFinite(attrs[key]) ? attrs[key] : undefined;
}

function stringAttr(attrs: Record<string, unknown>, key: string): string | undefined {
  return typeof attrs[key] === "string" ? attrs[key] : undefined;
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

function activityCategory(line: string): "model" | "progress" | "suggestion" | "process" {
  if (line.startsWith("[verlato:model]")) {
    return "model";
  }
  if (line.startsWith("[verlato:progress]")) {
    return "progress";
  }
  if (line.startsWith("[verlato:suggestion]")) {
    return "suggestion";
  }
  return "process";
}

function parseActivityLine(line: string): {
  message: string;
  event?: string;
  attributes?: Record<string, unknown>;
  payload?: string;
} {
  const progress = parseProgressActivityLine(line);
  if (progress) {
    return progress;
  }
  const suggestion = parseSuggestionActivityLine(line);
  if (suggestion) {
    return suggestion;
  }
  const jsonPrefix = "[verlato:model] json ";
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

function parseProgressActivityLine(line: string): {
  message: string;
  event: string;
  attributes: Record<string, unknown>;
} | undefined {
  const jsonPrefix = "[verlato:progress] json ";
  if (!line.startsWith(jsonPrefix)) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(line.slice(jsonPrefix.length)) as Record<string, unknown>;
    const label = typeof parsed.label === "string" ? parsed.label : "Progress update";
    const detail = typeof parsed.detail === "string" ? parsed.detail : "";
    return {
      message: detail ? `${label} - ${detail}` : label,
      event: typeof parsed.event === "string" ? parsed.event : "progress",
      attributes: parsed,
    };
  } catch {
    return {
      message: line,
      event: "progress",
      attributes: {},
    };
  }
}

function parseSuggestionActivityLine(line: string): {
  message: string;
  event: string;
  attributes: Record<string, unknown>;
} | undefined {
  const jsonPrefix = "[verlato:suggestion] json ";
  if (!line.startsWith(jsonPrefix)) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(line.slice(jsonPrefix.length)) as Record<string, unknown>;
    const event = typeof parsed.event === "string" ? parsed.event : "suggestion";
    return {
      message: suggestionActivityMessage(event, parsed, line),
      event,
      attributes: parsed,
    };
  } catch {
    return {
      message: line,
      event: "suggestion",
      attributes: {},
    };
  }
}

function suggestionActivityMessage(event: string, attrs: Record<string, unknown>, fallback: string): string {
  const itemId = typeof attrs.item_id === "string" ? attrs.item_id : undefined;
  if (event === "suggestion_preview_started") {
    const total = typeof attrs.total === "number" ? ` for ${attrs.total} item(s)` : "";
    return `Suggestion preview started${total}`;
  }
  if (event === "suggestion_item_result") {
    return itemId ? `Generated suggestion for ${itemId}` : "Generated suggestion";
  }
  if (event === "suggestion_item_failed") {
    return itemId ? `Suggestion failed for ${itemId}` : "Suggestion failed";
  }
  return fallback;
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
  const match = line.match(/^\[verlato:model\]\s+stream-chunk\s+provider=(\S+)\s+task=(\S+)\s+kind=(\S+)\s+chars=(\d+)(?:\s+redacted=(\S+))?\s+text=(.*)$/);
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
    prior.message = `[verlato:model] stream ${String(kind)} chunks=${priorChunks + nextChunks} chars=${priorChars + nextChars}`;
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

function countDraftCommits(document: DraftDocument | undefined): number {
  const shas = new Set<string>();
  document?.sections.forEach((section) => {
    section.items.forEach((item) => {
      (item.commit_shas ?? []).forEach((sha) => shas.add(sha));
    });
  });
  return shas.size;
}

function countHiddenItems(document: DraftDocument | undefined): number {
  return document?.sections.reduce((sum, section) => sum + section.items.filter((item) => item.hidden).length, 0) ?? 0;
}

function commitLookup(overview: RepositoryOverview | undefined): Map<string, RepositoryCommit> {
  const lookup = new Map<string, RepositoryCommit>();
  (overview?.commits ?? []).forEach((commit) => lookup.set(commit.sha, commit));
  return lookup;
}

function commitForSha(commitsBySha: Map<string, RepositoryCommit>, sha: string): RepositoryCommit | undefined {
  return commitsBySha.get(sha)
    ?? Array.from(commitsBySha.entries()).find(([candidate]) => candidate.startsWith(sha) || sha.startsWith(candidate))?.[1];
}

function renderNotice(state: ReleaseControlRenderState): string {
  if (state.errorMessage) {
    return `<div class="notice error">${renderIcon("warning")}<span>${escapeHtml(state.errorMessage)}</span></div>`;
  }
  if (state.busy) {
    const progress = activeOperationProgress(state);
    if (progress) {
      return "";
    }
    return "";
  }
  if (state.statusMessage) {
    return `<div class="notice">${renderIcon("document")}<span>${escapeHtml(state.statusMessage)}</span></div>`;
  }
  return "";
}

function renderSection(section: { title: string; items: DraftItem[] }, modelBusy = false, commitsBySha: Map<string, RepositoryCommit> = new Map()): string {
  return `<details open>
    <summary>${escapeHtml(section.title)} <span>${section.items.length} changeset(s)</span></summary>
    ${section.items.map((item) => renderItem(item, modelBusy, commitsBySha)).join("")}
  </details>`;
}

function renderItem(item: DraftItem, _modelBusy = false, commitsBySha: Map<string, RepositoryCommit> = new Map()): string {
  const pr = item.pr_number ? `#${item.pr_number}` : item.commit_shas?.[0]?.slice(0, 7) ?? "change";
  const commitCount = item.commit_shas?.length ?? 0;
  const hiddenClass = item.hidden ? " hidden" : "";
  const visibility = item.hidden ? "hidden" : "visible";
  const hiddenNotice = item.hidden ? `<div class="hidden-notice">${renderIcon("warning")}<span>Hidden by saved review state; it will not render until unhidden.</span></div>` : "";
  const visibilityButton = item.hidden
    ? `<button data-action="unhide" data-item="${escapeHtml(item.id)}">Unhide</button>`
    : `<button data-action="hide" data-item="${escapeHtml(item.id)}">Hide</button>`;
  return `<article class="item${hiddenClass}" data-visibility="${visibility}">
    <div class="item-head">
      <span class="name">${escapeHtml(item.title)}</span>
      <button class="icon mini" data-action="suggest" data-item="${escapeHtml(item.id)}" title="Infer intent for this diff" aria-label="Infer intent for this diff">${renderIcon("intent")}</button>
      <span class="badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
    </div>
    <p>${escapeHtml(item.summary)}</p>
    ${hiddenNotice}
    <div class="item-meta">${escapeHtml(pr)} | ${commitCount} commit(s) in changeset | ${escapeHtml(item.area)} | ${escapeHtml(item.semver)} | ${item.confidence.toFixed(2)}</div>
    ${renderChangesetCommits(item, commitsBySha)}
    <div class="actions">
      <button data-action="ready" data-item="${escapeHtml(item.id)}">Ready</button>
      <button data-action="accept" data-item="${escapeHtml(item.id)}">Accept</button>
      <button data-action="prompt" data-item="${escapeHtml(item.id)}">Prompt</button>
      ${visibilityButton}
    </div>
  </article>`;
}

function renderSources(overview: RepositoryOverview | undefined): string {
  if (!overview) {
    return renderEmptyState("branch", "No sources loaded", "Analyze repository to load PRs, tags, branches, commits, and release associations.");
  }
  const prsByNumber = pullRequestsByNumber(overview);
  const unreleasedCommits = visibleCommitsForPullRequestState(
    overview.commits.filter((commit) => commit.release === null),
    prsByNumber,
  );
  const prs = groupCommitsByPr(unreleasedCommits, prsByNumber).filter((pr) => shouldShowPullRequest(pr.pullRequest)).slice(0, 6);
  const tags = overview.tags.slice(0, 6).map((tag) => ({ label: tag.name, ref: tag.name, detail: tag.detail ?? "tag" }));
  const branches = overview.branches.slice(0, 5).map((branch) => ({
    label: branch.name,
    ref: branch.name,
    detail: branch.detail ?? "branch",
    relatedRefs: [
      branch.name,
      ...commitAndEarlierRefs(branch.detail ?? "", unreleasedCommits),
    ],
    unselectRefs: [
      branch.name,
      ...commitAndLaterRefs(branch.detail ?? "", unreleasedCommits),
    ],
  }));
  const commits = unreleasedCommits.slice(0, 8).map((commit) => ({
    label: commit.sha,
    ref: commit.sha,
    detail: commit.subject,
    relatedRefs: commitAndEarlierRefs(commit.sha, unreleasedCommits),
    unselectRefs: commitAndLaterRefs(commit.sha, unreleasedCommits),
  }));
  const groups = [
    renderPrSourceGroup("PRs From Commits", prs, unreleasedCommits),
    renderSourceGroup("Tags", tags),
    renderSourceGroup("Branches", branches),
    renderSourceGroup("Recent Commits", commits),
  ].filter(Boolean);
  if (!groups.length) {
    return renderEmptyState("branch", "No sources found", "Nothing to report.");
  }
  return `<details class="source-dropdown" id="source-picker">
    <summary>
      <span id="source-picker-label">Select sources</span>
      <small id="source-picker-count">0 selected</small>
    </summary>
    <div class="source-dropdown-menu">
      <div class="source-table-head" aria-hidden="true">
        <span></span>
        <span>Ref</span>
        <span>Detail</span>
        <span></span>
      </div>
      ${groups.join("")}
      <button class="inline-action source-done" data-action="closeSources">Done</button>
    </div>
  </details>`;
}

function pullRequestsByNumber(overview: RepositoryOverview): Map<number, RepositoryPullRequest> {
  return new Map((overview.pullRequests ?? []).map((pr) => [pr.number, pr]));
}

function shouldShowPullRequest(pr: RepositoryPullRequest | undefined): boolean {
  return !pr || pr.state !== "CLOSED" || Boolean(pr.mergedAt);
}

function visibleCommitsForPullRequestState(commits: RepositoryCommit[], prsByNumber: Map<number, RepositoryPullRequest>): RepositoryCommit[] {
  return commits.filter((commit) => commit.prNumber === null || shouldShowPullRequest(prsByNumber.get(commit.prNumber)));
}

function pullRequestStateLabel(pr: RepositoryPullRequest | undefined): string {
  if (!pr) {
    return "";
  }
  if (pr.state === "OPEN") {
    return "Open";
  }
  if (pr.state === "MERGED" || pr.mergedAt) {
    return "Merged";
  }
  return "";
}

function groupCommitsByPr(
  commits: RepositoryCommit[],
  prsByNumber = new Map<number, RepositoryPullRequest>(),
): { prNumber: number; commits: RepositoryCommit[]; pullRequest?: RepositoryPullRequest }[] {
  const groups = new Map<number, { prNumber: number; commits: RepositoryCommit[]; pullRequest?: RepositoryPullRequest }>();
  commits.forEach((commit) => {
    if (commit.prNumber === null) {
      return;
    }
    const group = groups.get(commit.prNumber) ?? { prNumber: commit.prNumber, commits: [], pullRequest: prsByNumber.get(commit.prNumber) };
    group.commits.push(commit);
    groups.set(commit.prNumber, group);
  });
  return Array.from(groups.values());
}

function renderPrSourceGroup(
  title: string,
  prs: { prNumber: number; commits: RepositoryCommit[]; pullRequest?: RepositoryPullRequest }[],
  unreleasedCommits: { sha: string }[],
): string {
  if (!prs.length) {
    return "";
  }
  const kind = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `<details class="source-group" open>
    <summary data-source-kind="${escapeHtml(kind)}">${escapeHtml(title)} <span>${prs.length}</span></summary>
    ${prs.map((pr) => {
      const refs = commitAndEarlierRefs(pr.commits[0].sha, unreleasedCommits).join(",");
      const unselectRefs = commitAndLaterRefs(pr.commits[0].sha, unreleasedCommits).join(",");
      const countLabel = pr.commits.length === 1 ? "1 commit" : `${pr.commits.length} commits`;
      const stateLabel = pullRequestStateLabel(pr.pullRequest);
      const detail = stateLabel ? `${stateLabel} | ${countLabel}` : (pr.commits.length === 1 ? pr.commits[0].subject : countLabel);
      return `<div class="source-row pr-row" data-ref="${escapeHtml(pr.commits[0].sha)}">
        <label class="source-main source-check" title="Select PR #${pr.prNumber} and all commits in it">
          <input class="pr-toggle" type="checkbox" data-ref="${escapeHtml(pr.commits[0].sha)}" data-refs="${escapeHtml(refs)}" data-unselect-refs="${escapeHtml(unselectRefs)}">
          <span>${escapeHtml(`PR #${pr.prNumber}`)}</span>
          <small>${escapeHtml(detail)} <span class="source-state"></span></small>
        </label>
        <button class="source-slot" data-action="useHead" data-ref="${escapeHtml(pr.commits[0].sha)}" title="Use PR tip as head ref">HEAD</button>
      </div>
      <div class="pr-commits">
        ${pr.commits.map((commit) => renderSourceRow({
          label: commit.sha,
          ref: commit.sha,
          detail: commit.release ? `released in ${commit.release}` : commit.subject,
          relatedRefs: commitAndEarlierRefs(commit.sha, unreleasedCommits),
          unselectRefs: commitAndLaterRefs(commit.sha, unreleasedCommits),
        })).join("")}
      </div>`;
    }).join("")}
  </details>`;
}

function commitAndEarlierRefs(
  ref: string,
  commits: { sha: string }[],
): string[] {
  const index = commits.findIndex((commit) => commit.sha === ref || commit.sha.startsWith(ref));
  if (index < 0) {
    return [];
  }
  return commits.slice(index).map((commit) => commit.sha);
}

function commitAndLaterRefs(
  ref: string,
  commits: { sha: string }[],
): string[] {
  const index = commits.findIndex((commit) => commit.sha === ref || commit.sha.startsWith(ref));
  if (index < 0) {
    return [];
  }
  return commits.slice(0, index + 1).map((commit) => commit.sha);
}

function renderSourceGroup(title: string, refs: { label: string; ref: string; detail: string; relatedRefs?: string[]; unselectRefs?: string[] }[]): string {
  if (!refs.length) {
    return "";
  }
  const kind = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `<details class="source-group" open>
    <summary data-source-kind="${escapeHtml(kind)}">${escapeHtml(title)} <span>${refs.length}</span></summary>
    ${refs.map(renderSourceRow).join("")}
  </details>`;
}

function renderSourceRow(ref: { label: string; ref: string; detail: string; relatedRefs?: string[]; unselectRefs?: string[] }): string {
  const relatedRefs = ref.relatedRefs?.length ? ref.relatedRefs : [ref.ref];
  const unselectRefs = ref.unselectRefs?.length ? ref.unselectRefs : relatedRefs;
  return `<div class="source-row" data-ref="${escapeHtml(ref.ref)}">
    <label class="source-main source-check" title="Select as base ref">
      <input class="source-toggle" type="checkbox" data-ref="${escapeHtml(ref.ref)}" data-refs="${escapeHtml(relatedRefs.join(","))}" data-unselect-refs="${escapeHtml(unselectRefs.join(","))}">
      <span>${escapeHtml(ref.label)}</span>
      <small>${escapeHtml(ref.detail)} <span class="source-state"></span></small>
    </label>
    <button class="source-slot" data-action="useHead" data-ref="${escapeHtml(ref.ref)}" title="Use as head ref">HEAD</button>
  </div>`;
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
      --verlato-bg: #020912;
      --verlato-panel: #08111d;
      --verlato-green: #22c55e;
      --verlato-blue: #3b82f6;
      --verlato-red: #ef4444;
      --verlato-purple: #8b5cf6;
      --verlato-neutral: #9ca3af;
      --verlato-text: #e5e7eb;
      --verlato-border: rgba(156, 163, 175, .22);
      margin: 0;
      color: var(--vscode-foreground, var(--verlato-text));
      background:
        linear-gradient(180deg, color-mix(in srgb, var(--verlato-bg) 72%, var(--vscode-sideBar-background, #111827) 28%), var(--vscode-sideBar-background, var(--verlato-bg)));
      font: var(--vscode-font-size) var(--vscode-font-family);
    }
    main { padding: 8px 6px 18px; }
    .layout { display: block; }
    .operations { display: grid; gap: 6px; align-content: start; min-width: 0; }
    @media (min-width: 620px) {
      main { padding: 10px 10px 20px; }
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 4px;
      min-height: 28px;
      margin-bottom: 8px;
      padding: 4px 5px;
      border-bottom: 1px solid color-mix(in srgb, var(--verlato-green) 38%, var(--verlato-border));
      background: linear-gradient(90deg, color-mix(in srgb, var(--verlato-green) 13%, transparent), color-mix(in srgb, var(--verlato-blue) 7%, transparent), transparent);
    }
    .view-toolbar {
      margin-bottom: 6px;
    }
    .process-rail {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 12px minmax(0, 1fr) 12px minmax(0, 1fr) 12px minmax(0, 1fr);
      align-items: stretch;
      gap: 4px;
      margin: -2px 0 8px;
      padding: 5px;
      border: 1px solid var(--verlato-border);
      border-left: 2px solid color-mix(in srgb, var(--verlato-green) 78%, transparent);
      background: color-mix(in srgb, var(--vscode-input-background, var(--verlato-panel)) 72%, var(--verlato-bg) 28%);
    }
    .process-step {
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 4px;
      align-items: center;
      min-width: 0;
      padding: 3px 2px;
      opacity: .72;
    }
    .process-step.active {
      opacity: 1;
      background: color-mix(in srgb, var(--verlato-green) 9%, transparent);
      outline: 1px solid color-mix(in srgb, var(--verlato-green) 30%, transparent);
    }
    .process-step strong,
    .process-step small {
      display: block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .process-step strong {
      color: var(--vscode-foreground, var(--verlato-text));
      font-size: 10px;
    }
    .process-step small {
      color: var(--vscode-descriptionForeground, var(--verlato-neutral));
      font-size: 9px;
    }
    .process-arrow {
      align-self: center;
      height: 1px;
      background: color-mix(in srgb, var(--verlato-neutral) 56%, transparent);
      position: relative;
    }
    .process-arrow::after {
      content: "";
      position: absolute;
      right: -1px;
      top: -3px;
      width: 6px;
      height: 6px;
      border-top: 1px solid color-mix(in srgb, var(--verlato-neutral) 70%, transparent);
      border-right: 1px solid color-mix(in srgb, var(--verlato-neutral) 70%, transparent);
      transform: rotate(45deg);
    }
    .progress-strip {
      position: sticky;
      top: 0;
      z-index: 2;
      display: grid;
      grid-template-columns: minmax(0, 1fr) max-content;
      gap: 5px 8px;
      align-items: center;
      margin: -3px 0 8px;
      padding: 6px 7px;
      border-left: 2px solid color-mix(in srgb, var(--verlato-blue) 72%, transparent);
      background: color-mix(in srgb, var(--vscode-sideBar-background, var(--verlato-bg)) 78%, var(--verlato-blue) 14%);
      box-shadow: 0 1px 0 var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18));
    }
    .progress-strip.model-work {
      border-left-color: var(--verlato-purple);
      background: color-mix(in srgb, var(--vscode-sideBar-background, var(--verlato-bg)) 78%, var(--verlato-purple) 14%);
    }
    .progress-strip vscode-progress-bar {
      grid-column: 1 / -1;
      width: 100%;
      min-height: 3px;
    }
    .progress-copy {
      display: grid;
      gap: 1px;
      min-width: 0;
    }
    .progress-copy strong,
    .progress-copy small {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .progress-copy strong {
      font-size: 12px;
    }
    .progress-subheading {
      color: var(--vscode-sideBarTitle-foreground);
    }
    .progress-meta {
      color: var(--vscode-descriptionForeground);
    }
    .progress-stop {
      min-height: 22px;
      padding: 2px 7px;
      color: var(--vscode-button-foreground);
      background: color-mix(in srgb, var(--verlato-red) 86%, var(--vscode-errorForeground, #f85149));
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .title {
      flex: 1;
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      color: var(--vscode-sideBarTitle-foreground);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .title small {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--vscode-charts-green, #22c55e);
      font-size: 9px;
      font-weight: 600;
    }
    .brand-mark {
      display: inline-block;
      width: 22px;
      height: 22px;
      flex: 0 0 22px;
      filter: drop-shadow(0 0 6px color-mix(in srgb, var(--verlato-green) 34%, transparent));
    }
    .brand-v-green { fill: var(--verlato-green); }
    .brand-v-blue { fill: var(--verlato-blue); }
    .brand-line {
      fill: none;
      stroke-width: 8;
      stroke-linecap: round;
    }
    .brand-line-add { stroke: var(--verlato-green); }
    .brand-line-neutral { stroke: var(--verlato-neutral); }
    .brand-line-remove { stroke: var(--verlato-red); }
    .title > span:not(.brand-mark) {
      min-width: 0;
    }
    .verlato-icon {
      width: 16px;
      height: 16px;
      display: inline-block;
      vertical-align: -3px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.8;
      stroke-linecap: round;
      stroke-linejoin: round;
      flex: 0 0 auto;
    }
    .verlato-icon .add,
    .verlato-icon .plus { stroke: var(--verlato-green); }
    .verlato-icon .remove,
    .verlato-icon .minus { stroke: var(--verlato-red); }
    .verlato-icon.intent,
    .verlato-icon .intent-line { stroke: var(--verlato-purple); }
    .verlato-icon.branch { stroke: var(--verlato-blue); }
    .verlato-icon.document { stroke: var(--vscode-foreground, var(--verlato-text)); }
    .verlato-icon.terminal { stroke: var(--verlato-green); }
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
      background: color-mix(in srgb, var(--verlato-purple) 30%, var(--vscode-button-secondaryBackground, #2a2d2e));
    }
    .model-mark .verlato-icon { width: 13px; height: 13px; }
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
    .icon {
      width: 24px;
      min-height: 22px;
      display: inline-grid;
      place-items: center;
      padding: 0;
      background: transparent;
      color: var(--vscode-icon-foreground, var(--verlato-neutral));
    }
    .icon:hover { color: var(--verlato-green); background: color-mix(in srgb, var(--verlato-green) 16%, transparent); }
    .stop {
      min-height: 22px;
      padding: 0 7px;
      color: var(--vscode-button-foreground);
      background: color-mix(in srgb, var(--verlato-red) 86%, var(--vscode-errorForeground, #f85149));
      font-size: 11px;
    }
    .mini { width: 20px; min-height: 18px; }
    .notice {
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      align-items: center;
      gap: 5px;
      margin: 0 0 8px;
      padding: 6px;
      border-left: 2px solid var(--verlato-blue);
      background: color-mix(in srgb, var(--vscode-input-background, var(--verlato-panel)) 82%, var(--verlato-blue) 8%);
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      line-height: 1.35;
    }
    .notice.error { border-left-color: var(--verlato-red); color: var(--vscode-errorForeground, var(--verlato-red)); }
    .section { margin-bottom: 10px; }
    .tree-section {
      padding: 4px 5px 6px;
      border-left: 2px solid color-mix(in srgb, var(--verlato-blue) 70%, var(--vscode-sideBarSectionHeader-border, transparent));
      background: color-mix(in srgb, var(--vscode-input-background, var(--verlato-panel)) 76%, transparent);
    }
    .control-panel {
      border-left-color: var(--verlato-green);
    }
    .model-panel {
      border-left-color: var(--verlato-purple);
    }
    .sources-panel {
      border-left-color: var(--verlato-blue);
      margin-top: 8px;
    }
    .releases-panel {
      border-left-color: var(--vscode-foreground, var(--verlato-text));
      min-width: 0;
      overflow: hidden;
    }
    .section-title {
      display: flex;
      align-items: center;
      gap: 5px;
      margin: 0 0 5px;
      color: var(--verlato-green);
      font-weight: 700;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .section-title span { color: inherit; font-weight: 700; }
    .section-title small { margin-left: auto; color: var(--vscode-descriptionForeground); font-weight: 400; text-transform: none; }
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
    summary.section-title span { margin-left: 0; }
    summary::marker { color: var(--vscode-focusBorder, #3794ff); }
    input,
    select {
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
    select {
      min-height: 25px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    select[multiple] {
      min-height: 132px;
      overflow: auto;
    }
    vscode-multi-select,
    vscode-single-select {
      width: 100%;
      margin-bottom: 5px;
      --dropdown-z-index: 10;
    }
    .backing-select {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
      border: 0;
    }
    .range { display: grid; grid-template-columns: minmax(0, 1fr) minmax(92px, 35%); gap: 5px; }
    .select-field {
      min-width: 0;
      display: grid;
      gap: 2px;
    }
    .select-field span,
    .select-field vscode-label {
      color: var(--vscode-descriptionForeground);
      font-size: 10px;
      text-transform: uppercase;
      font-weight: 700;
    }
    .primary { width: 100%; color: var(--vscode-button-foreground); background: var(--vscode-button-background); margin-bottom: 5px; box-shadow: inset 3px 0 0 var(--verlato-green); }
    .primary:hover { background: var(--vscode-button-hoverBackground); }
    .secondary.full-width {
      width: 100%;
      margin-top: 5px;
      margin-bottom: 0;
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
      box-shadow: inset 3px 0 0 var(--verlato-purple);
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
    .release-math {
      margin-top: 6px;
      padding: 6px;
      border: 1px solid var(--verlato-border);
      border-left: 2px solid var(--verlato-green);
      background: color-mix(in srgb, var(--vscode-input-background, var(--verlato-panel)) 82%, transparent);
    }
    .release-math-flow {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 14px minmax(0, 1fr) 14px minmax(0, 1fr);
      gap: 4px;
      align-items: center;
      margin-bottom: 4px;
    }
    .release-math-flow span:not(.release-math-arrow) {
      min-width: 0;
      display: grid;
      gap: 1px;
      text-align: center;
    }
    .release-math-flow strong {
      color: var(--vscode-foreground, var(--verlato-text));
      font-size: 14px;
      line-height: 1.1;
    }
    .release-math-flow small,
    .release-math > small {
      min-width: 0;
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    .release-math-arrow {
      height: 1px;
      background: color-mix(in srgb, var(--verlato-neutral) 62%, transparent);
      position: relative;
    }
    .release-math-arrow::after {
      content: "";
      position: absolute;
      right: 0;
      top: -3px;
      width: 6px;
      height: 6px;
      border-top: 1px solid color-mix(in srgb, var(--verlato-neutral) 70%, transparent);
      border-right: 1px solid color-mix(in srgb, var(--verlato-neutral) 70%, transparent);
      transform: rotate(45deg);
    }
    .split button,
    .suggestion-actions button,
    .copy {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
    }
    .split .verlato-icon,
    .copy .verlato-icon,
    .progress-stop .verlato-icon {
      width: 13px;
      height: 13px;
    }
    .meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; margin: 0 0 10px; }
    .operations .meta { margin-top: 10px; }
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
    .meta-card:nth-child(2) { border-top-color: var(--verlato-green); }
    .meta-card:nth-child(3) { border-top-color: var(--verlato-purple); }
    .meta-card:nth-child(4) { border-top-color: var(--verlato-red); }
    .meta strong { display: block; font-size: 15px; color: var(--vscode-foreground); }
    .meta span, small, .item-meta, .empty { color: var(--vscode-descriptionForeground); font-size: 11px; }
    body[data-filter="visible"] .item[data-visibility="hidden"] { display: none; }
    body[data-filter="hidden"] .item[data-visibility="visible"] { display: none; }
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
    .panel-action {
      float: none;
      margin: 0 0 5px;
    }
    .item { padding: 6px 0 7px; border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); }
    .item.hidden { opacity: .58; }
    .hidden-notice {
      display: grid;
      grid-template-columns: 16px minmax(0, 1fr);
      gap: 5px;
      align-items: center;
      margin: 4px 0;
      padding: 4px 5px;
      border-left: 2px solid var(--verlato-purple);
      background: color-mix(in srgb, var(--verlato-purple) 10%, transparent);
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      line-height: 1.35;
    }
    .hidden-notice .verlato-icon {
      width: 14px;
      height: 14px;
      color: var(--verlato-purple);
    }
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
      background: var(--verlato-green);
    }
    .item .badge.needs-review,
    .item .badge.edited {
      color: var(--vscode-editor-background);
      background: color-mix(in srgb, var(--verlato-blue) 70%, var(--verlato-green));
    }
    .item .badge.hidden {
      color: var(--vscode-descriptionForeground);
      background: var(--vscode-button-secondaryBackground, #2a2d2e);
    }
    .suggestion .badge {
      padding: 1px 5px;
      border-radius: 999px;
      background: var(--vscode-badge-background, var(--vscode-button-secondaryBackground));
    }
    .suggestion .badge.cached {
      color: var(--vscode-editor-background);
      background: var(--verlato-green);
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
      background: var(--verlato-blue);
    }
    .request .badge.completed {
      color: var(--vscode-editor-background);
      background: var(--verlato-green);
    }
    .request .badge.failed {
      color: var(--vscode-button-foreground);
      background: var(--verlato-red);
    }
    .request .badge.cancelled {
      color: var(--vscode-editor-background);
      background: var(--verlato-neutral);
    }
    .spinner {
      width: 9px;
      height: 9px;
      border: 1.5px solid currentColor;
      border-top-color: transparent;
      border-radius: 999px;
      animation: verlato-spin .8s linear infinite;
    }
    @keyframes verlato-spin {
      to { transform: rotate(360deg); }
    }
    .item p { margin: 3px 0; color: var(--vscode-descriptionForeground); line-height: 1.35; }
    .actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin-top: 5px; }
    .actions button { min-height: 22px; font-size: 11px; }
    body[data-model-busy="true"] button[data-action="suggestAll"],
    body[data-model-busy="true"] button[data-action="suggest"],
    body[data-model-busy="true"] button[data-action="regenerateSuggestion"],
    body[data-model-busy="true"] button[data-action="regenerateAll"] {
      opacity: .48;
      cursor: progress;
      pointer-events: none;
    }
    .suggestions { border-left: 2px solid var(--verlato-purple); padding-left: 6px; background: color-mix(in srgb, var(--verlato-purple) 8%, transparent); }
    .suggestion { padding: 6px 0; border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)); }
    .release-card {
      margin: 5px 0;
      border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18));
      min-width: 0;
    }
    .release-card > summary {
      display: flex;
      align-items: baseline;
      gap: 6px;
      text-transform: none;
      min-width: 0;
    }
    .release-card > summary span {
      color: var(--vscode-foreground);
      margin-left: 0;
      max-width: 42%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .release-card > summary small {
      flex: 1;
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
      gap: 3px;
      padding: 3px 0;
      border-top: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.12));
      min-width: 0;
    }
    .release-row-main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) max-content;
      gap: 6px;
      min-width: 0;
    }
    .release-row span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .release-row small {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .changeset-commits {
      margin: 3px 0 0;
      padding: 3px 5px;
      border-left: 2px solid color-mix(in srgb, var(--verlato-blue) 70%, transparent);
      background: color-mix(in srgb, var(--vscode-input-background, #1f1f1f) 72%, transparent);
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
    }
    .changeset-commits summary {
      padding: 0;
      font-size: 10px;
      text-transform: none;
    }
    .changeset-reason {
      margin-top: 4px;
      color: var(--vscode-descriptionForeground);
      line-height: 1.35;
    }
    .changeset-commits ol {
      display: flex;
      flex-direction: column;
      gap: 3px;
      margin: 5px 0 0;
      padding: 0;
      list-style: none;
    }
    .changeset-commits li {
      display: grid;
      grid-template-columns: minmax(82px, auto) minmax(0, 1fr) auto auto;
      align-items: start;
      gap: 6px;
      min-width: 0;
      padding: 2px 0;
      border-top: 1px solid color-mix(in srgb, var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18)) 70%, transparent);
    }
    .changeset-commits li:first-child { border-top: 0; }
    .changeset-commits .commit-message {
      min-width: 0;
      overflow-wrap: anywhere;
      color: var(--vscode-descriptionForeground);
    }
    .changeset-commits li small {
      color: var(--vscode-descriptionForeground);
      white-space: nowrap;
    }
    .changeset-commits button {
      min-height: 18px;
      padding: 0 5px;
      border-radius: 3px;
      font-size: 10px;
    }
    .commit-ref {
      justify-self: start;
      color: var(--vscode-textLink-foreground);
      background: color-mix(in srgb, var(--vscode-button-secondaryBackground, #2a2d2e) 70%, transparent);
    }
    .commit-head {
      color: var(--vscode-button-secondaryForeground);
      background: var(--vscode-button-secondaryBackground);
    }
    .changeset-commits code { font-size: 10px; color: var(--vscode-editor-foreground); }
    .release-commit .source-main {
      display: grid;
      grid-template-columns: minmax(46px, 64px) minmax(0, 1fr);
      min-width: 0;
    }
    .release-commit.source-row {
      grid-template-columns: minmax(0, 1fr) 45px;
      gap: 4px;
      padding: 2px 0;
    }
    .release-commit .source-main small { max-width: 100%; }
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
      min-width: 0;
      overflow: hidden;
    }
    .command-panel { padding-top: 3px; padding-bottom: 3px; }
    .activity-panel {
      border-left-color: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 72%, var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.24)));
    }
    .sub-panel-title {
      display: flex;
      align-items: center;
      gap: 5px;
      margin-bottom: 3px;
      color: var(--vscode-descriptionForeground);
      text-transform: uppercase;
      font-weight: 700;
      font-size: 10px;
    }
    .sub-panel-title small { margin-left: auto; }
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
    .activity li.model { border-left-color: var(--verlato-purple); }
    .activity li.progress { border-left-color: var(--verlato-green); }
    .activity li.process { border-left-color: var(--verlato-blue); }
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
    .compare div:nth-child(2) { border-left-color: var(--verlato-purple); }
    .compare strong { color: var(--vscode-descriptionForeground); font-size: 10px; text-transform: uppercase; }
    .compare span { line-height: 1.35; overflow-wrap: anywhere; }
    .evidence, .rejected, .reasoning { margin-top: 4px; }
    .evidence summary, .rejected summary, .reasoning summary { color: var(--vscode-descriptionForeground); font-size: 10px; font-weight: 400; text-transform: none; }
    .model-diagnostic {
      display: flex;
      align-items: center;
      gap: 5px;
      margin: 4px 0;
      padding: 4px 5px;
      color: var(--vscode-descriptionForeground);
      background: color-mix(in srgb, var(--verlato-purple) 10%, transparent);
      border-left: 2px solid var(--verlato-purple);
      font-size: 10px;
      line-height: 1.35;
    }
    .model-diagnostic .verlato-icon { width: 13px; height: 13px; flex: 0 0 auto; }
    .doctor-result {
      margin-top: 5px;
      padding: 5px;
      color: var(--vscode-descriptionForeground);
      background: color-mix(in srgb, var(--verlato-purple) 10%, transparent);
      border-left: 2px solid var(--verlato-purple);
      font-size: 10px;
      line-height: 1.35;
    }
    .doctor-result.error { border-left-color: var(--verlato-red); }
    .doctor-result.ok { border-left-color: var(--verlato-green); }
    .doctor-result div { display: flex; justify-content: space-between; gap: 6px; }
    .doctor-result strong { color: var(--vscode-foreground); }
    .doctor-result small { display: block; margin-top: 3px; }
    .doctor-result ul { margin: 4px 0 0 14px; padding: 0; }
    .suggestion-diagnostics {
      display: block;
      margin-top: 3px;
      color: var(--vscode-descriptionForeground);
      font-variant-numeric: tabular-nums;
    }
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
    .source-dropdown {
      margin-top: 6px;
    }
    .source-dropdown > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 28px;
      padding: 4px 7px;
      color: var(--vscode-dropdown-foreground, var(--vscode-foreground));
      background: var(--vscode-dropdown-background, var(--vscode-input-background));
      border: 1px solid var(--vscode-dropdown-border, var(--vscode-input-border, transparent));
      border-radius: 4px;
      cursor: pointer;
      text-transform: none;
    }
    .source-dropdown > summary span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
    }
    .source-dropdown > summary small {
      flex: 0 0 auto;
      color: var(--vscode-descriptionForeground);
    }
    .source-dropdown-menu {
      max-height: 280px;
      overflow: auto;
      margin-top: 4px;
      padding: 4px;
      background: var(--vscode-input-background);
      border: 1px solid var(--vscode-input-border, transparent);
      border-radius: 4px;
    }
    .base-tree-picker {
      margin-top: 0;
      margin-bottom: 5px;
    }
    .base-tree-menu {
      max-height: 300px;
      padding: 2px;
    }
    #base-tree {
      display: block;
      min-height: 28px;
    }
    .base-tree-row {
      display: inline-grid;
      grid-template-columns: 16px minmax(0, 1fr);
      gap: 5px;
      align-items: center;
      min-width: 0;
      max-width: 100%;
      cursor: pointer;
    }
    .base-tree-row input {
      width: 13px;
      height: 13px;
      margin: 0;
    }
    .base-tree-row span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .base-tree-item.duplicated-ref .base-tree-row span {
      color: var(--vscode-charts-yellow, #cca700);
      font-weight: 700;
    }
    .source-table-head,
    .source-row {
      display: grid;
      grid-template-columns: 18px minmax(96px, 24%) minmax(0, 1fr) 45px;
      gap: 6px;
      align-items: center;
      min-width: 0;
    }
    .source-table-head {
      position: sticky;
      top: 0;
      z-index: 1;
      padding: 3px 6px;
      color: var(--vscode-descriptionForeground);
      background: var(--vscode-input-background);
      border-bottom: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.18));
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .source-table-head span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .source-group { margin-bottom: 3px; }
    .source-group > summary {
      padding: 5px 4px 3px;
      border-bottom: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.14));
    }
    .source-row {
      min-height: 27px;
      margin: 0;
      padding: 2px 4px;
      border-bottom: 1px solid var(--vscode-sideBarSectionHeader-border, rgba(127,127,127,.10));
    }
    .source-row.selected-source {
      background: color-mix(in srgb, var(--vscode-list-activeSelectionBackground, #04395e) 44%, transparent);
    }
    .source-row.auto-selected-source {
      border-left: 2px solid var(--vscode-charts-green, #89d185);
      background: color-mix(in srgb, var(--vscode-charts-green, #89d185) 12%, var(--vscode-list-hoverBackground, transparent));
    }
    .source-main {
      display: contents;
      min-width: 0;
      overflow: hidden;
      padding: 0;
      text-align: left;
    }
    .source-check {
      color: var(--vscode-button-secondaryForeground);
      cursor: pointer;
    }
    .source-check:hover {
      background: color-mix(in srgb, var(--vscode-list-hoverBackground, var(--vscode-button-secondaryHoverBackground)) 70%, transparent);
    }
    .source-check input {
      width: 13px;
      height: 13px;
      margin: 0;
      justify-self: start;
    }
    .source-main span {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
    }
    .source-main small {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--vscode-descriptionForeground);
    }
    .source-state {
      display: inline-block;
      margin-left: 4px;
      color: var(--vscode-charts-green, #89d185);
      font-weight: 700;
    }
    .source-slot {
      min-height: 20px;
      padding: 0 4px;
      font-size: 10px;
    }
    .pr-row {
      background: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 8%, transparent);
    }
    .pr-commits {
      margin: 0 0 6px;
      padding: 0;
    }
    .pr-commits .source-row {
      padding-left: 16px;
      grid-template-columns: 18px minmax(84px, 23%) minmax(0, 1fr) 45px;
    }
    .source-done {
      width: 100%;
      min-height: 24px;
      margin-top: 3px;
    }
    .selection-summary {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      margin: 2px 0 4px;
      padding: 4px 5px;
      background: var(--vscode-input-background);
      border-left: 2px solid color-mix(in srgb, var(--vscode-charts-green, #89d185) 70%, var(--vscode-focusBorder, #3794ff));
    }
    .selection-summary strong {
      font-size: 10px;
      text-transform: uppercase;
    }
    .selection-list {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin-bottom: 6px;
    }
    .selection-list span {
      max-width: 100%;
      padding: 2px 5px;
      border-radius: 4px;
      color: var(--vscode-foreground);
      background: color-mix(in srgb, var(--vscode-focusBorder, #3794ff) 18%, var(--vscode-input-background, #1f1f1f));
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 10px;
    }
    .selection-list span.auto-selected-chip {
      color: var(--vscode-editor-background);
      background: color-mix(in srgb, var(--verlato-green) 78%, var(--verlato-blue));
    }
    .empty-state {
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr);
      gap: 2px 7px;
      align-items: center;
      padding: 7px 6px;
      border: 1px solid var(--verlato-border);
      border-left: 2px solid color-mix(in srgb, var(--verlato-green) 65%, transparent);
      background: color-mix(in srgb, var(--vscode-input-background, var(--verlato-panel)) 76%, transparent);
    }
    .empty-state .verlato-icon {
      grid-row: 1 / span 2;
      width: 18px;
      height: 18px;
    }
    .empty-state strong {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--vscode-foreground, var(--verlato-text));
      font-size: 12px;
    }
    .empty-state span {
      min-width: 0;
      color: var(--vscode-descriptionForeground, var(--verlato-neutral));
      font-size: 11px;
      line-height: 1.35;
    }
  `;
}
