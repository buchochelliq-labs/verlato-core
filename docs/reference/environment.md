# Environment variables

Environment variables override [`verlato.json`](configuration.md); command-line options
override both. The VS Code extension sets many of these from its settings (shown in the last
column).

## Files and evidence

| Variable | Meaning | VS Code setting |
|---|---|---|
| `VERLATO_CONFIG_PATH` | config file to use instead of `./verlato.json` | |
| `VERLATO_STATE_PATH` | state file (default `.verlato_state.json`) | |
| `VERLATO_DIFF_ENGINE` | `intentumdiff` or `git` | `verlato.diffEngine` |
| `VERLATO_INTENTUMDIFF_WASM_DIR` (or `INTENTUMDIFF_WASM_DIR`) | the IntentumDiff parser bundle directory | |
| `VERLATO_SEMANTIC_CACHE_PATH` | semantic review cache (default `.verlato_semantic_cache.json`) | |
| `VERLATO_SEMANTIC_PARALLELISM` | threads for uncached commit reviews (default up to 4, at most 16) | `verlato.semanticParallelism` |
| `VERLATO_PR_ADAPTER` | `standard-git` (default), `github` or `none` ([PR mapping](../guide/concepts.md#pull-request-mapping)) | |
| `VERLATO_GITHUB_REPO` | `owner/repo` for the `github` PR adapter | |
| `GITHUB_TOKEN` or `GH_TOKEN` | token for the `github` PR adapter and the GitHub Models provider | |
| `VERLATO_GITHUB_API_URL` | GitHub REST API base URL (default `https://api.github.com`) | |
| `VERLATO_COMMIT_INTENT_INFERENCE` | `off` or `low-signal` | `verlato.commitIntentInference` |
| `VERLATO_FEEDBACK_INCLUDE_TEXT` | `1` keeps summary text in feedback events | `verlato.feedbackIncludeText` |

## Models

| Variable | Meaning | VS Code setting |
|---|---|---|
| `VERLATO_MODEL_PROVIDER` | provider name | `verlato.modelProvider` |
| `VERLATO_MODEL_PRIVACY` | privacy mode | `verlato.modelPrivacy` |
| `VERLATO_ALLOW_REMOTE_PATCHES` | `1` selects `trusted-provider-patch` for scans and drafts | `verlato.allowRemotePatches` |
| `VERLATO_MODEL_MAX_ITEMS` | fanout guard: skip model calls above this many changesets (default 25, `-1` off) | `verlato.maxModelItems` |
| `VERLATO_OLLAMA_MODEL`, `VERLATO_OLLAMA_SUGGEST_MODEL` | Ollama model, and a suggestion-only model | `verlato.ollamaModel`, `verlato.ollamaSuggestionModel` |
| `VERLATO_OLLAMA_ENDPOINT` | Ollama chat endpoint | `verlato.ollamaEndpoint` |
| `VERLATO_OLLAMA_TIMEOUT_SECONDS` | per-request timeout (default 15) | `verlato.ollamaTimeoutSeconds` |
| `VERLATO_OLLAMA_STREAM` | `1` streams responses into telemetry | `verlato.ollamaStreamResponses` |
| `VERLATO_OLLAMA_KEEP_ALIVE` | keep the model loaded (default `10m`; empty for Ollama's default) | |
| `VERLATO_OLLAMA_NUM_CTX` | context window | |
| `VERLATO_OLLAMA_NUM_PREDICT` | global output cap | |
| `VERLATO_OLLAMA_SUMMARY_NUM_PREDICT`, `VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT`, `VERLATO_OLLAMA_SUGGEST_NUM_PREDICT` | per-task output caps (180, 24, adaptive) | |
| `VERLATO_OLLAMA_SUGGEST_THINK` | `1` allows thinking on suggestion requests | |
| `VERLATO_LITELLM_ENDPOINT`, `VERLATO_LITELLM_MODEL`, `VERLATO_LITELLM_SUGGEST_MODEL` | LiteLLM / OpenAI-compatible endpoint and models | |
| `VERLATO_LITELLM_API_KEY`, `VERLATO_LITELLM_API_KEY_ENV` | the API key, or the name of the variable holding it | |
| `VERLATO_LITELLM_TIMEOUT_SECONDS`, `VERLATO_LITELLM_MAX_TOKENS` | timeout (default 30) and `max_tokens` (default 1024, `0` none) | |
| `VERLATO_PLUGIN_COMMAND`, `VERLATO_PLUGIN_MODEL`, `VERLATO_PLUGIN_SUGGEST_MODEL` | plugin command (shell-style) and models | |
| `VERLATO_PLUGIN_ENV`, `VERLATO_PLUGIN_TIMEOUT_SECONDS` | comma-separated variables passed to the plugin; timeout (default 30) | |
| `VERLATO_GITHUB_MODEL`, `VERLATO_GITHUB_MODELS_ENDPOINT` | GitHub Models model (default `openai/gpt-4o-mini`) and endpoint | |
| `VERLATO_CODEX_COMMAND`, `VERLATO_CODEX_MODEL`, `VERLATO_CODEX_TIMEOUT_SECONDS` | the Codex CLI (default timeout 120) | |
| `VERLATO_CLAUDE_COMMAND`, `VERLATO_CLAUDE_MODEL`, `VERLATO_CLAUDE_TIMEOUT_SECONDS` | the Claude Code CLI (default timeout 120) | |

## Suggestions and style

| Variable | Meaning | VS Code setting |
|---|---|---|
| `VERLATO_SUGGESTION_STRATEGY` | `auto`, `single`, `paired`, `full-diff-json`, `tool-retrieval` | `verlato.suggestionStrategy` |
| `VERLATO_SUGGESTION_COUNT` | suggestions per item (1-5) | `verlato.suggestionCount` |
| `VERLATO_SUGGESTION_PARALLELISM` | items at once (1-8) | `verlato.suggestionParallelism` |
| `VERLATO_SUGGESTION_BATCH_SIZE` | items per structured request (1-5) | `verlato.suggestionBatchSize` |
| `VERLATO_SUGGESTION_PROMPT_PROFILE` | `conservative`, `lean`, `minimal` | `verlato.suggestionPromptProfile` |
| `VERLATO_SUGGESTION_ADAPTIVE_RECOMMENDATIONS` | `1` adds tuning advice | `verlato.suggestionAdaptiveRecommendations` |
| `VERLATO_SUGGESTION_TONE` | `release-note`, `concise`, `technical`, `customer-facing` | `verlato.suggestionTone` |
| `VERLATO_SUGGESTION_CACHE_BACKEND` | `json` (the only backend) | |
| `VERLATO_SUGGESTION_CACHE_PATH` | suggestion cache file | |
| `VERLATO_SUGGESTION_CACHE_TTL_DAYS` | cache expiry in days (default 30) | |
| `VERLATO_STYLE_GUIDANCE` | project writing guidance | `verlato.styleGuidance` |
| `VERLATO_PROJECT_VOCABULARY`, `VERLATO_BANNED_PHRASES` | comma-separated lists | `verlato.projectVocabulary`, `verlato.bannedPhrases` |
| `VERLATO_SPECIFICITY_FLOOR` | 0-1; reject vaguer suggestions | `verlato.specificityFloor` |

## Diagnostics

| Variable | Meaning | VS Code setting |
|---|---|---|
| `VERLATO_TRACE_PROGRESS` | `1` prints `[verlato:progress]` events on stderr | (always on) |
| `VERLATO_TRACE_MODEL_CALLS` | `1` prints `[verlato:model]` model lifecycle events | (always on) |
| `VERLATO_TRACE_MODEL_PAYLOADS` | `1` includes full prompts and responses | `verlato.telemetry.includePromptPayloads` |
| `VERLATO_TRACE_MODEL_REASONING` | `1` includes provider-exposed reasoning text | `verlato.telemetry.includeReasoningPayloads` |
| `VERLATO_REDACT_MODEL_PAYLOADS` | `0` turns payload redaction off (on by default) | `verlato.telemetry.redactPayloads` |
| `VERLATO_MODEL_LOG_PATH` | append JSONL model telemetry to this file | `verlato.telemetry.debugModelLogPath` |

The extension also sets `VERLATO_SEMANTIC_CROSS_FILE` from `verlato.semanticCrossFile`;
the native engine does not support cross-file move detection yet and ignores it.
