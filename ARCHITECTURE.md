# Verlato Architecture

This document captures the current MVP architecture and should be reviewed before making structural changes.

## Design principles

1. **PR-first unit of change**
   - The default release-note unit is a pull request or grouped change set.
   - Commits are supporting signals, not the primary final output unit.

2. **Signal hierarchy**
   - Conventional commits are preferred when present and high quality.
   - IntentumDiff semantic evidence (meaningful / refactoring / moved / style-only changes and symbol-level edits) is
     fallback truth when commit messages are vague; changed-file paths and hunks are supporting context.

3. **Deterministic first, model-ready second**
   - MVP behavior is deterministic and testable.
   - Data structures include compact evidence bundles that can be fed to model providers later.

4. **Idempotent regeneration**
   - Re-running `generate` on the same history range should not duplicate items.

## Module map

Verlato is implemented in Rust (`crates/verlato`); the Python package is a thin wrapper over it.

- `crates/verlato/src/cli.rs` (`verlato::cli::run`)
  - The command line (`scan`, `generate`, `reconcile`, `render`, `draft *`, `state *`, `feedback *`, `doctor *`,
    `commit conventional`, `model-logs export`, `service`, `serve`), shared by the executable and the bindings.
- `crates/verlato/src/git.rs`, `grouper.rs`
  - Collect commits in a ref range, map commits to PRs (standard git, or the GitHub API adapter), and group commits
    into PR-level groups (with no-PR fallback groups and conventional-commit parsing).
- `crates/verlato/src/engine.rs`, `semantic.rs`
  - The IntentumDiff engine, linked in-process: reviews each commit (`api::review_sources` per changed file, in
    parallel), reduces it to a privacy-safe digest, caches digests per commit, and aggregates group-level semantic
    evidence (verdict, group counts, symbols, guardrails).
- `crates/verlato/src/evidence.rs`, `classifier.rs`, `assist.rs`
  - Compact diff evidence led by the semantic evidence, heuristic classification with diff fallback signals, and the
    model prompts, privacy records and audits for summaries and suggestions.
- `crates/verlato/src/pipeline.rs`, `drafts.rs`, `store.rs`
  - Reconciliation by checksum, draft documents and Markdown rendering, JSON state persistence and lineage.
- `crates/verlato/src/providers/`
  - BYOM providers: stub, deterministic mock, Ollama, LiteLLM, GitHub Models, the external-command plugin, Codex CLI,
    Claude Code, fallback chains, and model-log sinks.
- `crates/verlato/src/suggest.rs`, `adaptive.rs`, `suggestion_cache.rs`, `suggestion_tools.rs`, `intents.rs`
  - Suggestions in every strategy, adaptive recommendations, the suggestion cache, retrieval tools, and commit intent
    inference / Conventional Commit drafting.
- `crates/verlato/src/service.rs`
  - The JSON-lines stdio service used by the VS Code extension (`verlato service`), and in-process calls for the
    Python bindings.
- `crates/verlato/src/mcp.rs`
  - The MCP server on stdio (`verlato serve`) for Copilot and other MCP clients.
- `crates/verlato-python/`, `python/verlato/`
  - The `verlato` Python package: the `verlato._native` PyO3 module and the `verlato`/`verlato-mcp` commands; the
    wheel ships the IntentumDiff parser bundle.
- `vscode-extension/`
  - Editor UX for draft review, suggestion preview/regeneration, tone selection, realtime model telemetry, and apply flows.

## Data flow

1. CLI receives `base` and `head` refs.
2. `Collector` loads commits and optional PR mappings, and the IntentumDiff engine attaches a semantic digest to every
   commit (cached per `parent..sha`).
3. `Grouper` converts commit stream into PR-oriented groups.
4. `Classifier` produces `ChangeItem` objects, attaching provenance and evidence.
5. `Reconciler` filters already-known items by checksum.
6. `StateStore` persists newly generated items.
7. `MarkdownGenerator` renders organized output sections.
8. `verlato serve` (`crates/verlato/src/mcp.rs`) exposes agent-facing MCP tools over stdio for Copilot and other MCP clients.

For suggestion previews, `draft suggest --preview` is diff-first and model-backed. The removed `instant` path is rejected because suggestions must be generated from Verlato-built diff payloads. `auto`, `paired`, and `full-diff-json` use structured JSON batches for capable providers such as Ollama, LiteLLM, and audited plugins; each uncached item includes full available changed-file patches, PR metadata, commits, labels, kind/audience context, the selected tone preset, and optional project language controls. The bounded single-item prompt path remains available as an explicit fallback. Project style controls include guidance text, preferred vocabulary, banned phrases, and an opt-in specificity floor. The CLI emits realtime model telemetry to stderr and optional JSONL debug logs. VS Code consumes those events as a request timeline, lets users regenerate one item or all visible suggestions, records lightweight local feedback, and passes summary provenance back through `draft review` when a suggestion is applied. External BYOM plugins run out of process with JSON stdin/stdout, minimal environment, no repo cwd, enforced timeout, and request/prompt/evidence/schema hashes recorded in provider metadata.

## Extensibility seams

- **GitHub adapter boundary** allows replacement with real API implementation and easy mocking in tests.
- **Provider boundary** allows future model-backed summarization/classification without changing core pipeline contracts.
- **Diff engine boundary** (`diff_engine`) selects IntentumDiff (default) or legacy git-patch evidence; IntentumDiff is
  consumed only through its public `CommitDiffer` API, so engine upgrades do not touch the pipeline.
- **Diff evidence bundle** is intentionally compact and model-friendly.
- **Suggestion tone** is explicit CLI/config/editor state, so prompt style changes are reproducible and recorded in applied-summary provenance.
- **Project language controls** stay explicit in config/env/editor settings and are copied into applied-summary provenance for auditability.
- **Feedback events** live in `.verlato_state.json` as lean records by default. Full text is opt-in, and export is handled by the dedicated `feedback export` command.
- **Model telemetry export** normalizes CLI and provider debug logs into a structured JSON document while preserving default redaction.
- **MCP tool surface** gives agent clients a stable, discoverable set of release-note operations.
- **Built-in editor service** reuses the MCP service layer for local VS Code draft/review/state operations, while model-heavy streaming flows can continue through direct CLI commands until they have a service event transport.

## Near-term architecture improvements

- Add explicit PR changed-file retrieval API in adapter interface.
- Add OTLP export for model logs.
- Use local feedback aggregates to suggest prompt/style adjustments without persisting raw prompt payloads by default.
- Add DuckDB/Delta-backed analytical storage for larger repositories and feedback/model telemetry exploration.
- Introduce richer evidence scoring and confidence calibration.
- Expand the local service and MCP transports into richer multi-client or networked transports.
