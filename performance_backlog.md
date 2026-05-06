# Verlato Performance Backlog

This backlog tracks performance work separately from product, quality, and integration backlog items. Use it for slow scans, sluggish editor flows, expensive provider calls, and scaling work that needs measurement before feature changes.

## Current priority

- [ ] Establish a repeatable performance baseline for representative repositories:
  - [ ] small repo with a handful of PRs
  - [ ] medium repo with hundreds of commits
  - [ ] large repo with long release history and many changed files
- [ ] Add timing instrumentation around the main slow paths:
  - [ ] git history collection
  - [ ] PR enrichment and adapter calls
  - [ ] diff evidence extraction
  - [ ] grouping and classification
  - [ ] draft generation
  - [ ] VS Code service/client round trips
- [ ] Capture performance expectations for interactive editor flows, especially scan, draft, review, regenerate, and apply.

## Investigation queue

1. **Git and collection**
   - [ ] Profile `StandardGitAdapter` operations for repeated subprocess calls.
   - [ ] Batch or cache git lookups that are currently repeated per commit, PR, or file.
   - [ ] Avoid fetching or parsing patch data before it is needed by downstream evidence builders.
   - [ ] Check whether tag/release marker scans are walking more history than required.
2. **Diff evidence**
   - [ ] Measure cost of changed-file and hunk parsing on large PRs.
   - [ ] Add size limits or progressive loading for very large diffs.
   - [ ] Cache derived evidence for unchanged commits/PRs within a scan session.
3. **Classification and grouping**
   - [ ] Profile deterministic classification over large changesets.
   - [ ] Reduce repeated path/label/audience scoring work.
   - [ ] Add tests for pathological cases with many small commits or very large PRs.
4. **Provider calls**
   - [ ] Track prompt construction time separately from model latency.
   - [ ] Use prompt budget telemetry to identify overlarge evidence payloads.
   - [ ] Add guardrails for large batches so slow model calls degrade predictably.
5. **VS Code extension**
   - [ ] Replace remaining CLI-backed flows with service calls where this reduces startup or repeated process overhead.
   - [ ] Surface scan progress and partial results so long-running work feels responsive.
   - [ ] Measure extension command latency from user action to rendered webview update.

## Candidate fixes

- [ ] Introduce an in-memory scan session cache for commit, PR, changed-file, and evidence lookups.
- [ ] Add lazy evidence expansion: collect summaries first, fetch full patches only for selected items.
- [ ] Add concurrency controls for independent adapter calls, with rate-limit-safe defaults.
- [ ] Persist reusable release lineage and publication metadata in the planned analytical storage backend.
- [ ] Add a `--profile` or `--trace-performance` mode that emits compact timing summaries.
- [ ] Add benchmark-style tests that run without network access and catch obvious regressions.

## Metrics to track

- [ ] Time to scan from a release marker to HEAD.
- [ ] Time to enrich commits with PR metadata.
- [ ] Time to build evidence for a draft release.
- [ ] Time to produce generated suggestions, split by prompt build and provider latency.
- [ ] VS Code command latency for scan, draft, regenerate, and apply.
- [ ] Number of git subprocess calls per scan.
- [ ] Number of provider calls per draft/review action.

## Open questions

- [ ] What repo sizes should define the small, medium, and large performance targets?
- [ ] Which user flow feels slowest right now: CLI scan, CLI draft, VS Code scan, VS Code review, or model-backed regeneration?
- [ ] Should the first performance pass optimize local-only deterministic flows before model-backed flows?
- [ ] What latency target should the extension meet before showing progress, partial results, or cancellation controls?
