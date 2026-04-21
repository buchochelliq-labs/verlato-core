# ReleasePilot

ReleasePilot is an MVP changelog intelligence layer over git history and pull requests.

It is **PR-first by default**: the primary analysis unit is a pull request (or grouped change set), not an individual commit. Conventional commits are treated as a strong signal, but when commit messages are weak, ReleasePilot falls back to **diff evidence** from changed files and representative hunks.

## MVP capabilities

- Collect commits between refs/tags.
- Group commits into PR-level change sets.
- Parse conventional commits (including breaking changes).
- Build compact diff evidence for inference.
- Deterministically classify changes (kind, area, audience, semver, confidence).
- Reconcile detected changes with persisted local state (idempotent reruns).
- Render markdown for:
  - GitHub release notes
  - CHANGELOG-style entries
- Persist state in JSON.
- Provider abstraction with a stub provider for future BYOM model support.

## Architecture

`releasepilot/`

- `cli/` — CLI entrypoint and command handling.
- `core/collector.py` — collects commits and maps commits to PRs.
- `core/grouper.py` — groups commits into PR-level units.
- `core/conventional.py` — conventional commit parsing.
- `core/diff_evidence.py` — compact evidence builder from changed files/patch hunks.
- `core/classifier.py` — deterministic classification + diff fallback path.
- `core/reconciler.py` — state reconciliation (new/unpublished items only).
- `core/generator.py` — markdown rendering.
- `github/adapter.py` — GitHub adapter boundary + stub implementation.
- `providers/` — model-provider interface + stub.
- `state/store.py` — JSON persistence.
- `tests/` — unit tests.

## Change item model

Each generated `ChangeItem` includes:

- source provenance (`pr_number`, commit SHAs, base/head refs)
- classification (`kind`, `area`, `audience`, `semver`)
- content (`title`, `summary`)
- evidence (`labels`, files, commit signals, diff summary)
- confidence score
- publication metadata (`published`, `published_in`)
- checksum for idempotent reconciliation

## Diff intelligence (MVP)

`DiffEvidenceBuilder` produces a compact summary object intended for later model-backed inference:

- groups files by inferred area
- flags possible public API changes
- flags migrations/config changes
- detects docs-only and tests-only changes
- extracts representative hunks (truncated) instead of full patches


## Project planning docs

- See `ARCHITECTURE.md` for system design and module boundaries.
- See `BACKLOG.md` for prioritized work remaining from the original prompt.

## Install

```bash
python -m pip install -e .
```

## CLI usage

```bash
releasepilot scan --base <ref> --head <ref>
releasepilot generate --base <ref> --head <ref>
releasepilot render --format github-release --version v1.2.0
releasepilot render --format changelog --version v1.2.0
releasepilot state show
```

### Command behavior

- `scan`: detects unreconciled change items in a ref range (no state mutation).
- `generate`: detects and persists newly generated items to state.
- `render`: renders markdown from persisted items.
- `state show`: prints persisted state JSON.

## Config sample

See `releasepilot.sample.json` for a minimal example.

## Testing

```bash
pytest
```

## Notes on signal priority

1. Conventional commits are preferred when available.
2. PR-level grouping is the default output unit.
3. Vague commit messages trigger diff-based fallback evidence.
4. Internal-only changes are separated from customer-facing changes when detectable.
