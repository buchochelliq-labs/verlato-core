# Output formats

Commands print JSON (pretty-printed) or Markdown on stdout. Diagnostics, progress and model
telemetry go to stderr, so stdout can always be parsed.

## Draft documents

`verlato draft json`, the service's `draft.get` and the MCP `verlato_get_draft_document` return:

```json
{
  "version": "v1.1.0",
  "generated_on": "2026-11-10",
  "repository_url": null,
  "base_ref": "v1.0.0",
  "head_ref": "HEAD",
  "targets": [
    {"name": "changelog", "path": "CHANGELOG.md", "format": "changelog"},
    {"name": "github-release", "path": ".github/release.md", "format": "github-release"}
  ],
  "sections": [
    {"kind": "added", "title": "Added", "items": [ "...draft items..." ]}
  ],
  "rendered": {"changelog": "### v1.1.0 (2026-11-10)\n...", "github-release": "## v1.1.0 - 2026-11-10\n..."},
  "item_count": 4
}
```

`repository_url` is the web URL of the `origin` remote, when it has one (the extension links
PRs, commits and files with it). Sections follow the order Breaking Changes, Added,
Changed, Deprecated, Removed, Fixed, Security, Documentation, Performance, Refactoring,
Tests, Build, Internal, Other.

### Draft items

| Field | Meaning |
|---|---|
| `id` | stable item ID (`ci_` + 12 hex digits) |
| `title`, `summary` | the entry; `summary` is the edited summary when there is one |
| `original_summary` | the generated summary |
| `summary_source`, `classification_source` | human-readable: where the summary and the classification came from |
| `evidence_summary` | one-line description of the changed-file evidence |
| `kind`, `area`, `audience`, `semver` | the classification ([Concepts](../guide/concepts.md#from-commits-to-release-notes)) |
| `confidence` | 0 to 1 |
| `pr_number`, `commit_shas`, `base_ref`, `head_ref` | provenance |
| `files` | changed paths |
| `commit_signals` | signals behind the classification, such as `conventional:fix`, `semantic:additive`, `semantic:api-break-candidate`, `diff-fallback` |
| `derived_conventional_commits` | Conventional Commit metadata inferred for the item's commits |
| `grouping_reason` | why these commits form one changeset |
| `status`, `hidden` | the [review status](../guide/concepts.md#review-status) |
| `reviewed_by`, `reviewed_at`, `edited_summary` | the reviewer's decision |
| `summary_provenance` | for applied suggestions: provider, model, prompt hash, tone, style, response metadata and time |
| `diff_engine` | `intentumdiff` or `git` |
| `semantic` | the IntentumDiff review (below), or `null` with the `git` engine |

### The semantic block

```json
{
  "engine": "intentumdiff",
  "engine_version": "intentumdiff-core@7ae58e9237e0",
  "verdict": "additive",
  "complete": true,
  "files_reviewed": 1,
  "files_unreviewed": 0,
  "group_counts": {"MEANINGFUL_CHANGE": 4, "REFACTORING": 1, "NOISE_SUPPRESSED": 1},
  "languages": ["python"],
  "meaningful_files": ["src/taskboard/tasks.py"],
  "style_only_files": [],
  "refactor_only_files": [],
  "added_symbols": ["overdue in src/taskboard/tasks.py"],
  "removed_symbols": [],
  "renamed_symbols": ["open_tasks -> pending in src/taskboard/tasks.py"],
  "signature_changes": [],
  "refactorings": ["RENAME_SYMBOL"],
  "cross_file": [],
  "guardrails": [],
  "unreviewed": [],
  "errors": [],
  "notes": [],
  "summary": "IntentumDiff semantic review: additive changes only; ...",
  "extended_signatures": []
}
```

`verdict` is one of `behavioral`, `additive`, `refactor-only`, `style-only` and `unreviewed`
(see [Semantic evidence](../technical/semantic-evidence.md)).

## Rendered Markdown

The `github-release` format (`draft preview`, `render --format github-release`):

```markdown
## v1.1.0 - 2026-11-10

### Added
- Merge pull request #12 from demo/feature/csv-export (PR #12): Export a board as CSV ...

### Fixed
- fix: reject tasks with empty titles: ...
```

The `changelog` format uses `### v1.1.0 (2026-11-10)` as the release heading. With
`--include-confidence` / `--include-provenance`, each entry ends with
`[confidence 0.74; source v1.0.0..HEAD]`.

## Other JSON documents

| Command | Shape |
|---|---|
| `scan`, `generate` | an array of change items as stored in state |
| `reconcile` | `{"unpublished": [...items], "count": n}` |
| `draft apply` | `{"target", "path", "format", "action": "create" or "update", "bytes_written"}` |
| `draft review` | the updated change item |
| `draft suggest --preview` | the run (`provider`, `privacy`, `strategy`, `tone`, `performance`, `batch_count`, ...) and per-item `items` with candidates, evidence and the audit record |
| `draft prompt` | the prompt, evidence, privacy record and audit (`ai_request`) for one item |
| `commit conventional` | `header`, `body`, `message`, `type`, `scope`, `breaking`, `confidence`, `privacy`, `ai_request` |
| `state show` | `{"items": [...]}`; the file itself (`.verlato_state.json`, `schema_version: "v3"`) also holds `feedback_events` and `area_release_lineage` |
| `feedback export` | `schema_version: "verlato.feedback.v1"` and the events (or JSON lines) |
| `feedback recommend` | `schema_version: "verlato.feedback_recommendations.v1"` |
| `model-logs export` | `schema_version: "verlato.model_logs.v1"` |
| `doctor engine` | engine availability |
| `doctor ollama` | `schema: "ollama_doctor.v1"` |
| `doctor model` | `schema: "model_doctor.v1"` |

## Stderr lines

These lines are a stable interface: the VS Code extension parses them.

```text
[verlato:progress] json {"event":"progress","stage":"...","label":"...","current":1,"total":4,"unit":"item"}
[verlato:model] <message>
[verlato:model] json {"event":"prompt_built","attributes":{...},"message":"..."}
[verlato:suggestion] json {"event":"...", ...}
```

`[verlato:progress]` lines appear with `VERLATO_TRACE_PROGRESS=1`; `[verlato:model]` and
`[verlato:suggestion]` lines with `VERLATO_TRACE_MODEL_CALLS=1`. Event names and fields are
listed in [Model telemetry](../technical/model-telemetry.md).
