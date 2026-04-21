# ReleasePilot Backlog

This backlog is the implementation tracker for the MVP and near-term follow-ups from the original project goal.

## Must-have MVP follow-ups

- [x] Implement a real GitHub API adapter:
  - [x] map commit SHAs to PRs
  - [x] fetch PR metadata (title/body/labels/base/head)
  - [x] fetch changed files + patch hunks
- [ ] Add collector support for scanning from tag/release markers in addition to refs.
- [x] Improve grouping for non-PR commit ranges (grouped changesets instead of single commits when appropriate).
- [x] Add smarter split logic when one PR contains unrelated user-facing changes.
- [x] Add more deterministic heuristics for audience/area classification by path and labels.
- [x] Strengthen release rendering templates and include confidence/provenance options.

## State and reconciliation

- [x] Track publication metadata by release/tag in state.
- [x] Add command to mark items as published in a specific release.
- [x] Add `reconcile` subcommand to explicitly list history deltas not yet represented.
- [ ] Track release lineage/provenance by file area so changed paths can be linked to their most recent prior release(s).
- [ ] Add checksum versioning strategy for future schema changes.

## Quality and DX

- [x] Add integration tests for CLI commands using temp git repos.
- [x] Add mocked GitHub adapter integration tests for PR enrichment.
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

## Next biggest ticket

- [x] Add smarter split logic when one PR contains unrelated user-facing changes.

## Suggested next items (recommended execution order)

1. **Smarter PR split logic** (current biggest ticket)
   - Implement heuristics to split one PR into multiple user-facing changelog items when unrelated areas are touched.
2. **Deterministic audience/area classification**
   - Add path/label mapping rules and deterministic fallback behavior for higher-confidence output.
3. **Release rendering confidence/provenance**
   - Include confidence and evidence provenance in output templates so reviewers can audit generated notes.
4. **CLI integration tests with temp git repos**
   - Validate end-to-end command behavior for realistic local workflows.
5. **Mocked GitHub adapter integration tests**
   - Lock down PR enrichment behavior and edge cases without external API dependence.
