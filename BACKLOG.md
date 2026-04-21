# ReleasePilot Backlog

This backlog is the implementation tracker for the MVP and near-term follow-ups from the original project goal.

## Must-have MVP follow-ups

- [ ] Implement a real GitHub API adapter:
  - [ ] map commit SHAs to PRs
  - [ ] fetch PR metadata (title/body/labels/base/head)
  - [ ] fetch changed files + patch hunks
- [ ] Add collector support for scanning from tag/release markers in addition to refs.
- [ ] Improve grouping for non-PR commit ranges (grouped changesets instead of single commits when appropriate).
- [ ] Add smarter split logic when one PR contains unrelated user-facing changes.
- [ ] Add more deterministic heuristics for audience/area classification by path and labels.
- [ ] Strengthen release rendering templates and include confidence/provenance options.

## State and reconciliation

- [ ] Track publication metadata by release/tag in state.
- [ ] Add command to mark items as published in a specific release.
- [ ] Add `reconcile` subcommand to explicitly list history deltas not yet represented.
- [ ] Track release lineage/provenance by file area so changed paths can be linked to their most recent prior release(s).
- [ ] Add checksum versioning strategy for future schema changes.

## Quality and DX

- [ ] Add integration tests for CLI commands using temp git repos.
- [ ] Add mocked GitHub adapter integration tests for PR enrichment.
- [ ] Add lint/type-check tasks (`ruff`, `mypy`) in CI.
- [ ] Add golden-file tests for markdown rendering.
- [ ] Add sample fixtures for real-world bad commit message scenarios.

## BYOM and MCP readiness

- [ ] Add provider request/response contracts for summarization and classification prompts.
- [ ] Add a local/mock model adapter with deterministic outputs for tests.
- [ ] Define MCP-facing service interface and wire read-only endpoints first.

## Documentation

- [ ] Keep `ARCHITECTURE.md` current with any structural changes.
- [ ] Keep this backlog prioritized and prune completed items regularly.
- [ ] Add contributor guide with development workflow and decision log.
