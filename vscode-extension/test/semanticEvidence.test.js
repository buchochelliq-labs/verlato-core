const assert = require("node:assert/strict");
const test = require("node:test");

const { renderSemanticEvidence, renderSemanticVerdictBadge } = require("../dist/webview/releaseControlMarkup");
const { normalizeDiffEngine, semanticEngineEnvironment } = require("../dist/client/modelEnvironment");
const manifest = require("../package.json");

function semantic(overrides = {}) {
  return {
    engine: "intentumdiff",
    engine_version: "0.0.2b2",
    verdict: "behavioral",
    complete: true,
    files_reviewed: 2,
    files_unreviewed: 0,
    group_counts: { MEANINGFUL_CHANGE: 4, MOVED_CODE: 1, IGNORED_STYLE: 1 },
    languages: ["python", "typescript"],
    style_only_files: [],
    refactor_only_files: [],
    meaningful_files: ["src/app.py"],
    added_symbols: ["fresh in src/app.py"],
    removed_symbols: ["gone in src/app.py"],
    renamed_symbols: [],
    signature_changes: ["greet in src/app.py"],
    refactorings: [],
    cross_file: [],
    guardrails: [],
    unreviewed: [],
    errors: [],
    notes: [],
    summary: "IntentumDiff semantic review: behavioral changes; 2 file(s) reviewed",
    ...overrides,
  };
}

function item(overrides = {}) {
  return {
    id: "ci_1",
    title: "Update app",
    summary: "Updates greeting",
    kind: "changed",
    area: "backend",
    audience: "external",
    semver: "patch",
    confidence: 0.7,
    pr_number: null,
    commit_shas: ["abc123456789"],
    base_ref: "v1",
    head_ref: "HEAD",
    files: ["src/app.py"],
    commit_signals: ["semantic:behavioral"],
    status: "needs-review",
    reviewed_by: null,
    reviewed_at: null,
    edited_summary: null,
    hidden: false,
    semantic: semantic(),
    diff_engine: "intentumdiff",
    ...overrides,
  };
}

test("renders IntentumDiff semantic evidence with counts and symbol changes", () => {
  const html = renderSemanticEvidence(item());
  assert.match(html, /class="semantic-evidence"/);
  assert.match(html, /Semantic review <span>behavioral \| 2 file\(s\) \| python, typescript<\/span>/);
  assert.match(html, /meaningful 4/);
  assert.match(html, /moved 1/);
  assert.match(html, /<span>Added<\/span><code title="fresh">fresh<\/code>/);
  assert.match(html, /<span>Removed<\/span><code title="gone">gone<\/code>/);
  assert.match(html, /<span>Signature<\/span><code title="greet">greet<\/code>/);
  assert.match(html, /IntentumDiff 0\.0\.2b2/);
  assert.doesNotMatch(html, /src\/app\.py<\/code>/, "symbol chips show names, not file suffixes");
});

test("flags guardrails, API-break candidates and partial reviews as alerts", () => {
  const html = renderSemanticEvidence(item({
    commit_signals: ["semantic:behavioral", "semantic:api-break-candidate"],
    semantic: semantic({
      complete: false,
      files_unreviewed: 1,
      guardrails: [{ rule_id: "protect-ports", severity: "immutable", file: "config.yaml" }],
    }),
  }));
  assert.match(html, /semantic-evidence has-alerts/);
  assert.match(html, /Protected config guardrail: protect-ports \(immutable\)/);
  assert.match(html, /confirm whether this is breaking/);
  assert.match(html, /1 file\(s\) could not be semantically reviewed/);
  assert.match(html, /partial review/);
});

test("escapes engine-provided symbol names", () => {
  const html = renderSemanticEvidence(item({ semantic: semantic({ added_symbols: ["<img src=x> in a.ts"] }) }));
  assert.doesNotMatch(html, /<img src=x>/);
  assert.match(html, /&lt;img src=x&gt;/);
});

test("renders nothing for legacy git-engine items", () => {
  assert.equal(renderSemanticEvidence(item({ semantic: null, diff_engine: "git" })), "");
  assert.equal(renderSemanticVerdictBadge(undefined), "");
});

test("verdict badge uses the semantic verdict class and summary tooltip", () => {
  const html = renderSemanticVerdictBadge(semantic({ verdict: "refactor-only" }));
  assert.match(html, /class="badge semantic semantic-refactor-only"/);
  assert.match(html, />refactor only</);
  assert.match(html, /title="IntentumDiff 0\.0\.2b2: IntentumDiff semantic review/);
});

test("maps diff engine settings to the Verlato CLI environment", () => {
  assert.equal(normalizeDiffEngine(undefined), "intentumdiff");
  assert.equal(normalizeDiffEngine("GIT"), "git");
  assert.equal(normalizeDiffEngine("something-else"), "intentumdiff");
  assert.deepEqual(semanticEngineEnvironment({}), {
    VERLATO_DIFF_ENGINE: "intentumdiff",
    VERLATO_SEMANTIC_CROSS_FILE: "0",
  });
  assert.deepEqual(semanticEngineEnvironment({ diffEngine: "git", parallelism: 3, crossFile: true }), {
    VERLATO_DIFF_ENGINE: "git",
    VERLATO_SEMANTIC_PARALLELISM: "3",
    VERLATO_SEMANTIC_CROSS_FILE: "1",
  });
  assert.equal(semanticEngineEnvironment({ parallelism: 99 }).VERLATO_SEMANTIC_PARALLELISM, "16");
  assert.equal(semanticEngineEnvironment({ parallelism: 0 }).VERLATO_SEMANTIC_PARALLELISM, undefined);
});

test("contributes IntentumDiff engine settings and status command", () => {
  const properties = manifest.contributes.configuration.properties;
  assert.equal(properties["verlato.diffEngine"].default, "intentumdiff");
  assert.deepEqual(properties["verlato.diffEngine"].enum, ["intentumdiff", "git"]);
  assert.equal(properties["verlato.semanticParallelism"].default, 0);
  assert.equal(properties["verlato.semanticCrossFile"].default, false);
  assert.ok(manifest.contributes.commands.some((command) => command.command === "verlato.showSemanticEngine"));
});
