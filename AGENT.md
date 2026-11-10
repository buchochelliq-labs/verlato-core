# Verlato Agent Guide

## Purpose

Verlato turns git history and pull-request evidence into draftable release notes. It helps
teams scan a range, review grouped changes, generate notes, and safely apply them to
repository files such as `CHANGELOG.md` and GitHub release-note drafts. Change evidence comes
from the IntentumDiff semantic diff engine.

## What Good Behavior Looks Like

- Stay PR-first. A pull request or grouped changeset is the default release-note unit.
- Prefer deterministic evidence first (Conventional Commits, labels, the semantic review), then
  provider-assisted wording and low-confidence help.
- Preserve idempotency. Re-running generation must not duplicate items in state.
- Keep repository edits safe and reviewable. Output lands in normal Markdown files.
- Treat hidden and edited items as reviewer intent, not as disposable suggestions.
- Base release notes on the semantic diff: never describe moved code, refactorings or
  formatting as new behaviour.

## Current Product Shape

- A native (Rust) runtime, `crates/verlato`, with the IntentumDiff engine linked in-process:
  the CLI, the JSON-lines editor service (`verlato service`) and the MCP server
  (`verlato serve`).
- The `verlato` Python package: a thin PyO3 wrapper (`crates/verlato-python`, `python/verlato`).
- A local state store for unpublished items, publication metadata, lineage, review state and
  feedback.
- The VS Code extension (`vscode-extension/`) for scanning, reviewing, suggesting and applying.
- Documentation in `docs/` (Zensical); see `docs/technical/architecture.md`.

## Repo Working Rules

- Keep core logic in the Rust crate; keep the Python package and the VS Code extension thin.
- Prefer shared service-layer behaviour over duplicated CLI and editor logic.
- Add tests whenever contracts change, especially CLI JSON output and state migrations.
- When changing release rendering, consider both draft flows and persisted-state render flows.
- Keep `docs/` current, regenerate `docs/reference/cli.md` with `scripts/gen_cli_reference.py`
  when commands change, and never add paid-feature code here (see `docs/open-core.md`).
