# Troubleshooting

## The doctor commands

### `doctor engine`

```sh
verlato doctor engine
```

Reports whether the IntentumDiff engine is linked and its parser bundle found: `available`,
`version`, `parser_bundle` (where it was found), the configured engine and the semantic cache
path. `error` explains what is missing. The VS Code command **Verlato: Show IntentumDiff
Engine Status** runs it.

### `doctor ollama`

```sh
verlato doctor ollama [--warmup] [--parallel-probe] [--suggest-model <model>] [--adaptive-recommendations]
```

Sends a tiny suggestion-shaped request to the configured Ollama model and reports, as JSON
(`schema: "ollama_doctor.v1"`):

- the effective configuration (endpoint, model, timeout, keep-alive, context and output caps);
- what Ollama has loaded (`/api/ps`, plus `ollama ps` when the `ollama` command is installed),
  including whether the model runs on CPU or GPU;
- for each probe: model load, queue, prompt evaluation and generation time;
- advisory recommendations (client parallelism, keep-alive and warmup, context size,
  processor placement, output caps).

`--warmup` loads the model first so the measurement is warm; `--parallel-probe` sends two
probes at once to see whether Ollama serialises requests (if so, set `OLLAMA_NUM_PARALLEL`
before starting Ollama). It changes no settings. In VS Code, **Model health** and **Warm
model** in the Intent inference panel run it.

### `doctor model`

```sh
verlato doctor model [--model-provider <name>] [--suggest-model <model>] [--timeout-seconds <s>]
```

Sends one tiny, diff-shaped structured request to the suggestion model of any provider and
reports whether it answered with valid structured JSON, how long it took and any warnings
(`schema: "model_doctor.v1"`). Use it to check that a model is capable enough for batched
suggestions before a large run.

## Common problems

### The parser bundle was not found

The IntentumDiff engine needs its parser bundle (`parser_manifest.json` and the `*.wasm`
parsers). Verlato looks, in order, at `VERLATO_INTENTUMDIFF_WASM_DIR`, `INTENTUMDIFF_WASM_DIR`,
then `wasm/` next to the executable, `../share/verlato/wasm` and `../../wasm` relative to it.
The pip package and the release bundles include it, so this usually means a source build:
run `scripts/fetch_intentumdiff_parsers.sh`, or point `VERLATO_INTENTUMDIFF_WASM_DIR` at a
bundle. To work without the engine, use `--diff-engine git` (or `VERLATO_DIFF_ENGINE=git`).

### "Provide --base or one marker mode"

Every range command needs a base: `--base <ref>`, `--from-latest-tag` or
`--from-latest-published`. "No reachable tag found" means the repository has no tag yet: use
`--base` with a commit. "No published release marker found in state" means no release was
recorded with `state mark-published` yet.

### Summaries never come from a model

- Check the provider: `verlato doctor model`, or `verlato doctor ollama` for Ollama.
- More changesets than `max_items_per_scan` (25) turn model calls off for that scan; raise it
  or set `VERLATO_MODEL_MAX_ITEMS=-1`.
- Model answers that are empty, error markers or just the title are discarded in favour of
  the deterministic summary. Run with `VERLATO_TRACE_MODEL_CALLS=1` to see each call on
  stderr.

### Suggestions are slow

Run `verlato doctor ollama --parallel-probe`. Typical fixes: a smaller `--suggest-model`, a
`lean` or `minimal` prompt profile, `OLLAMA_NUM_PARALLEL` on the server, keeping the model
loaded (`VERLATO_OLLAMA_KEEP_ALIVE`), and `--adaptive-recommendations` for advice tailored to
the run. Cached suggestions are reused, so second runs are fast.

### A changeset groups unrelated commits

Commits outside pull requests are grouped when they share a Conventional Commit type and
scope or touch the same files. Use pull requests or Conventional Commits, or turn on
[commit intent inference](models.md#commit-intent-inference) for vague histories. The
item's `grouping_reason` says why commits were grouped.

### The VS Code extension cannot find Verlato

Run **Verlato: Show Service Info**: it shows the executable in use and every place searched.
Set `verlato.cliPath` to the `verlato` executable (or install with pip so it is on `PATH`).

## Diagnostics

| Variable | Shows on stderr |
|---|---|
| `VERLATO_TRACE_PROGRESS=1` | `[verlato:progress] json {...}` progress events |
| `VERLATO_TRACE_MODEL_CALLS=1` | `[verlato:model]` model call lifecycle (also `draft suggest --trace-suggestions`) |
| `VERLATO_TRACE_MODEL_PAYLOADS=1` | full prompts and responses (redacted) |
| `VERLATO_MODEL_LOG_PATH=<file>` | appends JSONL model telemetry to a file |

See [Model telemetry](../technical/model-telemetry.md) for the formats.

## Starting over

```sh
verlato state clear-working           # forget unpublished items and their review decisions
verlato state clear-suggestion-cache  # forget cached suggestions
rm .verlato_semantic_cache.json       # re-review every commit next time
```
