# Models and providers

Verlato works without any model: it classifies and summarises from commit messages, labels and
the semantic review. A model makes three things better:

- **summaries** of changesets during scans and drafts (low-confidence classifications get a
  second opinion too);
- **suggestions** (`draft suggest` and the extension's Intent inference panel): alternative
  wordings you can apply;
- **commit intent**: Conventional Commit type and scope for vague commits, and full
  Conventional Commit messages (`commit conventional`).

The model is always an assistant: its answers are checked, and empty answers, error markers
or answers that only repeat the title fall back to the deterministic summary.

## Choosing a provider

The provider for a command comes from, in order: `--model-provider`, `VERLATO_MODEL_PROVIDER`
(the VS Code `verlato.modelProvider` setting sets it), then `provider.name` in
[`verlato.json`](../reference/configuration.md#provider). The default is `ollama`. If no
Ollama server is running, scans and drafts simply keep their deterministic summaries.

| Provider | Where the model runs | Notes |
|---|---|---|
| `stub` | nowhere | keeps the built-in summaries; for tests and fully offline use |
| `mock` | in Verlato | deterministic summaries and suggestions; for demos and tests |
| `ollama` (default) | your machine | a local [Ollama](https://ollama.com) server |
| `litellm` | wherever your LiteLLM proxy routes | the recommended way to bring your own model: any OpenAI-compatible endpoint |
| `plugin` | an external command | an audited command that speaks JSON, for runtimes LiteLLM cannot reach |
| `github-copilot` | GitHub Models | uses a GitHub token |
| `codex-cli` | OpenAI, via the Codex CLI | uses your Codex / ChatGPT login |
| `claude-code` | Anthropic, via Claude Code | uses your Claude Code login |

Remote providers never see your source code by default; see [Privacy modes](privacy.md).
When a scan has more changesets than `provider.max_items_per_scan` (25 by default,
`VERLATO_MODEL_MAX_ITEMS`), Verlato skips model calls for that scan rather than flood the
model; `-1` turns the guard off.

### Ollama

```sh
ollama pull gemma4:latest
verlato draft suggest --from-latest-tag --all-visible --preview
```

| Setting | `verlato.json` (`provider.*`) | Environment | Default |
|---|---|---|---|
| model | `model` | `VERLATO_OLLAMA_MODEL` | `gemma4:latest` |
| suggestion-only model | `suggest_model` | `VERLATO_OLLAMA_SUGGEST_MODEL` | the main model |
| endpoint | `endpoint` | `VERLATO_OLLAMA_ENDPOINT` | `http://127.0.0.1:11434/api/chat` |
| timeout per request | `timeout_seconds` | `VERLATO_OLLAMA_TIMEOUT_SECONDS` | 15 s |
| keep the model loaded | `keep_alive` | `VERLATO_OLLAMA_KEEP_ALIVE` | `10m` |
| context window | `num_ctx` | `VERLATO_OLLAMA_NUM_CTX` | Ollama's |
| output caps | `num_predict`, `summary_num_predict`, `classify_num_predict`, `suggest_num_predict` | `VERLATO_OLLAMA_*_NUM_PREDICT` | 180 for summaries, 24 for classification, adaptive for suggestions |
| thinking for suggestions | `suggest_think` | `VERLATO_OLLAMA_SUGGEST_THINK` | off |
| stream output | | `VERLATO_OLLAMA_STREAM=1` | off (on in VS Code) |

Ollama decides its own concurrency. If suggestion runs show queueing while
`suggestionParallelism` is above 1, start Ollama with `OLLAMA_NUM_PARALLEL` (and enough
memory). [`verlato doctor ollama`](troubleshooting.md#doctor-ollama) measures all of this.

### LiteLLM and other OpenAI-compatible endpoints

```json
{
  "provider": {
    "name": "litellm",
    "endpoint": "http://127.0.0.1:4000/v1/chat/completions",
    "model": "ollama/gemma4:latest",
    "api_key_env": "LITELLM_API_KEY"
  }
}
```

`api_key_env` names the environment variable that holds the key (or set
`VERLATO_LITELLM_API_KEY` directly). The output cap is sent as `max_tokens`: 1024 by default,
scaled with the number of suggestions up to 4096, `0` to send none
(`VERLATO_LITELLM_MAX_TOKENS` or `num_predict`). Any server with an OpenAI-style
`/v1/chat/completions` endpoint works, including local ones such as llama.cpp or vLLM.

### GitHub Models

`--model-provider github-copilot` calls GitHub Models with `GITHUB_TOKEN` or `GH_TOKEN`, or
the token of the logged-in GitHub CLI (`gh auth token`). Model: `VERLATO_GITHUB_MODEL`
(default `openai/gpt-4o-mini`); endpoint: `VERLATO_GITHUB_MODELS_ENDPOINT`.

### The Codex and Claude Code CLIs

`codex-cli` and `claude-code` run the locally installed, logged-in `codex` or `claude`
command, so you can use a ChatGPT or Claude plan instead of an API key. Override the command,
model and timeout (default 120 s) with `VERLATO_CODEX_COMMAND` / `VERLATO_CODEX_MODEL` /
`VERLATO_CODEX_TIMEOUT_SECONDS` and the `VERLATO_CLAUDE_*` equivalents.

### Plugins

A plugin is any command that reads one JSON request on stdin and writes one JSON response on
stdout:

```json
{
  "provider": {
    "name": "plugin",
    "plugin_manifest": {
      "id": "my-runtime",
      "display_name": "My runtime",
      "command": ["my-runtime-adapter", "--json"],
      "config_schema": {"type": "object"},
      "capabilities": {"tasks": ["suggest", "summarize", "classify"], "location": "local"}
    },
    "plugin_config": {},
    "plugin_env": ["MY_RUNTIME_TOKEN"],
    "timeout_seconds": 30
  }
}
```

The request (`schema: "verlato.model_plugin.request.v1"`) carries the task, model, chat
messages, prompt, evidence, response format, your `plugin_config`, and four hashes:
`request_hash`, `prompt_hash`, `evidence_hash` and `schema_hash`. The response must be a JSON
object with `content` (the model's answer) and **must echo `request_hash`**, or Verlato
rejects it. It may add `metadata`. The plugin runs with a minimal environment (only the
variables listed in `plugin_env`), outside the repository, and with a timeout (30 s by
default). The hashes catch drift and simple tampering, but a plugin that receives the prompt
can still lie about it: use a built-in provider or LiteLLM when Verlato must control the
whole request path. `VERLATO_PLUGIN_COMMAND`, `VERLATO_PLUGIN_MODEL`, `VERLATO_PLUGIN_ENV` and
`VERLATO_PLUGIN_TIMEOUT_SECONDS` override the configuration.

### Fallback chains

List fallbacks to try, in order, when a provider fails or is unreachable:

```json
{
  "provider": {
    "name": "ollama",
    "model": "gemma4:latest",
    "fallbacks": [
      {"name": "litellm", "model": "openai/gpt-4o-mini", "api_key_env": "OPENAI_API_KEY"}
    ]
  }
}
```

Each fallback inherits the primary's settings and overrides what it names. Verlato never falls
back to a remote provider you did not list, and under `local-summary-then-ai` source content
is only sent if every provider in the chain is local.

## Suggestion strategies

`draft suggest` (and the extension's suggestions) can send items to the model in different
ways. Choose with `--strategy`, `suggestion_strategy`, `VERLATO_SUGGESTION_STRATEGY` or
`verlato.suggestionStrategy`:

| Strategy | How items are sent |
|---|---|
| `single` | one bounded prompt per item |
| `paired` | two items per structured JSON request |
| `full-diff-json` | up to `--batch-size` (default 5) items per structured JSON request, with each file's semantic diff |
| `tool-retrieval` | the model is given tools to fetch each file's semantic diff itself (Ollama only) |
| `auto` (default) | `tool-retrieval` for Ollama, `full-diff-json` for LiteLLM and plugins, `single` otherwise |

The batched and tool strategies need source content, so they only run when the
[privacy mode](privacy.md) lets the provider see it; otherwise Verlato uses `single`. Under
the default `metadata-only` mode every strategy is `single`.

Suggestions are cached in `.verlato_suggestion_cache.json` (30 days by default) and reused
while the item, prompt, provider, model, tone, style, count, profile and strategy are
unchanged. `--refresh-cache` overwrites, `--no-cache` skips the cache, and
`verlato state clear-suggestion-cache` empties it.

Tuning: `--count` (1-5 alternatives), `--parallelism` (1-8 items at once),
`--batch-size` (1-5), `--prompt-profile conservative|lean|minimal` (prompt budget),
`--suggest-model` (route suggestions to a smaller model), and `--adaptive-recommendations`
(advice on these settings from the run's timings, never applied automatically).

## Commit intent inference

Vague commits group and classify badly. With commit intent inference on, Verlato asks the
model for a Conventional Commit type and scope for every commit that has no Conventional
Commit subject, or has one without a scope, before grouping. Authored scoped Conventional
Commits always win. Turn it on with `commit_intent_inference: "low-signal"`,
`VERLATO_COMMIT_INTENT_INFERENCE=low-signal`, `--infer-commit-intents`, or
`verlato.commitIntentInference`. It is skipped when the commits to infer exceed the fanout
guard.
