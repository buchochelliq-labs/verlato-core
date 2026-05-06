# Verlato

Verlato is an MVP changelog intelligence layer over git history and pull requests.

It is **PR-first by default**: the primary analysis unit is a pull request (or grouped change set), not an individual commit. Conventional commits are treated as a strong signal, but when commit messages are weak, Verlato falls back to **diff evidence** from changed files and representative hunks.

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
- Provider abstraction with stub, mock, local Ollama-backed model support, and an optional GitHub-backed provider for summary enrichment and low-confidence classification assists.
- Versioned checksums and area-level release lineage provenance in persisted state.
- MCP tool server for agent-facing scan, draft, review, and apply operations.

## Architecture

`verlato/`

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
- `mcp/service.py` — shared service interface for MCP and local transports.
- `mcp/server.py` — MCP tool registration and stdio server entrypoint.
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
- checksum version for safe schema evolution
- area lineage (`lineage_releases`) to show prior releases for similar file areas

## Diff intelligence (MVP)

`DiffEvidenceBuilder` produces compact release-note evidence for deterministic summaries and model-backed inference:

- groups files by inferred area
- records changed file statuses
- flags possible public API changes
- flags migrations/config changes
- detects docs-only and tests-only changes
- extracts representative added/removed hunk lines (truncated) instead of full patches


## Project planning docs

- See `ARCHITECTURE.md` for system design and module boundaries.
- See `BACKLOG.md` for prioritized work remaining from the original prompt.

## Install

```bash
python -m pip install -e .
```

## CLI usage

```bash
verlato scan --base <ref> --head <ref>
verlato scan --from-latest-tag --head <ref>
verlato scan --from-latest-published --head <ref>
verlato generate --base <ref> --head <ref>
verlato reconcile --base <ref> --head <ref>
verlato draft preview --base <ref> --head <ref> --version v1.2.0
verlato draft preview --base <ref> --head <ref> --version v1.2.0 --model-provider ollama
verlato draft prompt --base <ref> --head <ref> --id <item-id>
verlato draft json --base <ref> --head <ref> --version v1.2.0
verlato draft apply --base <ref> --head <ref> --target changelog --version v1.2.0
verlato draft review --base <ref> --id <item-id> --status ready --reviewer alice
verlato serve
verlato render --format github-release --version v1.2.0
verlato render --format changelog --version v1.2.0
verlato state show
verlato state mark-published --version v1.2.0 --all-unpublished
```

### Command behavior

- `scan`: detects unreconciled change items in a ref range (no state mutation).
- `generate`: detects and persists newly generated items to state.
- `render`: renders markdown from persisted items.
- `draft preview`: renders release notes directly from the requested range.
- `draft json`: returns a structured draft document for editor and automation clients.
- `draft prompt`: builds the exact summary prompt for one draft item without sending it to any model.
- `draft apply`: updates a configured or explicit release-note target file.
- `draft review`: persists review metadata such as status, hidden state, reviewer, and edited summary for a draft item.
- `serve`: starts the FastMCP stdio server.
- `reconcile`: explicitly lists unpublished deltas for a ref range (no state mutation).
- `state show`: prints persisted state JSON.
- `state mark-published`: marks items as published for a given release/tag.
- For `scan`/`generate`/`reconcile`/`draft`, you can provide either explicit refs (`--base` + optional `--head`) or marker-based range selection:
  - `--from-latest-tag`: base is resolved from the latest reachable git tag.
  - `--from-latest-published`: base is resolved from the most recent `published_in` marker in state.

## Config sample

See `verlato.sample.json` for a minimal example.

Environment variables:

- `VERLATO_STATE_PATH`: overrides local state file path.
- `VERLATO_CONFIG_PATH`: overrides the repo config file path.
- `VERLATO_PR_ADAPTER`: PR mapping mode: `standard-git` (default), `github`, or `none`.
- `VERLATO_GITHUB_REPO`: repository slug (`owner/repo`) for GitHub API enrichment.
- `VERLATO_MODEL_PROVIDER`: model provider mode: `ollama` (default), `stub`, `mock`, or `github-copilot`.
- `VERLATO_OLLAMA_MODEL`: model name when using `ollama` (default `gemma4:latest`).
- `VERLATO_OLLAMA_ENDPOINT`: override the local Ollama chat endpoint (default `http://127.0.0.1:11434/api/chat`).
- `VERLATO_OLLAMA_TIMEOUT_SECONDS`: timeout for each local Ollama model request (default `15`).
- `VERLATO_OLLAMA_STREAM`: set to `1` to stream Ollama response chunks into model telemetry while preserving the final response.
- `VERLATO_OLLAMA_KEEP_ALIVE`: Ollama keep-alive value to avoid reloading the model between nearby requests (default `10m`; set empty to use Ollama's default behavior).
- `VERLATO_OLLAMA_NUM_CTX`: optional Ollama context-window override for local requests.
- `VERLATO_OLLAMA_NUM_PREDICT`: optional global output-token cap for Ollama requests.
- `VERLATO_OLLAMA_SUMMARY_NUM_PREDICT`: summary output-token cap when no global cap is set (default `180`).
- `VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT`: classification output-token cap when no global cap is set (default `24`).
- `VERLATO_OLLAMA_SUGGEST_NUM_PREDICT`: suggestion-specific output-token cap; defaults adaptively to `max(96, min(180, 60 * suggestion_count))`.
- `VERLATO_OLLAMA_SUGGEST_THINK`: set to `1` to allow Ollama thinking/reasoning on suggestion requests; defaults to `0`.
- `VERLATO_MODEL_MAX_ITEMS`: maximum grouped changes before Verlato disables provider calls for that scan (default `25`; use `-1` to disable the fanout guard).
- `VERLATO_SUGGESTION_PARALLELISM`: maximum `draft suggest --preview` item suggestions to run at once (CLI/config default `1`, VS Code default `2`, clamped to `1`-`8`).
- `VERLATO_SUGGESTION_CACHE_BACKEND`: suggestion cache backend name (default `json`; additional backends can be registered by Python integrations).
- `VERLATO_SUGGESTION_CACHE_PATH`: overrides the persistent suggestion cache file (default `.verlato_suggestion_cache.json`; set it to an OS cache directory path if you prefer cache files outside the repo).
- `VERLATO_SUGGESTION_CACHE_TTL_DAYS`: number of days before suggestion cache entries expire (default `30`; key changes always invalidate entries first).
- `VERLATO_GITHUB_MODEL`: model name when using `github-copilot` (default `openai/gpt-4o-mini`; legacy values such as `gpt-4o-mini` are normalized).
- `VERLATO_GITHUB_MODELS_ENDPOINT`: override chat-completions endpoint for GitHub-hosted models.
- `GITHUB_TOKEN` or `GH_TOKEN`: API token used when `VERLATO_PR_ADAPTER=github`. Also used by `github-copilot` provider mode. If neither is set, the provider falls back to `gh auth token` so it can use the logged-in GitHub CLI user.

Provider behavior:

- `stub`: deterministic no-op provider that preserves the built-in classifier summary.
- `mock`: deterministic local provider that enriches summaries and can assist low-confidence classification.
- `ollama`: local provider that calls a locally running Ollama model and keeps AI requests on your machine.
- `github-copilot`: remote provider that enriches summaries and can assist low-confidence classification.

Provider defaults can also be set in `verlato.json`; environment variables override these values:

```json
{
  "provider": {
    "name": "ollama",
    "model": "gemma4:latest",
    "endpoint": "http://127.0.0.1:11434/api/chat",
    "keep_alive": "10m",
    "summary_num_predict": 180,
    "classify_num_predict": 24,
    "suggest_num_predict": null,
    "suggest_think": false,
    "max_items_per_scan": 25
  }
}
```

When a provider is enabled, Verlato sends compact file-diff evidence, representative hunks, PR context, and commit subjects to the summary prompt. Ollama requests keep the selected model warm for nearby calls and cap generated output by task, which helps local scans avoid reload and over-generation costs. Suggestion previews disable Ollama thinking by default and use an adaptive output cap so all-visible suggestion runs avoid spending tokens on private reasoning or oversized completions. If the model returns empty output, an error marker, or text that simply repeats the title/commit subject, Verlato falls back to its deterministic diff summary. If a scan expands beyond `max_items_per_scan`, Verlato skips provider calls for that scan to avoid hammering the selected model.

You can also override the provider for one CLI run with `--model-provider mock`, `--model-provider ollama`, or `--model-provider github-copilot` on `scan`, `generate`, `reconcile`, and `draft` commands. The VS Code extension exposes the same override through the `verlato.modelProvider` setting; leave it as `configured` to honor `verlato.json` or environment variables. The `draft suggest` command defaults to `ollama` when no provider is configured because it explicitly asks the model for a summary.

`draft suggest` generates one suggested description by default. Use `--count`, `VERLATO_SUGGESTION_COUNT`, `suggestion_count` in `verlato.json`, or the VS Code `verlato.suggestionCount` setting to request up to five variants. Use `--parallelism`, `VERLATO_SUGGESTION_PARALLELISM`, `suggestion_parallelism`, or the VS Code `verlato.suggestionParallelism` setting to run independent preview suggestions concurrently. Use `--tone`, `VERLATO_SUGGESTION_TONE`, `suggestion_tone`, or `verlato.suggestionTone` to choose `release-note`, `concise`, `technical`, or `customer-facing` wording. Project language controls can be set with `suggestion_style` in config, `VERLATO_STYLE_GUIDANCE`, `VERLATO_PROJECT_VOCABULARY`, `VERLATO_BANNED_PHRASES`, `VERLATO_SPECIFICITY_FLOOR`, or the matching VS Code settings. Banned phrase matches are rejected from suggestion candidates; specificity rejection is opt-in by setting a floor above `0`. Preview suggestions use the built-in `json` cache backend by default, persisted at `.verlato_suggestion_cache.json` after prompt preparation, so repeated runs can skip the provider when item checksum, prompt hash, provider/model, tone, style, count, and prompt profile still match. The cache backend is abstracted behind `verlato.state.suggestion_cache.register_suggestion_cache_backend`, so future integrations can plug in DiskCache, SQLite, or OS-cache-backed stores while preserving the CLI flow. Use `--refresh-cache` to bypass reads and overwrite entries, `--no-cache` to bypass reads and writes, or `verlato state clear-suggestion-cache` to remove the active cache. The VS Code release control view can regenerate all visible suggestions or regenerate a single item after changing tone; regenerate requests refresh the cache. Set `VERLATO_TRACE_MODEL_CALLS=1` or pass `draft suggest --trace-suggestions` to log model-call and suggestion parsing diagnostics to stderr without changing JSON stdout.

Ollama server-side concurrency is controlled by the Ollama process, not by Verlato. If suggestion diagnostics show queue time while `suggestionParallelism` is above `1`, set `OLLAMA_NUM_PARALLEL` before starting Ollama and size it carefully with available memory; `OLLAMA_MAX_QUEUE` controls the server request queue, `OLLAMA_CONTEXT_LENGTH` affects memory use per parallel request, and `VERLATO_OLLAMA_KEEP_ALIVE` keeps the selected model loaded between nearby calls.

Run `verlato doctor ollama` for a tiny suggestion-style health probe that reports model load, queue, prompt eval, and generation timing as JSON. Add `--parallel-probe` to send two concurrent probes and check whether Ollama appears to serialize requests. The VS Code Intent Inference panel exposes the same check as Model health and shows compact recommendations.

Model telemetry is ephemeral in the VS Code request timeline by default. Full prompt and response payloads are hidden unless `verlato.telemetry.includePromptPayloads` or `VERLATO_TRACE_MODEL_PAYLOADS=1` is enabled, because prompts can include PR text and diff snippets. Set `verlato.telemetry.debugModelLogPath` or `VERLATO_MODEL_LOG_PATH` to persist JSONL model telemetry for debugging; provider log records use an OpenTelemetry-style shape so they can later be exported to JSON/OTLP pipelines. Verlato captures provider-exposed reasoning fields when available, but does not assume every provider exposes private model reasoning. See [docs/model-telemetry.md](docs/model-telemetry.md) for the event schema.

Reasoning payloads are controlled separately with `verlato.telemetry.includeReasoningPayloads` or `VERLATO_TRACE_MODEL_REASONING=1`. When disabled, Verlato still reports whether reasoning was exposed and how many characters were produced, but it does not stream or return the full reasoning text in editor-facing telemetry.

Payload redaction is enabled by default whenever payload telemetry is shown or persisted. Use `verlato.telemetry.redactPayloads=false` or `VERLATO_REDACT_MODEL_PAYLOADS=0` only for deep local debugging when you are comfortable with raw prompt and response text being visible in telemetry.

When a generated suggestion is applied, Verlato stores summary provenance with the draft item, including source, provider/model, prompt hash when available, tone, style profile, response metadata, and apply timestamp. Draft JSON includes this provenance so model-assisted release-note text remains auditable after review.

Verlato also records lightweight local feedback in `.verlato_state.json` under `feedback_events`. Apply/edit actions are inferred automatically, and VS Code records skip, regenerate, useful, and not-useful actions. By default feedback stores hashes and metrics instead of full summary text. Set `feedback_include_text=true` or `VERLATO_FEEDBACK_INCLUDE_TEXT=1` only when you explicitly want accepted/edited summary text persisted. Export feedback with `verlato feedback export --output feedback.json`, or run `verlato feedback recommend` to get safe aggregate prompt/style recommendations without reading raw prompt or response payloads.

Persisted debug logs can be exported with `verlato model-logs export --file <model-debug.jsonl> --output model-export.json`. The export normalizes CLI and provider records into a `verlato.model_logs.v1` JSON document and reapplies redaction by default.

For local AI use, run Ollama locally and make sure the selected model is pulled before using `draft suggest`. For prompt debugging or manual model use, prefer `draft prompt`, which only returns the generated prompt and does not call any LLM. GitHub auth is no longer requested by default for configured/local flows.

Config targets:

- `verlato.json` can define draft/apply targets such as `CHANGELOG.md` and `.github/release.md`.
- See `verlato.sample.json` for the baseline shape.

Agent guidance:

- `AGENT.md` describes the project mission and working expectations for coding agents.
- `.github/copilot-instructions.md` gives Copilot a short workspace entrypoint and points to the deeper guidance files.
- `.codex/skills/verlato/SKILL.md` bundles an in-repo skill for agents working on Verlato itself.

Editor integration:

- The bundled VS Code extension uses one-off `verlato` CLI commands for its current UI workflow.
- The repo includes [.vscode/mcp.json](.vscode/mcp.json) so VS Code/Copilot can attach to the FastMCP server from the workspace.
- The MCP server exposes these tool names:
  - `verlato_service_health`
  - `verlato_service_info`
  - `verlato_scan_unpublished_changes`
  - `verlato_get_draft_document`
  - `verlato_preview_release_notes`
  - `verlato_apply_draft`
  - `verlato_update_review_state`
  - `verlato_reconcile_preview`
- The MCP server also exposes read-only resources:
  - `verlato://overview`
  - `verlato://guidance/{name}`
  - `verlato://sample-config`
- The MCP server exposes the prompt `verlato_review_request`.

MCP setup:

- Install the optional FastMCP v3+ dependency with `python -m pip install -e .[mcp]`.
- The checked-in [.vscode/mcp.json](.vscode/mcp.json) runs `fastmcp run ${workspaceFolder}/scripts/verlato_mcp_server.py:mcp`.
- Start the MCP server manually with `verlato serve` if you are not using the workspace config.

VS Code extension local testing:

Install into VS Code from this checkout:

```powershell
.\scripts\install_vscode_extension.cmd
```

The wrapper runs the PowerShell installer with a process-local execution-policy bypass, which avoids the common Windows "running scripts is disabled" failure. The script installs the Python backend in editable mode, installs the extension dependencies, builds the TypeScript, packages a local VSIX, and installs it into VS Code with `--force`. It uses `code.cmd` when available, which is more reliable on Windows than accidentally resolving `code` to `Code.exe`.

Useful script options:

```powershell
.\scripts\install_vscode_extension.cmd -SkipPythonInstall
.\scripts\install_vscode_extension.cmd -SkipNpmInstall
.\scripts\install_vscode_extension.cmd -SkipPackage
.\scripts\install_vscode_extension.cmd -PreRelease
```

Manual install path:

1. Install the Python package for the repo:
   `python -m pip install -e .`
2. Install the extension dependencies:
   `cd vscode-extension && npm install`
3. Build the extension:
   `npm run build`
4. Package the extension:
   `npx @vscode/vsce package`
5. Install the generated VSIX:
   `code --install-extension .\vscode-extension\verlato-vscode-0.0.1.vsix --force`

Development host path:

1. Follow steps 1-3 above.
2. Open the repo root in VS Code and press `F5`.
   The checked-in [.vscode/launch.json](.vscode/launch.json) starts an Extension Development Host using `vscode-extension/`.
3. In the Extension Development Host, open the `Verlato` activity bar view.

After installation, open a repository and use the `Verlato` activity bar view. The extension expects the `verlato` CLI to be available through one of these paths:

1. The `verlato.cliPath` VS Code setting.
2. A workspace-local `.venv\Scripts\verlato.exe` on Windows, or `.venv/bin/verlato` on macOS/Linux.
3. The installed extension's `.venv`, or the checked-out repo's `.venv` when developing this extension.
4. `verlato` on VS Code's inherited `PATH`.

On Windows, `scripts\install_vscode_extension.ps1` installs the backend into this repo's `.venv` and writes the matching `verlato.cliPath` VS Code user setting so the extension can run from other repositories.

To exercise the MCP server in the same workspace, ensure `fastmcp>=3` is installed and keep [.vscode/mcp.json](.vscode/mcp.json) enabled.

## Testing

```bash
python -m pytest
```

## Notes on signal priority

1. Conventional commits are preferred when available.
2. PR-level grouping is the default output unit.
3. Vague commit messages trigger diff-based fallback evidence and summaries.
4. Internal-only changes are separated from customer-facing changes when detectable.
