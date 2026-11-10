# verlato.json

Verlato reads `verlato.json` from the repository root (or the file named by
`VERLATO_CONFIG_PATH`). Every key is optional; without the file, the defaults below apply.
[Environment variables](environment.md) override the file, and command-line options override
both. A complete example is [`verlato.sample.json`](https://github.com/buchochelliq-labs/verlato-core/blob/main/verlato.sample.json)
(also served to MCP clients as `verlato://sample-config`).

```json
{
  "diff_engine": "intentumdiff",
  "model_privacy": "metadata-only",
  "provider": {"name": "ollama", "model": "gemma4:latest"},
  "suggestion_tone": "release-note",
  "suggestion_style": {"project_vocabulary": ["Release Control"], "banned_phrases": ["various improvements"]},
  "targets": {
    "changelog": {"path": "CHANGELOG.md", "format": "changelog"},
    "github-release": {"path": ".github/release.md", "format": "github-release"}
  }
}
```

## Evidence and state

| Key | Default | Meaning |
|---|---|---|
| `state_path` | `.verlato_state.json` | where items, review decisions, publication and feedback are kept |
| `diff_engine` | `intentumdiff` | `intentumdiff` (semantic review) or `git` (legacy text patches) |
| `semantic_cache_path` | `.verlato_semantic_cache.json` | per-commit semantic review cache |
| `semantic_parallelism` | up to 4 | threads for reviewing uncached commits |
| `commit_intent_inference` | `off` | `low-signal` infers Conventional Commit intent for vague commits ([details](../guide/models.md#commit-intent-inference)) |
| `feedback_include_text` | `false` | keep accepted and edited summary text in feedback events |

## Models

| Key | Default | Meaning |
|---|---|---|
| `model_privacy` | `metadata-only` | `metadata-only`, `local-summary-then-ai` or `trusted-provider-patch` ([Privacy modes](../guide/privacy.md)) |
| `provider` | `{"name": "ollama"}` | the model provider; see below |

### provider

| Key | Applies to | Meaning |
|---|---|---|
| `name` | all | `stub`, `mock`, `ollama` (default), `litellm`, `plugin`, `github-copilot`, `codex-cli`, `claude-code` |
| `model` | all | the model (Ollama default `gemma4:latest`; LiteLLM default `ollama/gemma4:latest`) |
| `suggest_model` | Ollama, LiteLLM, plugin | a different model for suggestions only |
| `endpoint` | Ollama, LiteLLM, GitHub Models | the chat endpoint |
| `timeout_seconds` | all | per request (Ollama 15, LiteLLM and plugins 30, CLI agents 120) |
| `api_key_env` | LiteLLM | name of the environment variable holding the API key |
| `keep_alive` | Ollama | how long Ollama keeps the model loaded (`10m`; `null` for Ollama's default) |
| `num_ctx` | Ollama | context window |
| `num_predict` | Ollama, LiteLLM | global output cap (LiteLLM: `max_tokens`, default 1024; `0` sends none) |
| `summary_num_predict`, `classify_num_predict`, `suggest_num_predict` | Ollama | per-task output caps (180, 24, adaptive) |
| `suggest_think` | Ollama | allow thinking on suggestion requests (`false`) |
| `max_items_per_scan` | all | above this many changesets, model calls are skipped for the scan (25; `-1` disables) |
| `plugin_manifest` | plugin | `id`, `display_name`, `command`, `config_schema`, `capabilities` |
| `plugin_command` (or `command`) | plugin | the command, as a list or a shell-style string (overrides the manifest's) |
| `plugin_config` | plugin | passed to the plugin as `config` |
| `plugin_env` | plugin | names of environment variables passed through to the plugin |
| `fallbacks` | all | providers to try in order when this one fails; each object inherits these settings and overrides what it names ([details](../guide/models.md#fallback-chains)) |

## Suggestions

| Key | Default | Meaning |
|---|---|---|
| `suggestion_strategy` | `auto` | `auto`, `single`, `paired`, `full-diff-json`, `tool-retrieval` ([strategies](../guide/models.md#suggestion-strategies)) |
| `suggestion_count` | 1 | suggestions per item (1-5) |
| `suggestion_parallelism` | 1 | items suggested at once (1-8) |
| `suggestion_batch_size` | 5 | items per structured request (1-5) |
| `suggestion_prompt_profile` | `conservative` | `conservative`, `lean` or `minimal` |
| `suggestion_adaptive_recommendations` | `false` | include tuning advice in suggestion previews and `doctor ollama` |
| `suggestion_tone` | `release-note` | `release-note`, `concise`, `technical` or `customer-facing` |
| `suggestion_style` | | `guidance`, `project_vocabulary`, `banned_phrases`, `specificity_floor` ([Writing style](../guide/style.md#project-style)) |
| `suggestion_cache_backend` | `json` | the only backend |
| `suggestion_cache_path` | `.verlato_suggestion_cache.json` | the suggestion cache |
| `suggestion_cache_ttl_days` | 30 | days before cached suggestions expire |

## targets

Release-note files that `draft apply` can write. Each target has a `path` (relative to the
repository root) and a `format`:

| Format | Output |
|---|---|
| `changelog` | Keep a Changelog-style entries |
| `github-release` | GitHub release notes |

The built-in targets are `changelog` (`CHANGELOG.md`) and `github-release`
(`.github/release.md`). A `targets` object **replaces** that list, so name every target you
use; each needs both `path` and `format`:

```json
{
  "targets": {
    "changelog": {"path": "docs/CHANGELOG.md", "format": "changelog"},
    "github-release": {"path": ".github/release.md", "format": "github-release"},
    "website": {"path": "site/releases.md", "format": "github-release"}
  }
}
```

`verlato draft apply --target` and the MCP `verlato_apply_draft` tool accept `changelog` and
`github-release` (use `--file` / `file` for any other path); the editor service's
`draft.apply` accepts any configured target name.
