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
- Provider abstraction with stub, mock, local Ollama-backed model support, and an optional GitHub-backed provider for summary enrichment and low-confidence classification assists.
- Versioned checksums and area-level release lineage provenance in persisted state.
- MCP tool server for agent-facing scan, draft, review, and apply operations.

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
releasepilot scan --base <ref> --head <ref>
releasepilot scan --from-latest-tag --head <ref>
releasepilot scan --from-latest-published --head <ref>
releasepilot generate --base <ref> --head <ref>
releasepilot reconcile --base <ref> --head <ref>
releasepilot draft preview --base <ref> --head <ref> --version v1.2.0
releasepilot draft preview --base <ref> --head <ref> --version v1.2.0 --model-provider ollama
releasepilot draft prompt --base <ref> --head <ref> --id <item-id>
releasepilot draft json --base <ref> --head <ref> --version v1.2.0
releasepilot draft apply --base <ref> --head <ref> --target changelog --version v1.2.0
releasepilot draft review --base <ref> --id <item-id> --status ready --reviewer alice
releasepilot serve
releasepilot render --format github-release --version v1.2.0
releasepilot render --format changelog --version v1.2.0
releasepilot state show
releasepilot state mark-published --version v1.2.0 --all-unpublished
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

See `releasepilot.sample.json` for a minimal example.

Environment variables:

- `RELEASEPILOT_STATE_PATH`: overrides local state file path.
- `RELEASEPILOT_CONFIG_PATH`: overrides the repo config file path.
- `RELEASEPILOT_PR_ADAPTER`: PR mapping mode: `standard-git` (default), `github`, or `none`.
- `RELEASEPILOT_GITHUB_REPO`: repository slug (`owner/repo`) for GitHub API enrichment.
- `RELEASEPILOT_MODEL_PROVIDER`: model provider mode: `ollama` (default), `stub`, `mock`, or `github-copilot`.
- `RELEASEPILOT_OLLAMA_MODEL`: model name when using `ollama` (default `gemma4:latest`).
- `RELEASEPILOT_OLLAMA_ENDPOINT`: override the local Ollama chat endpoint (default `http://127.0.0.1:11434/api/chat`).
- `RELEASEPILOT_MODEL_MAX_ITEMS`: maximum grouped changes before ReleasePilot disables provider calls for that scan (default `25`; use `-1` to disable the fanout guard).
- `RELEASEPILOT_GITHUB_MODEL`: model name when using `github-copilot` (default `openai/gpt-4o-mini`; legacy values such as `gpt-4o-mini` are normalized).
- `RELEASEPILOT_GITHUB_MODELS_ENDPOINT`: override chat-completions endpoint for GitHub-hosted models.
- `GITHUB_TOKEN` or `GH_TOKEN`: API token used when `RELEASEPILOT_PR_ADAPTER=github`. Also used by `github-copilot` provider mode. If neither is set, the provider falls back to `gh auth token` so it can use the logged-in GitHub CLI user.

Provider behavior:

- `stub`: deterministic no-op provider that preserves the built-in classifier summary.
- `mock`: deterministic local provider that enriches summaries and can assist low-confidence classification.
- `ollama`: local provider that calls a locally running Ollama model and keeps AI requests on your machine.
- `github-copilot`: remote provider that enriches summaries and can assist low-confidence classification.

Provider defaults can also be set in `releasepilot.json`; environment variables override these values:

```json
{
  "provider": {
    "name": "ollama",
    "model": "gemma4:latest",
    "endpoint": "http://127.0.0.1:11434/api/chat",
    "max_items_per_scan": 25
  }
}
```

When a provider is enabled, ReleasePilot sends compact file-diff evidence, representative hunks, PR context, and commit subjects to the summary prompt. If the model returns empty output, an error marker, or text that simply repeats the title/commit subject, ReleasePilot falls back to its deterministic diff summary. If a scan expands beyond `max_items_per_scan`, ReleasePilot skips provider calls for that scan to avoid hammering the selected model.

You can also override the provider for one CLI run with `--model-provider mock`, `--model-provider ollama`, or `--model-provider github-copilot` on `scan`, `generate`, `reconcile`, and `draft` commands. The VS Code extension exposes the same override through the `releasepilot.modelProvider` setting; leave it as `configured` to honor `releasepilot.json` or environment variables. The `draft suggest` command defaults to `ollama` when no provider is configured because it explicitly asks the model for a summary.

For local AI use, run Ollama locally and make sure the selected model is pulled before using `draft suggest`. For prompt debugging or manual model use, prefer `draft prompt`, which only returns the generated prompt and does not call any LLM. GitHub auth is no longer requested by default for configured/local flows.

Config targets:

- `releasepilot.json` can define draft/apply targets such as `CHANGELOG.md` and `.github/release.md`.
- See `releasepilot.sample.json` for the baseline shape.

Agent guidance:

- `AGENT.md` describes the project mission and working expectations for coding agents.
- `.github/copilot-instructions.md` gives Copilot a short workspace entrypoint and points to the deeper guidance files.
- `.codex/skills/releasepilot/SKILL.md` bundles an in-repo skill for agents working on ReleasePilot itself.

Editor integration:

- The bundled VS Code extension uses one-off `releasepilot` CLI commands for its current UI workflow.
- The repo includes [.vscode/mcp.json](.vscode/mcp.json) so VS Code/Copilot can attach to the FastMCP server from the workspace.
- The MCP server exposes these tool names:
  - `releasepilot_service_health`
  - `releasepilot_service_info`
  - `releasepilot_scan_unpublished_changes`
  - `releasepilot_get_draft_document`
  - `releasepilot_preview_release_notes`
  - `releasepilot_apply_draft`
  - `releasepilot_update_review_state`
  - `releasepilot_reconcile_preview`
- The MCP server also exposes read-only resources:
  - `releasepilot://overview`
  - `releasepilot://guidance/{name}`
  - `releasepilot://sample-config`
- The MCP server exposes the prompt `releasepilot_review_request`.

MCP setup:

- Install the optional FastMCP v3+ dependency with `python -m pip install -e .[mcp]`.
- The checked-in [.vscode/mcp.json](.vscode/mcp.json) runs `fastmcp run ${workspaceFolder}/scripts/releasepilot_mcp_server.py:mcp`.
- Start the MCP server manually with `releasepilot serve` if you are not using the workspace config.

VS Code extension local testing:

Install into VS Code from this checkout:

```powershell
.\scripts\install_vscode_extension.ps1
```

The script installs the Python backend in editable mode, installs the extension dependencies, builds the TypeScript, packages a local VSIX, and installs it into VS Code with `--force`. It uses `code.cmd` when available, which is more reliable on Windows than accidentally resolving `code` to `Code.exe`.

Useful script options:

```powershell
.\scripts\install_vscode_extension.ps1 -SkipPythonInstall
.\scripts\install_vscode_extension.ps1 -SkipNpmInstall
.\scripts\install_vscode_extension.ps1 -SkipPackage
.\scripts\install_vscode_extension.ps1 -PreRelease
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
   `code --install-extension .\vscode-extension\releasepilot-vscode-0.0.1.vsix --force`

Development host path:

1. Follow steps 1-3 above.
2. Open the repo root in VS Code and press `F5`.
   The checked-in [.vscode/launch.json](.vscode/launch.json) starts an Extension Development Host using `vscode-extension/`.
3. In the Extension Development Host, open the `ReleasePilot` activity bar view.

After installation, open a repository and use the `ReleasePilot` activity bar view. The extension expects the `releasepilot` CLI to be available through one of these paths:

1. The `releasepilot.cliPath` VS Code setting.
2. A workspace-local `.venv\Scripts\releasepilot.exe` on Windows, or `.venv/bin/releasepilot` on macOS/Linux.
3. The installed extension's `.venv`, or the checked-out repo's `.venv` when developing this extension.
4. `releasepilot` on VS Code's inherited `PATH`.

On Windows, `scripts\install_vscode_extension.ps1` installs the backend into this repo's `.venv` and writes the matching `releasepilot.cliPath` VS Code user setting so the extension can run from other repositories.

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
