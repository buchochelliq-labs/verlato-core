const assert = require("node:assert/strict");
const test = require("node:test");

const { buildReleaseControlHtml } = require("../dist/webview/releaseControlMarkup");

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

test("renders repository sources after refresh even before a draft exists", () => {
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
  assert.match(html, /PRs From Commits/);
  assert.match(html, /Add release control UX/);
  assert.match(html, /v1\.0\.0/);
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

test("renders live request inspection and per-request stop controls", () => {
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

  assert.match(html, /REQUESTS/);
  assert.match(html, /1 running/);
  assert.match(html, /releasepilot draft suggest --all-visible/);
  assert.match(html, /starting suggest provider=ollama/);
  assert.match(html, /data-action="cancelRequest" data-request="42"/);
});

test("renders model activity as a timeline with raw output preserved", () => {
  const html = buildReleaseControlHtml({
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

test("renders coalesced stream chunk activity as live blocks", () => {
  const html = buildReleaseControlHtml({
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
