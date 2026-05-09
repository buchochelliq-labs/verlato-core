# Verlato Backlog

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
- [x] Add LiteLLM as the recommended BYOM provider path.
- [x] Add an audited external-command model plugin contract for unsupported runtimes.
- [x] Compute and record request/prompt/evidence/schema hashes for plugin calls.
- [x] Enforce plugin `request_hash` echo and fail missing/mismatched responses.
- [x] Document the tamper limit: hashes catch drift/basic tampering, not malicious lying plugins.
- [x] Support JSON Schema plugin config metadata for future generic editor rendering.
- [x] Add explicit provider fallback chains; no silent cloud fallback.
- [ ] Add richer VS Code UI for generic plugin `config_schema` rendering and env-var secret hints.
- [ ] Add optional MCP/tool-call model-provider bridge once a concrete interoperable runtime needs it.
- [x] Define MCP-facing service interface and wire read-only endpoints first.
- [ ] Add MCP/tool-call diff retrieval for oversized suggestion payloads.

## Documentation

- [x] Keep `ARCHITECTURE.md` current with any structural changes.
- [x] Keep this backlog prioritized and prune completed items regularly.
- [x] Add contributor guide with development workflow and decision log.

## Next biggest ticket

- [ ] Add DuckDB/Delta-backed analytical storage for larger repositories and feedback/model telemetry exploration.

## Detailed remaining work (current view)

1. **Project language controls**
   - [x] Add vocabulary hints, banned phrases, and project-specific release-note style guidance.
   - [x] Add opt-in specificity scoring so generated text can reject vague candidates.
   - [x] Capture lightweight local feedback that can improve future prompts without persisting sensitive payloads by default.
   - [x] Use feedback aggregates to suggest prompt/style adjustments.
2. **Service-first editor integration**
   - Replace remaining CLI fallback flows in the extension with a richer local service contract.
   - Add connection lifecycle states so VS Code can show when the service is starting, ready, degraded, or unavailable.
3. **Changelog taxonomy and release-note intelligence**
   - [x] Expand the built-in changelog sections beyond `Added`, `Changed`, and `Fixed` to include breaking changes, removals, deprecations, security, docs, performance, refactors, tests, build, internal work, and other changes.
   - [ ] Add configurable section taxonomy, ordering, aliases, and display titles in `verlato.json`, with Keep a Changelog-style defaults.
   - [ ] Detect and preserve existing changelog section conventions when applying drafts to a target file.
   - [ ] Keep breaking changes protected: never silently hide them behind internal/test/docs filters, and always expose their major-version impact in draft/review surfaces.
   - [ ] Add release-note fragment support inspired by Changesets/Towncrier-style workflows, so explicit human-written snippets can override or seed inferred summaries.
   - [ ] Layer classification evidence in a predictable order: release fragments, PR labels, conventional commits, file status/path signals, diff evidence, then model suggestions.
   - [ ] Add strictness modes for unconventional histories: require conventional commits, bucket unconventional changes under `Other`, or allow diff-first fallback inference.
   - [ ] Include draft/review stats by changelog section, SemVer impact, author, scope, PR count, hidden item count, and suggestion provenance.
4. **Scale and collaboration**
   - Add auth/session strategy for non-local or multi-client MCP transports.
   - Add optional DuckDB/Delta-backed analytical storage for larger repositories and feedback/model telemetry exploration.
5. **Audited BYOM plugins**
   - [x] Keep prompts, diff evidence, schemas, validation, and provenance internal to Verlato.
   - [x] Run third-party plugins out of process with stdin/stdout JSON, stderr diagnostics, timeout, minimal env, and no repo cwd.
   - [x] Add tests for valid plugin responses, bad JSON, nonzero exit, timeout, stderr diagnostics, and hash mismatch.
   - [x] Add advisory `doctor model` probe for the selected suggestion model.
   - [ ] Surface plugin health/config metadata in VS Code settings.
   - [ ] Add a signed or trusted-plugin story if third-party plugins become distributed packages.
6. **Diff-first suggestion quality**
   - [x] Remove/reject the no-model `instant` suggestion path.
   - [x] Fail suggestion generation when no diff patch evidence is available.
   - [x] Record full-diff payload metrics and provider audit hashes in suggestion provenance metadata.
   - [x] Use first-parent commit collection for release ranges.
   - [x] Merge adjacent non-PR commits touching overlapping substantive files into one changeset.
   - [ ] Add PR-aware grouping through GitHub API metadata for squash/merge cases without `(#123)` subjects.
   - [ ] Add an oversized diff retrieval loop over MCP/tools for payloads that exceed model context.

## Suggested next items (recommended execution order)

1. **Full-diff structured suggestion batching**
   - [x] Send all available changed-file patches, commits, PR body, labels, file metadata, current summaries, and classification context to the suggestion LLM.
   - [x] Batch multiple items in one structured JSON request when the full uncapped payload fits.
   - [x] Avoid silently replacing full diff evidence with capped representative hunks for the full-diff strategy.
2. **MCP/tool-call diff retrieval for oversized suggestions**
   - Expose tools such as `list_suggestion_items`, `get_item_diff`, and `get_file_patch`.
   - Let a model request specific diff chunks only when full-diff JSON cannot fit in context.
   - Keep this as a follow-up because tool loops add round trips and may hurt local Ollama latency.
3. **Configurable changelog taxonomy and existing-section detection**
   - Add `verlato.json` settings for section order, aliases, and display titles.
   - Detect existing changelog headings such as `Added`, `Changed`, `Removed`, `Deprecated`, `Security`, and `Breaking Changes` before applying drafts.
   - Preserve project-specific section names while keeping canonical internal kinds for classification and drafts.
4. **Protected breaking-change review**
   - Ensure breaking changes remain visible even when filters hide internal, docs, tests, or low-confidence items.
   - Surface major-version impact in CLI JSON, rendered drafts, and the VS Code review panel.
   - Add regression tests for breaking-change labels, conventional `!`, and footer/body signals.
5. **Release-note fragments and evidence precedence**
   - Support checked-in release-note fragments for teams that want explicit human-authored entries.
   - Define precedence between fragments, PR labels, conventional commits, path/status heuristics, diff evidence, and model suggestions.
   - Show when a summary came from a fragment versus inference or a generated suggestion.
6. **Draft/review section analytics**
   - Add counts by changelog section, SemVer impact, author, scope, PR count, hidden count, and suggestion source to draft JSON.
   - Render those stats compactly in CLI output and VS Code so reviewers can spot missing categories before applying.
7. **Service-first editor integration**
   - Replace remaining CLI-backed VS Code flows with a richer local service contract.
   - Add connection lifecycle states so VS Code can show starting, ready, degraded, and unavailable service states.
8. **Feedback-driven prompt tuning**
   - Use accepted, rejected, skipped, and regenerated suggestion feedback to recommend project style and prompt adjustments.
   - Surface safe aggregate recommendations without persisting raw prompt or response text by default.
9. **Scale backend**
   - Add optional DuckDB/Delta-backed analytical storage for larger repositories, release lineage exploration, feedback events, and model telemetry.
10. **Model log export maturity**
   - Add OTLP export for provider/model telemetry so model-call diagnostics can feed standard observability tooling.
11. **BYOM plugin UX**
   - Render plugin `config_schema` in VS Code.
   - Add model health warnings for slow cold starts, slow warm responses, invalid schema output, and timeouts.
   - Keep LiteLLM as the documented default recommendation, with plugins reserved for unsupported runtimes/auth paths.

## Previous next items

1. **MCP transport and auth**
   - [x] Expose the local service interface via a stdio server process.
   - [x] Expose an MCP stdio server with named scan/draft/review/apply tools for agent clients.
   - [ ] Add auth/session strategy for non-local or multi-client transports.
2. **AI provider strategy (GitHub baked-in vs Copilot workflows)**
   - Add a provider selection mode that supports organization-managed GitHub-hosted models and Codex/Copilot-assisted workflows.
   - Document default model settings, token requirements, and local fallback behavior.
3. **Generated suggestion quality and observability**
   - [x] Default generated suggestions to one "best" suggestion while keeping explicit multi-suggestion overrides.
   - [x] Improve prompt guidance around "what changed + why it matters."
   - [x] Include kind/audience context in suggestion prompts.
   - [x] Add a descriptiveness floor and reject vague or overly short suggestions.
   - [x] Prefer concrete active verbs for release-note wording.
   - [x] Trace lifecycle stages: target selection, evidence build, prompt build, provider call, parse, filter, accept.
   - [x] Log candidate rejection reasons and compact evidence stats.
   - [x] Consider a `--trace-suggestions` alias for discoverability.
   - [x] Show compact generation details in the VS Code output channel.
   - [x] Adjust VS Code copy for single-suggestion review.
   - [x] Add regenerate and tone-refinement actions.
   - [x] Show current vs suggested summary before applying.
   - [x] Preserve advanced multi-suggestion mode.
   - [x] Store provider/model provenance for applied suggestions.
   - [x] Add preview source evidence and too-similar detection.
   - [x] Add provider response metadata and expanded model-call diagnostics.
   - [x] Capture model-exposed reasoning fields in provider responses and trace logs.
   - [x] Keep provider reasoning collapsed by default in the VS Code detail view.
   - [x] Log final suggestion decision context, including accepted text and rejected candidate counts.
   - [x] Add OpenTelemetry-style pluggable model log sink abstraction for provider traces.
   - [x] Add opt-in prompt/response payload telemetry with default-on redaction.
   - [x] Add realtime Ollama stream telemetry with coalesced VS Code response/reasoning chunks.
   - [x] Add prompt budget telemetry for prompt-size tuning.
   - [x] Add optional structured JSON export for model logs.
   - [ ] Add OTLP export for model logs.
   - [x] Add bad-output tests and prompt golden tests.
   - [x] Add explicit tone presets for suggestion prompts.
   - [x] Add project vocabulary hints, banned phrases, specificity scoring, and style guidance.
   - [x] Add local feedback loops for accepted/rejected wording.
   - [x] Use local feedback to recommend prompt/style adjustments.
4. **Confidence calibration**
   - Improve confidence scoring with richer evidence weighting and calibration tests.
5. **State backend expansion**
   - Add optional DuckDB/Delta-backed analytical storage for larger repositories and feedback/model telemetry exploration.
6. **Service-first editor integration**
   - Replace CLI fallbacks in the extension with a richer local service contract and connection lifecycle UX.
