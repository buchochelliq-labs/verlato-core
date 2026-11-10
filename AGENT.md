# Verlato Agent Guide

## Purpose

Verlato turns git history and pull-request evidence into draftable release notes.
The project exists to help teams scan a range, review grouped changes, generate notes,
and safely apply those notes back into repo-managed files such as `CHANGELOG.md` and
GitHub release-note drafts.

## What Good Behavior Looks Like

- Stay PR-first when possible. A pull request or grouped changeset is the default note unit.
- Prefer deterministic evidence first, then provider-assisted wording and low-confidence help.
- Preserve idempotency. Re-running generation should not duplicate items in state.
- Keep repo edits safe and reviewable. Generated output should land in normal Markdown files.
- Treat hidden and edited items as reviewer intent, not as disposable suggestions.

## Current Product Shape

- Native (Rust) backend for scan, generate, reconcile, render, and draft workflows, also shipped as the `verlato` Python package.
- Local state store for unpublished items, publication metadata, lineage, and review state.
- MCP-facing service layer for editor and agent integrations.
- VS Code extension scaffold for scan, preview, apply, and review actions.

## Preferred Next Steps

1. Add stronger review workflows and richer editor actions.
2. Expose a real local service transport so the extension stops shelling out to the CLI.
3. Expand config and target support for multi-file release-note strategies.
4. Improve confidence calibration and evidence explanations.

## Repo Working Rules

- Keep core logic in the Rust crate (`crates/verlato`); keep the Python package and the VS Code extension thin.
- Prefer shared service-layer behavior over duplicated CLI and editor logic.
- Add tests whenever contracts change, especially for CLI JSON output and state migration.
- When changing release rendering, consider both draft flows and persisted-state render flows.
