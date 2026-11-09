# Moving Verlato from Python to Rust

Verlato is moving to Rust. The native `verlato` binary (crate `crates/verlato`) now owns the
release-note pipeline and links the IntentumDiff engine in-process, and every model provider
is native: scans and drafts call Ollama (the default), LiteLLM/OpenAI-compatible endpoints,
GitHub Models, the external-command plugin, the Codex CLI and Claude Code agents, the mock and
stub providers, and fallback chains of those. The remaining model-backed features (suggestions,
commit intents, the MCP server) still run in the Python implementation, which the binary
delegates to transparently. This document records the assessment, the current state, and the
remaining phases.

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
| Model providers: stub, mock, Ollama (incl. streaming), LiteLLM, GitHub Models, plugin, Codex/Claude CLI, fallback chains; request audits, privacy records, OTEL-style model logs | `providers/*`, `core/model_privacy.py` | ~2.4k | **Ported** (Ollama tool calling stays in Python with the suggestion strategies) |
| Classifier model assists (summary rewrite, diff-fallback classification, change-summary payload) | parts of `core/classifier.py`, `core/change_summary.py` | ~0.6k | **Ported** |
| Suggestions: prompt preview, single-strategy `--preview`/`--preflight`, JSON cache, scoring, traces | `cli/main.py` (parts), `state/suggestion_cache.py` (JSON), `core/change_summary.py` | ~2.5k | **Ported** |
| Suggestions: batched strategies (paired, full-diff-json), tool retrieval, non-JSON (plugin-registered) cache backends, adaptive recommendations, applying a suggestion (`draft suggest` without `--preview`) | `cli/main.py` (parts), `core/suggestion_tools.py` | ~2k | Delegated |
| Commit intent inference and Conventional Commit drafting | `core/commit_intent.py` | ~1.1k | Delegated |
| MCP server (FastMCP) | `mcp/server.py` | ~0.7k | Delegated |
| GitHub API PR adapter | `github/adapter.py` | ~0.1k | Delegated |

The CLI/service JSON contract is the migration seam: the VS Code extension only talks to
`verlato <command>` (JSON on stdout, progress on stderr) and to `verlato service` (JSON lines),
so the Rust binary replaces the Python one command by command without extension changes.

## What runs where today

The native binary handles these command paths itself:

```
scan | generate | reconcile          (with any model provider; see below)
draft json | preview | apply | review (including --model-provider)
draft prompt
draft suggest --preview | --preflight (single strategy, JSON cache; see below)
render
state show | mark-published | clear-working
feedback record
doctor engine
service                              (health, info, draft.* incl. model providers, feedback.record, state.show, state.clear_working)
```

Everything else is delegated to the Python implementation with the original arguments:
the rest of `draft suggest` (see below), `commit conventional`, `doctor ollama|model`, `serve` (MCP),
`model-logs`, `feedback export|recommend`, `state clear-suggestion-cache`, and the
`suggestion.*` / `commit.*` service methods (proxied to a lazily started Python service).

`scan`/`generate`/`reconcile` (and drafts or reviews with `--model-provider`) ask the provider
to enrich summaries and, on the diff-fallback path, classifications. This runs natively for every
provider, with the same prompts, evidence payloads, request audits and hashes, `[verlato:model]`
trace lines and `VERLATO_MODEL_LOG_PATH` records as Python. The parity suite checks it with a
recording plugin, fake Ollama (plain and streaming), OpenAI-compatible and GitHub Models servers,
fake Codex/Claude executables, and the deterministic mock provider (which reproduces Python's
Mersenne Twister). A native command is still delegated for commit intent inference and the
`github` PR adapter.

`draft suggest --preview`/`--preflight` runs natively when the effective strategy is `single`:
`--strategy single`, or `auto`/`paired`/`full-diff-json`/`tool-retrieval` whenever the privacy
mode doesn't let the provider see source content (the default `metadata-only`), because Python
downgrades those to `single`. It is delegated for a strategy that stays batched or uses tool
retrieval, a non-JSON cache backend, adaptive recommendations, and without `--preview`/`--preflight`
(which applies the suggestion to review state). The parity suite compares outputs, cache files,
recorded model requests and trace lines against Python for both engines, a recording plugin and a
fake Ollama server.

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

## Upstream IntentumDiff issues

Engine gaps found while building on IntentumDiff, with the workaround Verlato carries until each
is fixed (both runtimes implement the same workaround):

| Issue | Gap | Verlato workaround |
|---|---|---|
| [core#297](https://github.com/buchochelliq-labs/intentumdiff-core/issues/297) | No definition-level signature change; a new required parameter is only an `identifier` addition | Infers "extended" (compatible) signatures from `default_parameter`/`optional_parameter` additions; required-parameter additions go undetected |
| [core#298](https://github.com/buchochelliq-labs/intentumdiff-core/issues/298) | One file over the fuel budget fails cross-file analysis for the whole commit | Parses the failing path from the error, excludes it and retries (`_attach_cross_file`); cross-file stays opt-in |
| [core#299](https://github.com/buchochelliq-labs/intentumdiff-core/issues/299) | Spurious `MOVED_CODE` for the surviving sub-expression of a modified statement | Folds moves inside meaningful-edit spans (`_is_moved_within_meaningful`) |
| [core#300](https://github.com/buchochelliq-labs/intentumdiff-core/issues/300) | Change descriptions don't say what changed (`text_diff` empty) | Slices bounded before/after snippets from the file contents (`_node_source`) |
| [core#301](https://github.com/buchochelliq-labs/intentumdiff-core/issues/301) | Files over ~10k lines fail the fixed 16 MiB output/tree limits | Recorded as unreviewed with the engine's reason |
| [core#250](https://github.com/buchochelliq-labs/intentumdiff-core/issues/250) (comment) | Spurious `module` pseudo-moves and a `module.` prefix on real moves | Cross-file stays opt-in |
| [python#332](https://github.com/buchochelliq-labs/intentumdiff-python/issues/332) | `finalize_commit_diff` crashes on `FileDiffError` results | Only diffed results are passed to cross-file analysis |

## LLM evidence

Model-backed features still run in Python, but they already consume the IntentumDiff semantic
diff instead of raw patches (see "What the LLM sees" in the README). The semantic diff lives in
the per-commit digest (`FileSemantics.changes`, digest format `v2`), which the Rust engine
produces in the identical format, so the providers ported in the next phase receive the same
evidence without depending on patch text.

## Remaining phases

1. **Providers** (done): the `Provider` trait, request/prompt/evidence hashes (byte-identical
   canonical JSON), privacy records, every provider, fallback chains, model telemetry and the
   classifier's model assists.
2. **Suggestions** (in progress): done: `draft prompt`, and `draft suggest --preview`/`--preflight`
   for the single strategy (target selection, fan-out guard, prompt budgets, the JSON cache with
   the same file format and lock, candidate parsing and filters, parallel workers, and the
   `[verlato:suggestion]`/`[verlato:model]`/progress traces). Next: the paired and full-diff-json
   batch strategies, tool retrieval (Ollama tool calling and `core/suggestion_tools.py`), the
   registered non-JSON cache backends, adaptive recommendations and applying a suggestion to
   review state.
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
