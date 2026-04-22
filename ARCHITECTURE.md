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
  - Heuristic classification layer with diff fallback signals.
- `releasepilot/core/reconciler.py`
  - Reconciliation by checksum against persisted state.
- `releasepilot/core/generator.py`
  - Markdown rendering for GitHub release notes and changelog output.
- `releasepilot/state/store.py`
  - JSON persistence, schema versioning, and area lineage index.
- `releasepilot/github/adapter.py`
  - GitHub adapter boundary, including stub implementation.
- `releasepilot/providers/`
  - BYOM provider abstraction with stub + deterministic mock providers.
- `releasepilot/mcp/service.py`
  - Read-only MCP service interface over scan/reconcile/render.

## Data flow

1. CLI receives `base` and `head` refs.
2. `Collector` loads commits and optional PR mappings.
3. `Grouper` converts commit stream into PR-oriented groups.
4. `Classifier` produces `ChangeItem` objects, attaching provenance and evidence.
5. `Reconciler` filters already-known items by checksum.
6. `StateStore` persists newly generated items.
7. `MarkdownGenerator` renders organized output sections.

## Extensibility seams

- **GitHub adapter boundary** allows replacement with real API implementation and easy mocking in tests.
- **Provider boundary** allows future model-backed summarization/classification without changing core pipeline contracts.
- **Diff evidence bundle** is intentionally compact and model-friendly.

## Near-term architecture improvements

- Add explicit PR changed-file retrieval API in adapter interface.
- Add SQLite state backend option.
- Introduce richer evidence scoring and confidence calibration.
- Expand MCP service into a networked server transport.
