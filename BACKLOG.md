# ReleasePilot Backlog

This backlog is the implementation tracker for the MVP and near-term follow-ups from the original project goal.

## Must-have MVP follow-ups

- [x] Implement a real GitHub API adapter:
  - [x] map commit SHAs to PRs
  - [x] fetch PR metadata (title/body/labels/base/head)
  - [x] fetch changed files + patch hunks
- [x] Add collector support for scanning from tag/release markers in addition to refs.
- [x] Improve grouping for non-PR commit ranges (grouped changesets instead of single commits when appropriate).
- [x] Add smarter split logic when one PR contains unrelated user-facing changes.
- [x] Add more deterministic heuristics for audience/area classification by path and labels.
- [x] Strengthen release rendering templates and include confidence/provenance options.

## State and reconciliation

- [x] Track publication metadata by release/tag in state.
- [x] Add command to mark items as published in a specific release.
- [x] Add `reconcile` subcommand to explicitly list history deltas not yet represented.
- [x] Track release lineage/provenance by file area so changed paths can be linked to their most recent prior release(s).
- [x] Add checksum versioning strategy for future schema changes.

## Quality and DX

- [x] Add integration tests for CLI commands using temp git repos.
- [x] Add mocked GitHub adapter integration tests for PR enrichment.
- [x] Add lint/type-check tasks (`ruff`, `mypy`) in CI.
- [x] Add golden-file tests for markdown rendering.
- [x] Add sample fixtures for real-world bad commit message scenarios.

## BYOM and MCP readiness

- [x] Add provider request/response contracts for summarization and classification prompts.
- [x] Add a local/mock model adapter with deterministic outputs for tests.
- [x] Define MCP-facing service interface and wire read-only endpoints first.

## Documentation

- [x] Keep `ARCHITECTURE.md` current with any structural changes.
- [x] Keep this backlog prioritized and prune completed items regularly.
- [x] Add contributor guide with development workflow and decision log.

## Next biggest ticket

- [x] Add smarter split logic when one PR contains unrelated user-facing changes.

## Suggested next items (recommended execution order)

1. **MCP transport and auth**
   - Expose the read-only MCP service interface via a deployable server process.
2. **AI provider strategy (GitHub baked-in vs Copilot workflows)**
   - Add a provider selection mode that supports organization-managed GitHub-hosted models and Codex/Copilot-assisted workflows.
   - Document default model settings, token requirements, and local fallback behavior.
3. **Confidence calibration**
   - Improve confidence scoring with richer evidence weighting and calibration tests.
4. **State backend expansion**
   - Add optional SQLite backend for larger repositories and concurrent workflows.
