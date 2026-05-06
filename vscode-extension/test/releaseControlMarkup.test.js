const assert = require("node:assert/strict");
const test = require("node:test");

const { buildReleaseActivityHtml, buildReleaseControlHtml, buildReleaseReleasesHtml } = require("../dist/webview/releaseControlMarkup");
const manifest = require("../package.json");

const modelSettings = {
  provider: "mock",
  model: "mock-model",
  suggestionModel: "",
  endpoint: "http://127.0.0.1:11434/api/chat",
  ollamaTimeoutSeconds: 120,
  maxModelItems: 25,
  suggestionCount: 1,
  suggestionParallelism: 2,
  suggestionBatchSize: 2,
  suggestionAutoWarmCache: true,
  suggestionInstantProvisional: true,
  suggestionPromptProfile: "conservative",
  suggestionAdaptiveRecommendations: false,
  suggestionTone: "release-note",
  styleGuidance: "",
  projectVocabulary: [],
  bannedPhrases: [],
  specificityFloor: 0,
  feedbackIncludeText: false,
  includePromptPayloads: false,
  includeReasoningPayloads: false,
  redactPayloads: true,
  debugModelLogPath: "",
  ollamaStreamResponses: true,
};

const range = { base: "v1.0.0", head: "HEAD", version: "Unreleased" };

test("renders sticky scan progress inside the release control view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 7,
        command: "verlato",
        args: ["draft", "json", "--base", "v1.0.0"],
        label: "draft json",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
      },
    ],
  });

  assert.match(html, /class="progress-strip scan"/);
  assert.match(html, /<vscode-progress-bar long-running-threshold="1000"><\/vscode-progress-bar>/);
  assert.match(html, /Processing diffs/);
  assert.match(html, /class="process-rail"/);
  assert.match(html, /Diffs/);
  assert.match(html, /Conventional commits/);
  assert.match(html, /Intent inference/);
  assert.match(html, /Release notes/);
  assert.match(html, /data-action="cancel"/);
  assert.match(html, /data-action="cancelRequest" data-request="7"/);
  assert.match(html, /Stop/);
});

test("renders model suggestion progress without generic scan copy", () => {
  const html = buildReleaseControlHtml({
    modelSettings: { ...modelSettings, provider: "ollama", model: "gemma4:latest" },
    range,
    busy: true,
    requests: [
      {
        id: 11,
        command: "verlato",
        args: ["draft", "suggest", "--preview", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
      },
    ],
  });

  assert.match(html, /data-model-busy="true"/);
  assert.match(html, /class="progress-strip suggest model-work"/);
  assert.match(html, /Generating suggestions/);
  assert.match(html, /Inferring intent from all rendered release-note items with gemma4:latest/);
  assert.doesNotMatch(html, /Scanning v1\.0\.0\.\.HEAD/);
  assert.match(html, /body\[data-model-busy="true"\] button\[data-action="suggestAll"\]/);
});

test("renders running substatus and determinate progress when progress telemetry is available", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 12,
        command: "verlato",
        args: ["draft", "suggest", "--preview", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
        activity: [
          {
            id: 1,
            stream: "stderr",
            category: "progress",
            message: "Prompting model for ci_1",
            event: "progress",
            attributes: {
              label: "Prompting model for ci_1",
              current: 2,
              total: 6,
              unit: "item",
              batch: 1,
              batch_total: 3,
              elapsed_ms: 1250,
              model_ms: 1100,
              queue_ms: 300,
              prompt_eval_ms: 450,
              eval_ms: 500,
            },
            receivedAt: "2026-05-03T12:00:01.000Z",
          },
        ],
      },
    ],
  });

  assert.match(html, /Prompting model for ci_1/);
  assert.match(html, /item 2\/6 \| batch 1\/3 \| elapsed 1\.3s \| model 1\.1s \| queue 300ms \| prompt 450ms \| gen 500ms/);
  assert.match(html, /<vscode-progress-bar value="2" max="6"><\/vscode-progress-bar>/);
  assert.doesNotMatch(html, /long-running-threshold="1000"/);
});

test("keeps unknown running commands indeterminate", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 13,
        command: "verlato",
        args: ["unknown"],
        label: "unknown",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
      },
    ],
  });

  assert.match(html, /Running Verlato/);
  assert.match(html, /<vscode-progress-bar long-running-threshold="1000"><\/vscode-progress-bar>/);
  assert.doesNotMatch(html, /value="\d+"/);
});

test("does not show running controls when busy has no active request", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 15,
        command: "verlato",
        args: ["draft", "json"],
        label: "draft json",
        status: "completed",
        startedAt: "2026-05-03T12:00:00.000Z",
        endedAt: "2026-05-03T12:00:01.000Z",
        stdout: "{}",
        stderr: "",
      },
    ],
  });

  assert.doesNotMatch(html, /Running Verlato/);
  assert.doesNotMatch(html, /class="progress-strip/);
  assert.doesNotMatch(html, /class="stop" data-action="cancel"/);
  assert.doesNotMatch(html, /class="progress-stop"/);
});

test("uses model streaming as compact substatus without exposing payloads", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 14,
        command: "verlato",
        args: ["draft", "suggest", "--preview", "--id", "ci_1"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
        activity: [
          {
            id: 1,
            stream: "stderr",
            category: "progress",
            message: "Prompting model for ci_1",
            event: "progress",
            attributes: {
              label: "Prompting model for ci_1",
              current: 1,
              total: 2,
              unit: "item",
            },
            receivedAt: "2026-05-03T12:00:01.000Z",
          },
          {
            id: 2,
            stream: "stderr",
            category: "model",
            message: "[verlato:model] stream content chunks=2 chars=18",
            event: "stream_chunk",
            attributes: { kind: "content", chunks: 2, chars: 18 },
            payload: "Do not show this payload",
            receivedAt: "2026-05-03T12:00:02.000Z",
          },
        ],
      },
    ],
  });

  assert.match(html, /Response streaming\.\.\. 2 chunks/);
  assert.match(html, /item 1\/2/);
  assert.doesNotMatch(html, /Do not show this payload/);
});

test("renders prompt payload telemetry toggle in the model panel", () => {
  const disabledHtml = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: false,
  });
  const enabledHtml = buildReleaseControlHtml({
    modelSettings: { ...modelSettings, includePromptPayloads: true },
    range,
    busy: false,
  });

  assert.match(disabledHtml, /data-action="togglePromptPayloads"/);
  assert.match(disabledHtml, /data-action="toggleReasoningPayloads"/);
  assert.match(disabledHtml, /data-action="togglePayloadRedaction"/);
  assert.match(disabledHtml, /class="section tree-section model-panel"/);
  assert.match(disabledHtml, /Prompt payloads/);
  assert.match(disabledHtml, /Reasoning payloads/);
  assert.match(disabledHtml, /Payload redaction/);
  assert.match(disabledHtml, /<small>Off<\/small>/);
  assert.match(enabledHtml, /<small>On<\/small>/);
});

test("renders current model as a clickable hover badge", () => {
  const html = buildReleaseControlHtml({
    modelSettings: { ...modelSettings, provider: "ollama", model: "gemma4:latest", suggestionCount: 3 },
    range,
    busy: false,
  });

  assert.match(html, /class="model-badge" data-action="changeModel"/);
  assert.match(html, /<span class="model-mark"><svg class="verlato-icon intent"/);
  assert.match(html, /<span class="model-name">gemma4:latest<\/span>/);
  assert.match(html, /Provider: ollama/);
  assert.match(html, /Suggestions: 3/);
  assert.match(html, /Parallelism: 2/);
  assert.match(html, /Streaming: on/);
});

test("renders repository sources as clickable data in the release control view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: false,
    overview: {
      branches: [{ name: "main", detail: "abc1234" }],
      tags: [{ name: "v1.0.0", detail: "2026-05-01" }],
      commits: [
        {
          sha: "f954ba2",
          subject: "Add release control UX",
          author: "n1ckyb, 2026-05-03",
          prNumber: 12,
          release: null,
        },
      ],
      releases: [],
      unpublishedCount: 3,
    },
    statusMessage: "Repository activity loaded: 1 recent commit(s), 1 branch(es), 1 tag(s).",
  });

  assert.match(html, /Repository activity loaded: 1 recent commit/);
  assert.match(html, /<details class="section tree-section sources-panel" id="sources-panel">/);
  assert.match(html, /CONVENTIONAL COMMITS/);
  assert.match(html, /data-action="openSources"/);
  assert.match(html, /<details class="source-dropdown" id="source-picker">/);
  assert.doesNotMatch(html, /<details class="source-dropdown" id="source-picker" open>/);
  assert.match(html, /data-action="closeSources"/);
  assert.match(html, /PRs From Commits/);
  assert.match(html, /Recent Commits/);
  assert.match(html, /Add release control UX/);
  assert.match(html, /class="pr-toggle" type="checkbox" data-ref="f954ba2" data-refs="f954ba2"/);
  assert.match(html, /class="source-toggle" type="checkbox" data-ref="f954ba2" data-refs="f954ba2"/);
  assert.match(html, /data-action="useHead" data-ref="f954ba2"/);
  assert.match(html, /\.source-row\.auto-selected-source/);
  assert.match(html, /auto-selected-chip/);
  assert.match(html, /<span class="source-state"><\/span>/);
});

test("places all release control sections in one left stack", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: false,
    overview: {
      branches: [],
      tags: [],
      commits: [],
      releases: [],
      unpublishedCount: 0,
    },
  });
  const operations = html.match(/<aside class="operations">([\s\S]*?)<\/aside>/)?.[1] ?? "";
  const metaIndex = operations.indexOf('class="meta"');
  const changesIndex = operations.indexOf('id="changes-panel"');
  const sourcesIndex = operations.indexOf('id="sources-panel"');
  const modelIndex = operations.indexOf('class="section tree-section model-panel"');
  const releasesIndex = operations.indexOf('id="releases-panel"');

  assert.doesNotMatch(html, /class="workspace"/);
  assert.ok(metaIndex > -1);
  assert.ok(changesIndex > metaIndex);
  assert.ok(sourcesIndex > changesIndex);
  assert.ok(modelIndex > sourcesIndex);
  assert.ok(releasesIndex > modelIndex);
});

test("renders draft range refs as selections and excludes released commits", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range: { base: "v1.0.0", head: "HEAD", version: "Unreleased" },
    busy: false,
    overview: {
      branches: [{ name: "main", detail: "abc1234" }, { name: "maintenance", detail: "ccc3333" }],
      tags: [{ name: "v1.0.0", detail: "2026-05-01" }],
      commits: [
        { sha: "cls1111", subject: "Closed without merge", author: "drew", prNumber: 13, release: null },
        { sha: "abc1234", subject: "Unreleased work", author: "alice", prNumber: 12, release: null },
        { sha: "bbb2222", subject: "Earlier unreleased work", author: "alice", prNumber: 12, release: null },
        { sha: "opn1111", subject: "Open PR work", author: "oscar", prNumber: 14, release: null },
        { sha: "ccc3333", subject: "Independent older work", author: "casey", prNumber: null, release: null },
        { sha: "def5678", subject: "Already shipped", author: "bob", prNumber: 10, release: "v0.9.0" },
      ],
      pullRequests: [
        { number: 12, title: "Merged feature", state: "MERGED", mergedAt: "2026-05-03T10:00:00Z" },
        { number: 13, title: "Closed experiment", state: "CLOSED", mergedAt: null },
        { number: 14, title: "Open feature", state: "OPEN", mergedAt: null },
      ],
      releases: [],
      unpublishedCount: 1,
    },
  });
  const basePicker = html.match(/<details class="source-dropdown base-tree-picker" id="base-picker">([\s\S]*?)<\/details>/)?.[1] ?? "";

  assert.match(html, /<select id="base" class="backing-select" multiple aria-hidden="true" tabindex="-1"/);
  assert.match(html, /<details class="source-dropdown base-tree-picker" id="base-picker">/);
  assert.match(basePicker, /<vscode-tree id="base-tree" multi-select indent-guides="always">/);
  assert.match(basePicker, /<vscode-tree-item open>\s+Branches\s+<span slot="description">2<\/span>/);
  assert.match(basePicker, /data-ref="main" data-related-refs="main,abc1234,bbb2222,opn1111,ccc3333"/);
  assert.match(basePicker, /<span>main - abc1234<\/span>/);
  assert.match(basePicker, /data-ref="maintenance" data-related-refs="maintenance,ccc3333"/);
  assert.match(basePicker, /<span>maintenance - ccc3333<\/span>/);
  assert.match(basePicker, /<span>PR #12<\/span>/);
  assert.match(basePicker, /Merged \| 2 commits/);
  assert.match(basePicker, /abc1234 - Unreleased work/);
  assert.match(basePicker, /data-ref="abc1234" data-related-refs="abc1234,bbb2222" data-release-root="true"/);
  assert.match(basePicker, /bbb2222 - Earlier unreleased work/);
  assert.match(basePicker, /<span>PR #14<\/span>/);
  assert.match(basePicker, /Open \| 1 commit/);
  assert.match(basePicker, /opn1111 - Open PR work/);
  assert.doesNotMatch(basePicker, /PR #13/);
  assert.doesNotMatch(basePicker, /Closed without merge/);
  assert.equal((basePicker.match(/ccc3333 - Independent older work \*/g) ?? []).length, 2);
  assert.match(basePicker, /class="base-tree-item duplicated-ref" data-ref="ccc3333"/);
  assert.match(basePicker, /data-duplicated="true"/);
  assert.doesNotMatch(html, /<vscode-multi-select id="base"/);
  assert.match(html, /<select id="head"/);
  assert.doesNotMatch(html, /<input id="base"/);
  assert.doesNotMatch(html, /<input id="head"/);
  assert.doesNotMatch(html, /data-action="selectAllBase"/);
  assert.doesNotMatch(html, /data-action="unselectAllBase"/);
  assert.doesNotMatch(html, />Select all<\/button>/);
  assert.doesNotMatch(html, />Unselect all<\/button>/);
  assert.match(html, /<option value="abc1234" data-related-refs="abc1234,bbb2222" data-release-root="true">PR #12 - 2 commit\(s\)<\/option>/);
  assert.doesNotMatch(html, /PR #13 - Closed without merge/);
  assert.match(html, /<option value="opn1111" data-related-refs="opn1111" data-release-root="true">PR #14 - Open PR work<\/option>/);
  assert.match(html, /<option value="bbb2222" data-related-refs="bbb2222,opn1111,ccc3333">bbb2222 - Earlier unreleased work<\/option>/);
  assert.match(html, /<option value="ccc3333" data-related-refs="ccc3333" data-release-root="true">ccc3333 - Independent older work<\/option>/);
  assert.match(html, /function recomputeBaseSelection\(\)/);
  assert.match(html, /recomputeBaseSelection\(\);\n    }\n    function syncBaseSelection/);
  assert.match(html, /function expandRelatedRefs\(control, refs\)/);
  assert.match(html, /expandRelatedRefs\(base, manualRefs\)\.forEach/);
  assert.match(html, /class="source-toggle" type="checkbox" data-ref="main" data-refs="main,abc1234,bbb2222,opn1111,ccc3333"/);
  assert.match(html, /class="source-toggle" type="checkbox" data-ref="maintenance" data-refs="maintenance,ccc3333"/);
  assert.match(html, /class="source-toggle" type="checkbox" data-ref="abc1234" data-refs="abc1234,bbb2222,opn1111,ccc3333"/);
  assert.match(html, /class="source-toggle" type="checkbox" data-ref="bbb2222" data-refs="bbb2222,opn1111,ccc3333" data-unselect-refs="abc1234,bbb2222"/);
  assert.doesNotMatch(html, /def5678 - Already shipped/);
});

test("allows an intentionally empty base selection", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range: { base: "v1.0.0", head: "HEAD", version: "Unreleased" },
    busy: false,
    overview: {
      branches: [],
      tags: [{ name: "v1.0.0", detail: "2026-05-01" }],
      commits: [],
      releases: [],
      unpublishedCount: 0,
    },
  });

  assert.match(html, /baseSelectionTouched === true/);
  assert.match(html, /return selected\[selected\.length - 1\] \|\| currentValue \|\| '';/);
  assert.doesNotMatch(html, /ensureSelection\(base/);
});

test("loads VSCode Elements bundle when a webview asset URI is provided", () => {
  const html = buildReleaseControlHtml({ modelSettings, range, busy: false }, {
    webviewElementsScriptUri: "vscode-resource:/extension/media/vendor/vscode-elements.bundled.js",
  });

  assert.match(html, /<script type="module" nonce="\d+" src="vscode-resource:\/extension\/media\/vendor\/vscode-elements\.bundled\.js"><\/script>/);
});

test("renders releases as a dedicated sub-panel with notes, commits, and suggestions", () => {
  const html = buildReleaseReleasesHtml({
    modelSettings,
    range,
    busy: false,
    document: {
      version: "Unreleased",
      generated_on: "2026-05-03",
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      targets: [],
      rendered: { "github-release": "## Unreleased\n\n- Improve API review" },
      item_count: 1,
      sections: [{
        kind: "changed",
        title: "Changed",
        items: [draftItem({
          id: "ci_1",
          title: "Improve API",
          commit_shas: ["abc123456789", "def567890123"],
          grouping_reason: "Grouped by PR #12.",
        })],
      }],
    },
    overview: {
      branches: [],
      tags: [],
      commits: [
        {
          sha: "abc1234",
          subject: "Improve API review",
          author: "alice, 2026-05-03",
          prNumber: 12,
          release: null,
        },
        {
          sha: "def5678",
          subject: "Ship previous release",
          author: "bob, 2026-05-02",
          prNumber: 10,
          release: "v1.0.0",
        },
      ],
      releases: [
        {
          version: "v1.0.0",
          itemCount: 2,
          items: [
            {
              id: "published_1",
              title: "Previous release note",
              summary: "Shipped the previous release",
              status: "accepted",
              source: "model-suggestion",
            },
          ],
        },
      ],
      unpublishedCount: 1,
    },
    suggestions: {
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      provider: "mock",
      count: 1,
      items: [
        {
          item_id: "ci_1",
          title: "Improve API",
          current_summary: "A concise release note",
          provider: "mock-local",
          suggestions: ["Improve API review for release notes"],
        },
      ],
    },
  });

  assert.match(html, /id="releases-panel"/);
  assert.match(html, /<details class="section tree-section releases-panel" id="releases-panel" open>/);
  assert.match(buildReleaseControlHtml({ modelSettings, range, busy: false }), /id="releases-panel"/);
  assert.match(html, /data-release-state="unreleased"/);
  assert.match(html, /2 draft commit\(s\) -> 1 changeset\(s\) -> 1 release note\(s\) \| 1 suggestion\(s\)/);
  assert.match(html, /Release note/);
  assert.match(html, /## Unreleased/);
  assert.match(html, /Changesets/);
  assert.match(html, /Improve API/);
  assert.match(html, /2 commit\(s\) in changeset/);
  assert.match(html, /Changeset commits <span>2<\/span>/);
  assert.match(html, /Grouped by PR #12\./);
  assert.match(html, /abc123456789/);
  assert.match(html, /def567890123/);
  assert.match(html, /class="commit-message" title="Improve API review">Improve API review<\/span>/);
  assert.match(html, /data-action="useBase" data-ref="abc1234"/);
  assert.match(html, /data-action="useHead" data-ref="abc1234"/);
  assert.match(html, /INTENT INFERENCE/);
  assert.match(html, /Improve API review for release notes/);
  assert.match(html, /Unreleased commits/);
  assert.match(html, /data-action="useBase" data-ref="abc1234"/);
  assert.match(html, /data-release-state="released"/);
  assert.match(html, /v1\.0\.0/);
  assert.match(html, /Previous release note/);
  assert.match(html, /Shipped the previous release/);
  assert.match(html, /accepted \| model-suggestion/);
  assert.match(html, /Commits in release/);
  assert.match(html, /data-action="useBase" data-ref="def5678"/);
  assert.match(html, /body\[data-filter="released"\] \.release-card\[data-release-state="unreleased"\]/);
  assert.match(html, /body\[data-filter="unreleased"\] \.release-card\[data-release-state="released"\]/);
  assert.doesNotMatch(html, /Associated Releases/);
});

test("explains selected sources versus draft items and rendered release notes", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: false,
    document: {
      version: "Unreleased",
      generated_on: "2026-05-03",
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      targets: [],
      rendered: {},
      item_count: 2,
      sections: [
        {
          kind: "changed",
          title: "Changed",
          items: [
            draftItem({ id: "visible", title: "Visible item", hidden: false, status: "needs-review", commit_shas: ["aaa111111111", "bbb222222222", "ccc333333333"] }),
            draftItem({ id: "hidden", title: "Hidden item", hidden: true, status: "hidden" }),
          ],
        },
      ],
    },
  });

  assert.match(html, /class="meta-card" data-action="filterItems" data-filter="unreleased" title="Show the commits represented in the current draft"/);
  assert.match(html, /<strong>4<\/strong><span>commits in draft<\/span>/);
  assert.match(html, /title="Show changesets built by grouping those commits"/);
  assert.match(html, /<strong>2<\/strong><span>changesets<\/span>/);
  assert.match(html, /class="meta-card" data-action="filterItems" data-filter="visible"/);
  assert.match(html, /<strong>1<\/strong><span>release notes<\/span>/);
  assert.match(html, /data-filter="hidden" title="Show hidden draft items that can be unhidden"/);
  assert.match(html, /<strong>1<\/strong><span>hidden<\/span>/);
  assert.match(html, /CHANGESETS FROM DIFFS/);
  assert.match(html, /4 commit\(s\) -> 2 changeset\(s\) -> 1 release note\(s\)/);
  assert.match(html, /class="release-math"/);
  assert.match(html, /data-action="clearWorkingState"/);
  assert.match(html, /Clear working state/);
  assert.match(html, /preserving published releases/);
  assert.match(html, /selected sources/);
  assert.match(html, /changesets/);
  assert.match(html, /release notes/);
  assert.match(html, /Commits are grouped into changesets by PR, conventional commit signal, and changed area/);
  assert.match(html, /1 hidden item\(s\) are kept out of rendered release notes/);
  assert.match(html, /id="source-selection-total">0<\/strong>/);
  assert.match(html, /id="source-selection-auto">Selection updates as refs change\.<\/span>/);
  assert.match(html, /selectedLabel = refs\.length === 1 \? '1 selected source' : refs\.length \+ ' selected sources'/);
  assert.match(html, /autoLabel = autoRefs\.length \? ' \(' \+ autoRefs\.length \+ ' auto-selected\)' : ''/);
  assert.match(html, /data-visibility="visible"/);
  assert.match(html, /data-visibility="hidden"/);
  assert.match(html, /body\[data-filter="visible"\] \.item\[data-visibility="hidden"\]/);
  assert.match(html, /body\[data-filter="hidden"\] \.item\[data-visibility="visible"\]/);
  assert.match(html, /Visible item/);
  assert.match(html, /3 commit\(s\) in changeset/);
  assert.match(html, /aaa111111111/);
  assert.match(html, /Hidden item/);
  assert.match(html, /item hidden/);
  assert.match(html, /Hidden by saved review state; it will not render until unhidden/);
  assert.match(html, /data-action="unhide" data-item="hidden"/);
});

test("renders scanner errors in the view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: false,
    errorMessage: "Verlato CLI was not found",
  });

  assert.match(html, /notice error/);
  assert.match(html, /Verlato CLI was not found/);
});

test("renders suggestion actions and review variants", () => {
  const state = {
    modelSettings,
    range,
    busy: false,
    document: {
      version: "Unreleased",
      generated_on: "2026-05-03",
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      targets: [],
      rendered: {},
      item_count: 1,
      sections: [{ kind: "changed", title: "Changed", items: [draftItem({ id: "ci_1", title: "Improve API" })] }],
    },
    suggestions: {
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      provider: "mock",
      count: 3,
      items: [
        {
          item_id: "ci_1",
          title: "Improve API",
          current_summary: "A concise release note",
          provider: "instant:local",
          provisional: { source: "instant-local", state: "provisional" },
          instant_confidence: {
            score: 0.81,
            band: "high",
            reasons: ["top-local-candidate", "specific-evidence"],
            evidence_overlap: 0.75,
            similarity_to_current: 0.12,
            specificity_score: 0.7,
            candidate_rank: 1,
          },
          provider_reasoning: "Focused on the API response diff.",
          source_evidence: [
            { kind: "pull-request", label: "PR #12", detail: "Improve API" },
            { kind: "file", label: "src/api.ts", detail: "modified" },
          ],
          suggestion_details: [
            {
              summary: "Make API responses easier to inspect",
              similarity_to_current: 0.12,
              comparison: {
                current: "A concise release note",
                suggested: "Make API responses easier to inspect",
              },
            },
          ],
          rejected_candidates: [
            {
              summary: "A concise release note",
              reason: "too-similar-to-current",
              similarity_to_current: 1,
            },
          ],
          suggestions: ["Make API responses easier to inspect", "Refine API response details"],
        },
      ],
    },
  };
  const controlHtml = buildReleaseControlHtml(state);
  const html = buildReleaseReleasesHtml(state);

  assert.match(controlHtml, /data-action="suggestAll"/);
  assert.match(controlHtml, /data-action="suggest" data-item="ci_1"/);
  assert.match(html, /INTENT INFERENCE/);
  assert.match(html, /provisional/);
  assert.match(html, /confidence high 81%/);
  assert.match(html, /Current/);
  assert.match(html, /Suggested/);
  assert.match(html, /Evidence/);
  assert.match(html, /Provider reasoning/);
  assert.match(html, /Focused on the API response diff/);
  assert.match(html, /data-copy-label="provider reasoning"/);
  assert.match(html, /src\/api\.ts/);
  assert.match(html, /Rejected/);
  assert.match(html, /too-similar-to-current/);
  assert.match(html, /Make API responses easier to inspect/);
  assert.match(html, /12% similar to current/);
  assert.match(html, /data-action="applySuggestion" data-item="ci_1" data-index="0"/);
  assert.match(html, /data-action="skipSuggestion" data-item="ci_1"/);
  assert.match(html, /data-action="regenerateSuggestion" data-item="ci_1"/);
  assert.match(html, /data-action="regenerateAll"/);
  assert.match(html, /data-action="feedbackPositive" data-item="ci_1"/);
  assert.match(html, /data-action="feedbackNegative" data-item="ci_1"/);
});

test("renders Ollama timing diagnostics and queue warning for suggestions", () => {
  const state = {
    modelSettings: { ...modelSettings, provider: "ollama" },
    range,
    busy: false,
    document: {
      version: "Unreleased",
      generated_on: "2026-05-03",
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      targets: [],
      rendered: {},
      item_count: 1,
      sections: [{ kind: "changed", title: "Changed", items: [draftItem({ id: "ci_1", title: "Improve API" })] }],
    },
    suggestions: {
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      provider: "ollama",
      count: 1,
      performance: { parallelism: 2, batch_size: 2, batched_model_calls: 1, model_calls_saved: 3 },
      adaptive_recommendations: {
        schema_version: "verlato.suggestion_adaptive.v1",
        recommendations: [
          {
            kind: "use-lean-prompt-profile",
            title: "Try the lean suggestion prompt profile",
            detail: "Prompt evaluation dominates suggestion time.",
            confidence: "medium",
          },
        ],
      },
      items: [
        {
          item_id: "ci_1",
          title: "Improve API",
          current_summary: "A concise release note",
          provider: "ollama:gemma4:latest",
          timings: {
            model_ms: 3000,
            queue_ms: 600,
            load_ms: 120,
            prompt_eval_ms: 1400,
            eval_ms: 900,
            prompt_tokens: 30,
            eval_tokens: 15,
            tokens_per_second: 16.7,
          },
          prompt_budget: {
            profile: "suggestion-conservative",
            prompt_chars: 1720,
            files_included: 8,
            files_total: 14,
            files_skipped: 6,
            hunks_included: 2,
            hunks_total: 7,
            hunks_skipped: 5,
          },
          cache: { hit: true, age_seconds: 90 },
          suggestions: ["Make API responses easier to inspect"],
        },
      ],
    },
  };

  const html = buildReleaseReleasesHtml(state);

  assert.match(html, /Ollama queue time detected with parallelism 2/);
  assert.match(html, /batched 2\/item \| saved 3 calls \| 1 batch call/);
  assert.match(html, /Adaptive advice: Try the lean suggestion prompt profile \(medium\): Prompt evaluation dominates suggestion time/);
  assert.match(html, /cached 2m/);
  assert.match(html, /Ollama bottleneck: prompt eval 1\.4s \| 16\.7 tok\/s \| 30\/15 tok \| prompt 1\.7k chars \| files 8\/14 \| hunks 2\/7 \| skipped 6 files, 5 hunks/);
});

test("renders Ollama doctor health recommendations", () => {
  const html = buildReleaseControlHtml({
    modelSettings: { ...modelSettings, provider: "ollama", model: "gemma4:latest" },
    range,
    busy: false,
    ollamaDoctor: {
      schema: "ollama_doctor.v1",
      provider: "ollama",
      config: { model: "gemma4:latest" },
      runtime: {
        api_ps: { ok: true, url: "http://127.0.0.1:11434/api/ps" },
        cli_ps: { ok: true, available: true, rows: [{ name: "gemma4:latest", processor: "100% GPU" }] },
        selected_model: {
          loaded: true,
          name: "gemma4:latest",
          model: "gemma4:latest",
          size_vram: 4 * 1024 * 1024 * 1024,
          context_length: 8192,
          processor: "100% GPU",
          expires_at: "2099-01-01T00:00:00Z",
        },
      },
      probes: [
        {
          name: "single",
          ok: true,
          elapsed_ms: 2000,
          prompt_chars: 120,
          timings: {
            model_ms: 2000,
            queue_ms: 700,
            eval_ms: 400,
          },
        },
      ],
      summary: {
        status: "warning",
        bottleneck: "queue",
        warmed_up: true,
        diagnostics: ["Ollama queue time reached 700ms during the probe."],
        recommendations: ["Set OLLAMA_NUM_PARALLEL before starting Ollama if your hardware has memory for parallel requests."],
        recommended_settings: { suggestion_parallelism: 1 },
      },
      adaptive_recommendations: {
        schema_version: "verlato.suggestion_adaptive.v1",
        recommendations: [
          {
            kind: "reduce-queueing",
            title: "Reduce Ollama queueing",
            detail: "Queue time suggests Ollama is serializing.",
            confidence: "medium",
          },
        ],
      },
    },
  });

  assert.match(html, /Model health/);
  assert.match(html, /Warm model/);
  assert.match(html, /warning \| queue \| warmed/);
  assert.match(html, /queue 700ms/);
  assert.match(html, /Runtime: gemma4:latest loaded \| ctx 8192 \| vram 4\.0GB \| 100% GPU \| expires in/);
  assert.match(html, /try parallelism 1/);
  assert.match(html, /Adaptive advice: Reduce Ollama queueing \(medium\): Queue time suggests Ollama is serializing/);
  assert.match(html, /OLLAMA_NUM_PARALLEL/);
});

test("keeps request inspection out of the release control view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 42,
        command: "verlato",
        args: ["draft", "suggest", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "[verlato:model] starting suggest provider=ollama title=Improve API",
      },
    ],
  });

  assert.doesNotMatch(html, /REQUESTS/);
  assert.doesNotMatch(html, /verlato draft suggest --all-visible/);
});

test("renders live request inspection and per-request stop controls in activity view", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 42,
        command: "verlato",
        args: ["draft", "suggest", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "[verlato:model] starting suggest provider=ollama title=Improve API",
      },
    ],
  });

  assert.match(html, /REQUESTS/);
  assert.match(html, /1 running/);
  assert.match(html, /<details class="section tree-section requests" open>/);
  assert.match(html, /<span class="badge request-status running"><span class="spinner" aria-hidden="true"><\/span><span>running<\/span><\/span>/);
  assert.match(html, /verlato draft suggest --all-visible/);
  assert.match(html, /starting suggest provider=ollama/);
  assert.match(html, /data-action="cancelRequest" data-request="42"/);
});

test("opens completed request groups in the dedicated activity view", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: false,
    requests: [
      {
        id: 45,
        command: "verlato",
        args: ["draft", "prompt"],
        label: "draft prompt",
        status: "completed",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
      },
    ],
  });

  assert.match(html, /<details class="section tree-section requests" open>/);
  assert.match(html, /<span class="badge request-status completed"><span>completed<\/span><\/span>/);
  assert.match(html, /1 recent/);
});

test("renders model activity newest first with raw output preserved", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 43,
        command: "verlato",
        args: ["draft", "suggest", "--id", "ci_1"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "[verlato:model] provider=ollama task=suggest model=gemma4 endpoint=http://127.0.0.1 prompt_chars=123 evidence_keys=4",
        activity: [
          {
            id: 1,
            stream: "stderr",
            category: "model",
            message: "[verlato:model] provider=ollama task=suggest model=gemma4 endpoint=http://127.0.0.1 prompt_chars=123 evidence_keys=4",
            receivedAt: "2026-05-03T12:00:01.000Z",
          },
          {
            id: 2,
            stream: "stderr",
            category: "model",
            message: "[verlato:model] response provider=ollama task=suggest content_chars=88 empty=False",
            receivedAt: "2026-05-03T12:00:02.000Z",
          },
          {
            id: 3,
            stream: "stderr",
            category: "model",
            message: "prompt-preview",
            event: "prompt_preview",
            attributes: { chars: 34, redacted: true },
            payload: "Summarize the changed API behavior",
            receivedAt: "2026-05-03T12:00:03.000Z",
          },
          {
            id: 4,
            stream: "stderr",
            category: "model",
            message: "[verlato:model] prompt-budget chars=1692 files=12/24 commits=1/1 hunks=0/0 pr_body_chars=0/0 diff_summary_chars=300/300",
            event: "prompt_budget",
            attributes: {
              prompt_chars: 1692,
              files_included: 12,
              files_total: 24,
              commits_included: 1,
              commits_total: 1,
              hunks_included: 0,
              hunks_total: 0,
            },
            receivedAt: "2026-05-03T12:00:04.000Z",
          },
        ],
      },
    ],
  });

  assert.match(html, /<ol class="activity">/);
  assert.match(html, /Latest activity/);
  assert.ok(html.indexOf("prompt budget files=12/24") < html.indexOf("prompt preview"));
  assert.ok(html.indexOf("prompt preview") < html.indexOf("response provider=ollama"));
  assert.match(html, /provider=ollama task=suggest model=gemma4/);
  assert.match(html, /response provider=ollama task=suggest content_chars=88/);
  assert.match(html, /prompt preview/);
  assert.match(html, /prompt budget files=12\/24 commits=1\/1 hunks=0\/0 chars=1692/);
  assert.match(html, /Payload 34 chars redacted/);
  assert.match(html, /data-copy-label="payload 34 chars redacted"/);
  assert.match(html, /Summarize the changed API behavior/);
  assert.match(html, /Raw output/);
  assert.doesNotMatch(html, /\[verlato:model\] provider=ollama task=suggest model=gemma4<\/span>/);
});

test("renders structured suggestion activity events", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 46,
        command: "verlato",
        args: ["draft", "suggest", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: [
          `[verlato:suggestion] json ${JSON.stringify({
            event: "suggestion_preview_started",
            total: 2,
            preview: { base_ref: "v1.0.0", head_ref: "HEAD", provider: "mock", count: 1, items: [] },
          })}`,
          `[verlato:suggestion] json ${JSON.stringify({
            event: "suggestion_item_result",
            order: 0,
            item_id: "ci_1",
            item: {
              item_id: "ci_1",
              title: "Improve API",
              current_summary: "Old summary",
              provider: "mock-local",
              suggestions: ["Improves API release note wording"],
            },
          })}`,
        ].join("\n"),
      },
    ],
  });

  assert.match(html, /Suggestion preview started for 2 item\(s\)/);
  assert.match(html, /Generated suggestion for ci_1/);
  assert.match(html, /<li class="suggestion">/);
});

test("renders semantic status badge colors and running spinner hooks", () => {
  const controlHtml = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    document: {
      version: "Unreleased",
      generated_on: "2026-05-03",
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      targets: [],
      rendered: {},
      item_count: 2,
      sections: [
        {
          kind: "changed",
          title: "Changed",
          items: [
            draftItem({ id: "ready", title: "Ready item", status: "ready" }),
            draftItem({ id: "review", title: "Review item", status: "needs-review" }),
          ],
        },
      ],
    },
    requests: [
      {
        id: 50,
        command: "verlato",
        args: ["draft", "preview"],
        label: "draft preview",
        status: "failed",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "failed",
      },
    ],
  });
  const activityHtml = buildReleaseActivityHtml({
    requests: [
      {
        id: 50,
        command: "verlato",
        args: ["draft", "preview"],
        label: "draft preview",
        status: "failed",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "failed",
      },
    ],
  });

  assert.match(controlHtml, /<span class="badge ready">ready<\/span>/);
  assert.match(controlHtml, /<span class="badge needs-review">needs-review<\/span>/);
  assert.match(activityHtml, /<span class="badge request-status failed"><span>failed<\/span><\/span>/);
  assert.match(activityHtml, /\.request \.badge\.completed[\s\S]*var\(--vscode-charts-green/);
  assert.match(activityHtml, /\.request \.badge\.running[\s\S]*var\(--vscode-charts-yellow/);
  assert.match(activityHtml, /\.request \.badge\.failed[\s\S]*var\(--vscode-errorForeground/);
  assert.match(activityHtml, /@keyframes verlato-spin/);
});

test("renders coalesced stream chunk activity as live blocks", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 44,
        command: "verlato",
        args: ["draft", "suggest", "--id", "ci_1"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "",
        activity: [
          {
            id: 1,
            stream: "stderr",
            category: "model",
            message: "[verlato:model] stream reasoning chunks=3 chars=18",
            event: "stream_chunk",
            attributes: { kind: "reasoning", chunks: 3, chars: 18 },
            payload: "Thinking through diff",
            receivedAt: "2026-05-03T12:00:01.000Z",
          },
          {
            id: 2,
            stream: "stderr",
            category: "model",
            message: "[verlato:model] stream content chunks=2 chars=19",
            event: "stream_chunk",
            attributes: { kind: "content", chunks: 2, chars: 19 },
            payload: "Adds model tracing",
            receivedAt: "2026-05-03T12:00:02.000Z",
          },
        ],
      },
    ],
  });

  assert.match(html, /Reasoning streaming\.\.\. 3 chunks/);
  assert.match(html, /Response streaming\.\.\. 2 chunks/);
  assert.match(html, /Reasoning stream 18 chars/);
  assert.match(html, /Response stream 19 chars/);
  assert.match(html, /<details class="payload stream-payload">/);
  assert.doesNotMatch(html, /<details class="payload stream-payload" open>/);
  assert.match(html, /<pre class="stream-scroll" tabindex="0" data-scroll-key="request-44-stream-task-reasoning">/);
  assert.match(html, /let restoringScroll = false;/);
  assert.match(html, /if \(restoringScroll\) return;/);
  assert.match(html, /\.request \.stream-payload pre \{/);
  assert.match(html, /Thinking through diff/);
  assert.match(html, /Adds model tracing/);
});

test("contributes focused Verlato views and optional walkthrough", () => {
  const views = manifest.contributes.views.verlato;

  assert.deepEqual(views.map((view) => view.id), [
    "verlato.control",
    "verlato.releases",
    "verlato.activity",
    "verlato.sources",
  ]);
  assert.equal(views[0].contextualTitle, "Verlato Control");
  assert.equal(views[1].contextualTitle, "Verlato Releases");
  assert.equal(views[2].contextualTitle, "Verlato Activity");
  assert.equal(views[3].contextualTitle, "Verlato Sources");
  assert.equal(manifest.contributes.configuration.properties["verlato.notifications.successLevel"].default, "visible");
  assert.deepEqual(manifest.contributes.configuration.properties["verlato.notifications.successLevel"].enum, [
    "visible",
    "quiet",
  ]);
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionParallelism"].default, 2);
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionParallelism"].maximum, 8);
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionBatchSize"].default, 2);
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionBatchSize"].maximum, 2);
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionAutoWarmCache"].default, true);
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionInstantProvisional"].default, true);
  assert.equal(manifest.contributes.configuration.properties["verlato.ollamaSuggestionModel"].default, "");
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionPromptProfile"].default, "conservative");
  assert.equal(manifest.contributes.configuration.properties["verlato.suggestionAdaptiveRecommendations"].default, false);
  assert.equal(manifest.contributes.walkthroughs[0].title, "Get started with Verlato");
  assert.match(manifest.contributes.walkthroughs[0].description, /Optional shortcuts/);
  assert.deepEqual(
    manifest.contributes.walkthroughs[0].steps.map((step) => step.completionEvents[0]),
    [
      "onView:verlato.control",
      "onCommand:verlato.changeModel",
      "onCommand:verlato.scanRepository",
      "onCommand:verlato.previewReleaseNotes",
    ],
  );
});

function draftItem(overrides) {
  return {
    id: overrides.id,
    title: overrides.title,
    summary: "A concise release note",
    kind: "changed",
    area: "backend",
    audience: "external",
    semver: "patch",
    confidence: 0.82,
    pr_number: null,
    commit_shas: ["abcdef123456"],
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    files: ["verlato/cli/main.py"],
    commit_signals: ["diff-fallback"],
    status: "needs-review",
    reviewed_by: null,
    reviewed_at: null,
    edited_summary: null,
    ...overrides,
  };
}
