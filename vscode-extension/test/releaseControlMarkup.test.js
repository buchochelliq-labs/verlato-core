const assert = require("node:assert/strict");
const test = require("node:test");

const { buildReleaseActivityHtml, buildReleaseControlHtml } = require("../dist/webview/releaseControlMarkup");
const manifest = require("../package.json");

const modelSettings = {
  provider: "mock",
  model: "mock-model",
  endpoint: "http://127.0.0.1:11434/api/chat",
  ollamaTimeoutSeconds: 120,
  maxModelItems: 25,
  suggestionCount: 1,
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

test("renders scan progress inside the release control view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
  });

  assert.match(html, /Scanning v1\.0\.0\.\.HEAD/);
  assert.match(html, /Scanning\.\.\./);
  assert.match(html, /data-action="cancel"/);
  assert.match(html, /Stop/);
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
  assert.match(html, /<span class="model-mark">AI<\/span>/);
  assert.match(html, /<span class="model-name">gemma4:latest<\/span>/);
  assert.match(html, /Provider: ollama/);
  assert.match(html, /Suggestions: 3/);
  assert.match(html, /Streaming: on/);
});

test("keeps repository sources out of the release control view", () => {
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
  assert.doesNotMatch(html, /PRs From Commits/);
  assert.doesNotMatch(html, /Add release control UX/);
});

test("counts visible draft items separately from hidden reviewed items", () => {
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
            draftItem({ id: "visible", title: "Visible item", hidden: false, status: "needs-review" }),
            draftItem({ id: "hidden", title: "Hidden item", hidden: true, status: "hidden" }),
          ],
        },
      ],
    },
  });

  assert.match(html, /<strong>1<\/strong><span>visible<\/span>/);
  assert.match(html, /Visible item/);
  assert.match(html, /Hidden item/);
  assert.match(html, /item hidden/);
});

test("renders scanner errors in the view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: false,
    errorMessage: "ReleasePilot CLI was not found",
  });

  assert.match(html, /notice error/);
  assert.match(html, /ReleasePilot CLI was not found/);
});

test("renders suggestion actions and review variants", () => {
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
          provider: "mock-local",
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
  });

  assert.match(html, /data-action="suggestAll"/);
  assert.match(html, /data-action="suggest" data-item="ci_1"/);
  assert.match(html, /SUGGESTIONS/);
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

test("keeps request inspection out of the release control view", () => {
  const html = buildReleaseControlHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 42,
        command: "releasepilot",
        args: ["draft", "suggest", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "[releasepilot:model] starting suggest provider=ollama title=Improve API",
      },
    ],
  });

  assert.doesNotMatch(html, /REQUESTS/);
  assert.doesNotMatch(html, /releasepilot draft suggest --all-visible/);
});

test("renders live request inspection and per-request stop controls in activity view", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 42,
        command: "releasepilot",
        args: ["draft", "suggest", "--all-visible"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "[releasepilot:model] starting suggest provider=ollama title=Improve API",
      },
    ],
  });

  assert.match(html, /REQUESTS/);
  assert.match(html, /1 running/);
  assert.match(html, /<details class="section tree-section requests" open>/);
  assert.match(html, /<span class="badge request-status running"><span class="spinner" aria-hidden="true"><\/span><span>running<\/span><\/span>/);
  assert.match(html, /releasepilot draft suggest --all-visible/);
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
        command: "releasepilot",
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
        command: "releasepilot",
        args: ["draft", "suggest", "--id", "ci_1"],
        label: "draft suggest",
        status: "running",
        startedAt: "2026-05-03T12:00:00.000Z",
        stdout: "",
        stderr: "[releasepilot:model] provider=ollama task=suggest model=gemma4 endpoint=http://127.0.0.1 prompt_chars=123 evidence_keys=4",
        activity: [
          {
            id: 1,
            stream: "stderr",
            category: "model",
            message: "[releasepilot:model] provider=ollama task=suggest model=gemma4 endpoint=http://127.0.0.1 prompt_chars=123 evidence_keys=4",
            receivedAt: "2026-05-03T12:00:01.000Z",
          },
          {
            id: 2,
            stream: "stderr",
            category: "model",
            message: "[releasepilot:model] response provider=ollama task=suggest content_chars=88 empty=False",
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
            message: "[releasepilot:model] prompt-budget chars=1692 files=12/24 commits=1/1 hunks=0/0 pr_body_chars=0/0 diff_summary_chars=300/300",
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
  assert.doesNotMatch(html, /\[releasepilot:model\] provider=ollama task=suggest model=gemma4<\/span>/);
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
        command: "releasepilot",
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
        command: "releasepilot",
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
  assert.match(activityHtml, /@keyframes releasepilot-spin/);
});

test("renders coalesced stream chunk activity as live blocks", () => {
  const html = buildReleaseActivityHtml({
    modelSettings,
    range,
    busy: true,
    requests: [
      {
        id: 44,
        command: "releasepilot",
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
            message: "[releasepilot:model] stream reasoning chunks=3 chars=18",
            event: "stream_chunk",
            attributes: { kind: "reasoning", chunks: 3, chars: 18 },
            payload: "Thinking through diff",
            receivedAt: "2026-05-03T12:00:01.000Z",
          },
          {
            id: 2,
            stream: "stderr",
            category: "model",
            message: "[releasepilot:model] stream content chunks=2 chars=19",
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
  assert.match(html, /<details class="payload stream-payload" open>/);
  assert.match(html, /Thinking through diff/);
  assert.match(html, /Adds model tracing/);
});

test("contributes focused ReleasePilot views and optional walkthrough", () => {
  const views = manifest.contributes.views.releasepilot;

  assert.deepEqual(views.map((view) => view.id), [
    "releasepilot.control",
    "releasepilot.activity",
    "releasepilot.sources",
  ]);
  assert.equal(views[0].contextualTitle, "ReleasePilot Control");
  assert.equal(views[1].contextualTitle, "ReleasePilot Activity");
  assert.equal(views[2].contextualTitle, "ReleasePilot Sources");
  assert.equal(manifest.contributes.configuration.properties["releasepilot.notifications.successLevel"].default, "visible");
  assert.deepEqual(manifest.contributes.configuration.properties["releasepilot.notifications.successLevel"].enum, [
    "visible",
    "quiet",
  ]);
  assert.equal(manifest.contributes.walkthroughs[0].title, "Get started with ReleasePilot");
  assert.match(manifest.contributes.walkthroughs[0].description, /Optional shortcuts/);
  assert.deepEqual(
    manifest.contributes.walkthroughs[0].steps.map((step) => step.completionEvents[0]),
    [
      "onView:releasepilot.control",
      "onCommand:releasepilot.changeModel",
      "onCommand:releasepilot.scanRepository",
      "onCommand:releasepilot.previewReleaseNotes",
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
    files: ["releasepilot/cli/main.py"],
    commit_signals: ["diff-fallback"],
    status: "needs-review",
    reviewed_by: null,
    reviewed_at: null,
    edited_summary: null,
    ...overrides,
  };
}
