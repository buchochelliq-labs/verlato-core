# Semantic evidence

Verlato's change evidence comes from [IntentumDiff](https://github.com/buchochelliq-labs/intentumdiff-core),
a semantic diff engine that parses both sides of every changed file (with WebAssembly parsers
for dozens of languages and file formats) and compares their structure rather than their
text. Verlato links the engine in-process (`intentumdiff-rust-core`, pinned in `Cargo.toml`)
and reviews every collected commit as `parent..sha`, each changed file in parallel.

## What a review gives Verlato

For each commit, Verlato keeps a **digest**:

- **change groups**: meaningful changes, behaviour-preserving refactorings, moved code,
  formatting that was ignored, and suppressed noise;
- **symbol changes**: functions, methods and classes added, removed, renamed or with a changed
  signature;
- **guardrail violations**: changes to protected settings declared in the repository's
  `intentumdiff.yaml`;
- **unreviewed files**, with the engine's reason (they are reported, never silently dropped).

Only structure and definition names are kept: no source bodies, literal values or
IntentumDiff's raw change descriptions. Digests are cached per commit in
`.verlato_semantic_cache.json` (commits are immutable, so later runs only review new
commits). Files the engine cannot parse fall back to path and status evidence.

## Verdicts

The digests of a changeset's commits (restricted to its files) are folded into a verdict:

| Verdict | Meaning | Effect when no Conventional Commit or label decides |
|---|---|---|
| `style-only` | formatting or noise only | kind `internal`, SemVer `none`, confidence at least 0.88 |
| `refactor-only` | moves and refactorings, no meaningful edits | kind `refactoring`, SemVer `patch`, confidence at least 0.82 |
| `additive` | only additions | kind `added`, SemVer `minor` when symbols were added |
| `behavioral` | meaningful edits, removals or signature changes | kind `changed` |
| `unreviewed` | nothing could be reviewed | no effect |

Authored Conventional Commits and labels keep precedence. The review adds signals on top,
recorded in the item's `commit_signals`:

| Signal | When | Effect |
|---|---|---|
| `semantic:<verdict>` | always | |
| `semantic:partial` | some files could not be reviewed | |
| `semantic:api-break-candidate` | public-looking symbols removed, renamed or re-signatured outside tests | confidence capped below 0.8, so `needs-review` |
| `semantic:guardrail` | a protected setting changed | confidence capped below 0.8, so `needs-review` |
| `semantic:conflict` | a `feat:`, `fix:` or `perf:` commit whose changes are formatting only | confidence capped at 0.7, so `needs-review` |

## What models see

When Verlato asks a model to write (summaries, suggestions in every strategy, `draft prompt`,
commit intent inference and Conventional Commit drafting), it sends IntentumDiff's **semantic
diff**, not the raw patch. For each file the model gets the meaningful changes with their
enclosing symbol, the refactorings and moved code, and the formatting that was ignored:

```text
Semantic diff (IntentumDiff, structure):
src/taskboard/tasks.py (python, modified) @6ecd58bfa5ac:
  - meaningful in add: addition expression_statement; addition if_statement
src/taskboard/tasks.py (python, modified) @25865576cf24:
  - refactoring RENAME_SYMBOL: open_tasks -> pending: refactoring function_definition open_tasks -> pending
  - ignored as formatting/noise (noise:1)
src/taskboard/tasks.py (python, modified) @249231ac5bdd:
  - meaningful in import_from_statement: addition import_from_statement
  - meaningful in Task: addition expression_statement
  - meaningful in Board: addition function_definition overdue
```

(That is the "fix: reject tasks with empty titles" changeset of the demo repository used for
the screenshots: three commits, one of them the rename, one a vague `wip`.)

The prompt tells the model to base the entry on this semantic diff and never to present moved
code, refactorings or formatting as new behaviour. Tool-calling models get
`verlato_get_file_semantic_diff` and `verlato_search_semantic_diff` instead of patch tools,
and the `full-diff-json` strategy sends a `semantic_diff` per file instead of a `patch`.

The [privacy mode](../guide/privacy.md) sets the detail level:

- Under `metadata-only` (default), the semantic diff carries structure and identifier names
  only.
- When the mode lets the provider see source content (`local-summary-then-ai` with a local
  model, or `trusted-provider-patch`), it also includes IntentumDiff's change descriptions
  and, for meaningful edits, the changed statement before and after (at most 120 characters
  each), for example
  `` modification assignment start (was `start = page * per_page + 1`, now `start = page * per_page`) ``.
  Moved sub-expressions inside a meaningful edit are folded into that edit.
- Raw patches are only sent with the legacy `git` engine.

Prompt budgets and model request records report `evidence_source` and
`semantic_diff_detail`, so you can see which evidence a request used.

## The legacy `git` engine

With `diff_engine: "git"` there is no semantic review. Evidence is compact path and patch
evidence: files grouped by area, change statuses, possible public API changes, migrations and
config changes, docs-only and tests-only changes, and a few representative hunk lines per
file. It needs no parser bundle.

## Engine versions

`verlato doctor engine` reports the linked engine build (`intentumdiff-core@<revision>`) and
where its parser bundle was found. The parser bundle must match the engine revision:
`scripts/fetch_intentumdiff_parsers.sh` pins the bundle, and both are updated together (see
[Releasing](releasing.md#the-parser-bundles-pin)).
