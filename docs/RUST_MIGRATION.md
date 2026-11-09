# Moving Verlato from Python to Rust

Verlato is moving to Rust. The native `verlato` binary (crate `crates/verlato`) now owns the
release-note pipeline and links the IntentumDiff engine in-process. Model-backed features still
run in the Python implementation, which the binary delegates to transparently. This document
records the assessment, the current state, and the remaining phases.

## Why Rust

- **One engine, no Python at runtime for the core path.** IntentumDiff's engine is Rust
  (`intentumdiff-core`); the Python package is a thin ctypes binding over it. Linking
  `intentumdiff_rust_core::api::review_sources` directly removes the Python interpreter,
  pydantic model construction and the per-call FFI/JSON round trip from every file review.
- **Speed.** Measured on this repository (`draft json` over three commits, 80 changed files,
  no cache): **8.5 s native vs 29 s Python** (Python already using four worker processes), with
  identical verdicts, group counts and symbol findings. The native pipeline reviews the files of
  each commit on parallel threads instead of spawning interpreter processes.
- **Distribution.** The VS Code extension can ship a single executable plus the parser bundle
  (`bin/verlato` + `wasm/`) instead of requiring a Python 3.12 virtual environment.
- **One language with the engine.** Engine API changes surface as compile errors instead of
  runtime attribute errors.

## Assessment of the Python codebase

About 16k lines of Python (plus ~7k lines of tests):

| Area | Python modules | Lines | Status |
|---|---|---:|---|
| Pipeline core: git collection, grouping, conventional commits, areas, checksums, evidence, deterministic classification, reconciliation | `core/{collector,adapters,grouper,conventional,areas,checksum,diff_evidence,classifier (deterministic path),reconciler}` | ~1.9k | **Ported** |
| IntentumDiff semantic evidence (digest, verdicts, summary, cache) | `core/semantic.py` | ~1k | **Ported** (engine linked natively) |
| Drafts, rendering, changelog apply, state store, review/feedback/state commands | `core/{drafts,generator,changelog}`, `state/store.py`, parts of `cli/main.py` | ~1.5k | **Ported** |
| Editor stdio service (deterministic methods) | `service_stdio.py`, parts of `mcp/service.py` | ~0.5k | **Ported**; other methods proxied |
| Model providers (Ollama, LiteLLM, GitHub Models, plugin, Codex/Claude CLI, fallback, mock, telemetry) | `providers/*` | ~2.2k | Delegated |
| Suggestions (prompts, batching, cache, retrieval tools, adaptive recommendations) | `cli/main.py` (most of it), `core/suggestion_tools.py`, `state/suggestion_cache.py`, `core/change_summary.py` | ~4.5k | Delegated |
| Commit intent inference and Conventional Commit drafting | `core/commit_intent.py` | ~1.1k | Delegated |
| MCP server (FastMCP) | `mcp/server.py` | ~0.7k | Delegated |
| GitHub API PR adapter | `github/adapter.py` | ~0.1k | Delegated |

The CLI/service JSON contract is the migration seam: the VS Code extension only talks to
`verlato <command>` (JSON on stdout, progress on stderr) and to `verlato service` (JSON lines),
so the Rust binary replaces the Python one command by command without extension changes.

## What runs where today

The native binary handles these command paths itself:

```
scan | generate | reconcile          (when no real model provider is configured; see below)
draft json | preview | apply | review
render
state show | mark-published | clear-working
feedback record
doctor engine
service                              (health, info, draft.*, feedback.record, state.show, state.clear_working)
```

Everything else is delegated to the Python implementation with the original arguments:
`draft suggest`, `draft prompt`, `commit conventional`, `doctor ollama|model`, `serve` (MCP),
`model-logs`, `feedback export|recommend`, `state clear-suggestion-cache`, and the
`suggestion.*` / `commit.*` service methods (proxied to a lazily started Python service).

A native command is also delegated when it would call a model: a non-`stub` provider for
`scan`/`generate`/`reconcile` (Python asks the configured provider, Ollama by default, to enrich
summaries), an explicit `--model-provider` on draft commands, commit intent inference, or the
`github` PR adapter. The extension already runs non-model commands with the `stub` provider, so
its draft/review flows are fully native.

The Python CLI is found through `VERLATO_PYTHON_CLI` (a path to `verlato-py` or a command line
such as `python3 -m verlato.cli`), then `verlato-py` on `PATH`. The Python package installs
`verlato-py` for this purpose. Set `VERLATO_TRACE_DELEGATION=1` to log delegations. If no Python
implementation is available, delegated commands fail with an explanation.

## Engine and parser bundle

`crates/verlato` depends on `intentumdiff-core` pinned to a commit (see `Cargo.toml`) and calls
the supported `api::review_sources` per changed file. Each commit is reviewed as
`parent..sha`: changed paths from `git diff --raw`, both blob versions from one
`git cat-file --batch`, files reviewed in parallel. Files the engine cannot review are recorded
as unreviewed with the engine's reason; a failing file never fails the commit. Digests use the
same JSON format as the Python implementation and are cached in `.verlato_semantic_cache.json`
(cache keys include the engine build, so the two runtimes do not share entries).

The engine loads its language parsers (WebAssembly components) from a parser bundle:
`parser_manifest.json` plus `*.wasm`. The binary looks in `VERLATO_INTENTUMDIFF_WASM_DIR`,
`INTENTUMDIFF_WASM_DIR`, then `wasm/` next to the executable. Provision it with:

```bash
scripts/fetch_intentumdiff_parsers.sh            # -> target/release/wasm
```

The script extracts the components from the checksum-pinned `intentumdiff-python` 0.0.2b2
wheel on PyPI (the components are platform independent). When IntentumDiff publishes native
releases with a standalone parser bundle, the script should switch to that artifact.

## Parity

`tests/test_rust_parity.py` builds repositories with conventional commits, PR merges, area
splits, docs/test-only changes, formatting-only edits, added/removed functions and breaking
markers, runs every native command through both implementations, and requires identical JSON,
Markdown, changelog files and state files (with `--diff-engine git` exactly; with IntentumDiff,
ignoring only the engine version strings). It also checks the stdio service protocol and that
delegated commands produce the Python output.

Known, intentional differences:

- JSON output escapes: Python writes `\uXXXX` for non-ASCII characters, Rust writes UTF-8.
  Both parse to the same values.
- `health`/`info` service responses add `"runtime": "rust"` (and `info` reports the Python
  delegate and parser bundle).
- Cross-file move detection (`semantic_cross_file`) is not available natively yet; it is
  opt-in and off by default in Python as well.

A changelog re-apply bug found while porting (re-applying a version left that version's old
subsections behind) was fixed in both implementations.

## LLM evidence

Model-backed features still run in Python, but they already consume the IntentumDiff semantic
diff instead of raw patches (see "What the LLM sees" in the README). The semantic diff lives in
the per-commit digest (`FileSemantics.changes`, digest format `v2`), which the Rust engine
produces in the identical format, so the providers ported in the next phase receive the same
evidence without depending on patch text.

## Remaining phases

1. **Providers** (next): a `Provider` trait with Ollama, OpenAI-compatible/LiteLLM, GitHub
   Models and the external-command plugin protocol (request/prompt/evidence hashes), plus the
   model privacy rules from `core/model_privacy.py`. Then `scan`/`generate` stop delegating.
2. **Suggestions**: prompt construction and budgets, batching strategies, the suggestion cache,
   retrieval tools, telemetry events (`[verlato:suggestion]`), `draft suggest|prompt`.
3. **Commit intents**: inference and Conventional Commit drafting (`commit conventional`).
4. **MCP server** with the same tools/resources/prompts (e.g. `rmcp`), and the GitHub adapter.
5. **Retire Python**: remove delegation, ship the extension with the native binary and parser
   bundle per platform, and drop the Python package (or keep it only as a library binding).

Each phase moves a command from "delegated" to "native" behind the same CLI contract and
extends the parity suite before the Python code for it is deleted.

## Building

```bash
cargo build --release -p verlato                 # links the IntentumDiff engine (first build ~6 min)
scripts/fetch_intentumdiff_parsers.sh            # parser bundle next to the binary
cargo test -p verlato                            # unit tests
pytest tests/test_rust_parity.py                 # parity against the Python implementation
```

`cargo build -p verlato --no-default-features` builds without the engine (only
`--diff-engine git` works) for fast iteration on the pipeline.
