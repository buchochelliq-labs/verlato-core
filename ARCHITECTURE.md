# ReleasePilot Architecture

This document captures the current MVP architecture and should be reviewed before making structural changes.

## Design principles

1. **PR-first unit of change**
   - The default release-note unit is a pull request or grouped change set.
   - Commits are supporting signals, not the primary final output unit.

2. **Signal hierarchy**
   - Conventional commits are preferred when present and high quality.
   - Diff evidence and changed-file context are fallback truth when commit messages are vague.

3. **Deterministic first, model-ready second**
   - MVP behavior is deterministic and testable.
   - Data structures include compact evidence bundles that can be fed to model providers later.

4. **Idempotent regeneration**
   - Re-running `generate` on the same history range should not duplicate items.

## Module map

- `releasepilot/cli/main.py`
  - CLI orchestration for `scan`, `generate`, `render`, `state show`.
- `releasepilot/core/collector.py`
  - Collect commits in a ref range and map commits to PRs via adapter.
- `releasepilot/core/grouper.py`
  - Group commits into PR-level groups (with no-PR fallback groups).
- `releasepilot/core/conventional.py`
  - Conventional commit parser + breaking-change detection.
- `releasepilot/core/diff_evidence.py`
  - Builds compact `DiffEvidence` from changed files and patch hunks.
- `releasepilot/core/classifier.py`
  - Heuristic classification layer with diff fallback signals and model prompt construction for summary suggestions.
- `releasepilot/core/reconciler.py`
  - Reconciliation by checksum against persisted state.
- `releasepilot/core/generator.py`
  - Markdown rendering for GitHub release notes and changelog output.
- `releasepilot/state/store.py`
  - JSON persistence, schema versioning, and area lineage index.
- `releasepilot/github/adapter.py`
  - GitHub adapter boundary, including stub implementation.
- `releasepilot/providers/`
  - BYOM provider abstraction with stub, deterministic mock, Ollama, GitHub-hosted model, and model-log sink support.
- `vscode-extension/`
  - Editor UX for draft review, suggestion preview/regeneration, tone selection, realtime model telemetry, and apply flows.
- `releasepilot/mcp/service.py`
  - Shared service interface used by MCP tools.
- `releasepilot/mcp/server.py`
  - MCP tool registration and stdio server entrypoint for VS Code/Copilot.

## Data flow

1. CLI receives `base` and `head` refs.
2. `Collector` loads commits and optional PR mappings.
3. `Grouper` converts commit stream into PR-oriented groups.
4. `Classifier` produces `ChangeItem` objects, attaching provenance and evidence.
5. `Reconciler` filters already-known items by checksum.
6. `StateStore` persists newly generated items.
7. `MarkdownGenerator` renders organized output sections.
8. `releasepilot/mcp/server.py` exposes agent-facing MCP tools over stdio for Copilot and other MCP clients.

For model-assisted suggestion previews, `draft suggest --preview` builds a bounded prompt from PR metadata, changed-file evidence, representative hunks, kind/audience context, the selected tone preset, and optional project language controls. Project style controls include guidance text, preferred vocabulary, banned phrases, and an opt-in specificity floor. The CLI emits realtime model telemetry to stderr and optional JSONL debug logs. VS Code consumes those events as a request timeline, lets users regenerate one item or all visible suggestions, records lightweight local feedback, and passes summary provenance back through `draft review` when a suggestion is applied.

## Extensibility seams

- **GitHub adapter boundary** allows replacement with real API implementation and easy mocking in tests.
- **Provider boundary** allows future model-backed summarization/classification without changing core pipeline contracts.
- **Diff evidence bundle** is intentionally compact and model-friendly.
- **Suggestion tone** is explicit CLI/config/editor state, so prompt style changes are reproducible and recorded in applied-summary provenance.
- **Project language controls** stay explicit in config/env/editor settings and are copied into applied-summary provenance for auditability.
- **Feedback events** live in `.releasepilot_state.json` as lean records by default. Full text is opt-in, and export is handled by the dedicated `feedback export` command.
- **Model telemetry export** normalizes CLI and provider debug logs into a structured JSON document while preserving default redaction.
- **MCP tool surface** gives agent clients a stable, discoverable set of release-note operations.

## Near-term architecture improvements

- Add explicit PR changed-file retrieval API in adapter interface.
- Add OTLP export for model logs.
- Use local feedback aggregates to suggest prompt/style adjustments without persisting raw prompt payloads by default.
- Add DuckDB/Delta-backed analytical storage for larger repositories and feedback/model telemetry exploration.
- Introduce richer evidence scoring and confidence calibration.
- Expand the local stdio server into a richer multi-client or networked transport.
