# Architecture

Verlato is one Rust program, the `verlato` crate, with the IntentumDiff semantic diff engine
linked in-process. The command line, the editor service, the MCP server and the Python
package are all front ends over the same library.

```text
                 ┌──────────────── crates/verlato (library + `verlato` binary) ────────────────┐
 verlato CLI ───►│ cli.rs ──► pipeline.rs ──► git.rs / grouper.rs ──► engine.rs + semantic.rs  │
 VS Code ──────► │ service.rs (JSON lines)        classifier.rs, evidence.rs, assist.rs        │
 MCP clients ──► │ mcp.rs (MCP)                   drafts.rs, store.rs                          │
 Python ───────► │ (via crates/verlato-python)    providers/, suggest.rs, intents.rs           │
                 └─────────────────────────────────────────────────────────────────────────────┘
```

## Design principles

1. **Pull-request-first.** The unit of a release note is a pull request or a grouped
   changeset, not a commit. Commits are supporting signals.
2. **Evidence hierarchy.** Authored Conventional Commits and PR labels first; then the
   IntentumDiff semantic review (meaningful, refactoring, moved and style-only changes, and
   symbol-level edits); then changed-file paths and statuses.
3. **Deterministic first, models second.** Classification and summaries are deterministic and
   testable. Models only improve wording and help with low-confidence classifications, under
   an explicit privacy mode, and their output is checked.
4. **Idempotent.** Re-running over the same range never duplicates items; review decisions
   survive.
5. **Reviewable output.** Results land in ordinary Markdown files in the repository.
6. **Thin front ends.** Business logic lives in the crate. The extension, the Python package
   and the MCP server do not reimplement it.

## Module map

| Module | Responsibility |
|---|---|
| `cli.rs` | the command line (`verlato::cli::run` / `run_with`), shared by the executable and the bindings |
| `pipeline.rs` | the `Context` for a repository: config, ranges, scans, drafts, reviews, apply, publish, feedback |
| `config.rs` | `verlato.json` and its defaults |
| `git.rs`, `grouper.rs` | collecting commits in a range, mapping commits to PRs (standard git, the GitHub REST API, or an extension's forge), and grouping commits into changesets |
| `engine.rs`, `semantic.rs` | the IntentumDiff engine: per-commit reviews (each file in parallel), privacy-safe digests, the per-commit cache, and group-level semantic evidence |
| `evidence.rs`, `classifier.rs`, `vocab.rs` | compact diff evidence, deterministic classification (kind, area, audience, SemVer, confidence) and the shared vocabulary (sections, checksums, areas, Conventional Commits) |
| `assist.rs` | model assistance: prompts and prompt budgets, privacy records, request audits, the style profile |
| `drafts.rs`, `store.rs` | draft documents and Markdown rendering; the JSON state file and its migrations |
| `providers/` | model providers: stub, mock, Ollama, LiteLLM, GitHub Models, plugins, the Codex and Claude Code CLIs, fallback chains, HTTP, subprocesses and model-log sinks |
| `suggest.rs`, `adaptive.rs`, `suggestion_cache.rs`, `suggestion_tools.rs` | `draft suggest` in every strategy, adaptive recommendations, the suggestion cache, and the retrieval tools models and MCP clients use |
| `intents.rs` | commit intent inference and Conventional Commit drafting |
| `maintenance.rs` | `doctor`, `feedback export/recommend`, `model-logs export`, cache clearing |
| `service.rs` | the JSON-lines editor service (`verlato service`) and in-process calls for Python |
| `mcp.rs`, `mcp_catalog.json` | the MCP server (`verlato serve`) and its tool, resource and prompt catalog |
| `extensions.rs` | [extension points](extending.md) for embedding binaries |
| `pyjson.rs` | byte-exact Python `json.dumps` output, so hashes and audits stay identical to earlier releases |

Around the crate:

- `crates/verlato-python` and `python/verlato`: the PyO3 bindings (`verlato._native`) and the
  thin Python package; the wheel ships the parser bundle.
- `vscode-extension/`: the TypeScript extension. `client/` runs the CLI and the service,
  `webview/` renders the Control, Releases and Activity views and the preview panel, `tree/`
  the Sources view.

## Data flow

1. A front end asks for a range (`base..head`).
2. `git.rs` collects the commits and their changed files; the PR adapter maps commits to pull
   requests.
3. With the `intentumdiff` engine, `engine.rs` reviews every commit that is not cached
   (`parent..sha`, each file in parallel) and attaches a digest: change groups, symbol edits,
   guardrails and unreviewed files. Only structure and identifier names are kept.
4. `grouper.rs` turns the commits into changesets: one per PR, plus groups of non-PR commits
   by Conventional Commit type and scope or by overlapping files.
5. `classifier.rs` turns each changeset into a change item, with evidence from
   `evidence.rs` and the semantic verdict, optionally asking a model (through `assist.rs` and
   a provider) for a better summary.
6. `pipeline.rs` reconciles the items with state by checksum, applying saved review
   decisions.
7. `drafts.rs` builds the draft document and renders Markdown; `draft apply` writes it into a
   target file.

Suggestions (`suggest.rs`) start from the same items, build model requests in the chosen
[strategy](../guide/models.md#suggestion-strategies), cache the answers, and record their
provenance when applied.

## The VS Code extension

The extension keeps no business logic. With `verlato.serviceMode` set to `auto` (the
default), it starts one long-running `verlato service` and sends it draft, review, state,
feedback and suggestion-evidence requests; if the service cannot start, it runs one CLI
command per action and shows a degraded status. Model-heavy flows (`draft suggest`, the
Ollama doctor and warm-up) run as CLI commands so their progress and telemetry can stream
from stderr into the Activity view. Repository activity (branches, tags, recent commits) comes
from git directly.

## Persistence

| File | Written by | Contents |
|---|---|---|
| `.verlato_state.json` | `store.rs` | items, review decisions, publication, lineage, feedback (`schema_version` `v3`; older versions are migrated on load) |
| `.verlato_semantic_cache.json` | `semantic.rs` | per-commit semantic digests, with the engine and digest versions they were made with |
| `.verlato_suggestion_cache.json` | `suggestion_cache.rs` | model suggestions, keyed by item checksum, prompt hash, provider, model and settings |

The suggestion cache is written atomically (a temporary file renamed into place) under a file
lock, so concurrent runs do not corrupt it.
