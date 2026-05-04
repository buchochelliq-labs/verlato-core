const assert = require("node:assert/strict");
const test = require("node:test");

const { buildReleaseControlHtml } = require("../dist/webview/releaseControlMarkup");

const modelSettings = {
  provider: "mock",
  model: "mock-model",
  endpoint: "http://127.0.0.1:11434/api/chat",
  maxModelItems: 25,
  suggestionCount: 1,
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
          suggestions: ["Make API responses easier to inspect", "Refine API response details"],
        },
      ],
    },
  });

  assert.match(html, /data-action="suggestAll"/);
  assert.match(html, /data-action="suggest" data-item="ci_1"/);
  assert.match(html, /SUGGESTIONS/);
  assert.match(html, /Make API responses easier to inspect/);
  assert.match(html, /data-action="applySuggestion" data-item="ci_1" data-index="0"/);
  assert.match(html, /data-action="skipSuggestion" data-item="ci_1"/);
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
        ],
      },
    ],
  });

  assert.match(html, /<ol class="activity">/);
  assert.match(html, /provider=ollama task=suggest model=gemma4/);
  assert.match(html, /response provider=ollama task=suggest content_chars=88/);
  assert.match(html, /Raw output/);
  assert.doesNotMatch(html, /\[releasepilot:model\] provider=ollama task=suggest model=gemma4<\/span>/);
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
