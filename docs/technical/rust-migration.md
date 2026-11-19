# Moving Verlato from Python to Rust

Verlato has moved to Rust, and the Python implementation is retired. The `verlato` crate
(`crates/verlato`) owns everything: the release-note pipeline with the IntentumDiff engine linked
in-process, every model provider (Ollama, LiteLLM/OpenAI-compatible endpoints, GitHub Models, the
external-command plugin, the Codex CLI and Claude Code agents, mock, stub and fallback chains),
commit intent inference and Conventional Commit drafting, every suggestion strategy (single,
paired, full-diff-json and tool-retrieval with Ollama tool calling) with adaptive
recommendations, the maintenance commands, the editor service and the MCP server
(`verlato serve`). Nothing is delegated to Python any more.

The `verlato` Python package is now a thin wrapper over that runtime: PyO3 bindings
(`crates/verlato-python`, the `verlato._native` extension module) plus a few lines of Python
(`python/verlato`). See [The Python package](#the-python-package). This document records the
assessment, how the port was checked, and the result.

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
| Editor stdio service (deterministic methods) | `service_stdio.py`, parts of `mcp/service.py` | ~0.5k | **Ported** |
| Model providers: stub, mock, Ollama (incl. streaming), LiteLLM, GitHub Models, plugin, Codex/Claude CLI, fallback chains; request audits, privacy records, OTEL-style model logs | `providers/*`, `core/model_privacy.py` | ~2.4k | **Ported** (including Ollama tool calling) |
| Classifier model assists (summary rewrite, diff-fallback classification, change-summary payload) | parts of `core/classifier.py`, `core/change_summary.py` | ~0.6k | **Ported** |
| Suggestions: prompt preview, `--preview`/`--preflight` for every strategy (single, paired, full-diff-json, tool-retrieval), batch fallbacks, applying a suggestion, the suggestion cache, scoring, adaptive recommendations, traces | `cli/main.py` (parts), `state/suggestion_cache.py`, `core/change_summary.py`, `core/suggestion_tools.py` | ~4.5k | **Ported** |
| Commit intent inference and Conventional Commit drafting | `core/commit_intent.py` | ~1.1k | **Ported** |
| Maintenance: `doctor ollama`, `doctor model`, `model-logs export`, `feedback export`, `feedback recommend`, `state clear-suggestion-cache`; the `suggestion.*` and `state.clear_suggestion_cache` service methods | `cli/main.py` (parts), `service_stdio.py`, `mcp/service.py` | ~1k | **Ported** |
| MCP server (`verlato serve`): FastMCP's handshake, tool/resource/prompt catalog, argument validation and result shapes; the MCP evidence tools (`SuggestionEvidenceToolProvider`) | `mcp/server.py`, `mcp/service.py`, `core/suggestion_tools.py` | ~1.7k | **Ported** |
| GitHub API PR adapter | `github/adapter.py` | ~0.1k | **Ported** |

The CLI/service JSON contract is the migration seam: the VS Code extension only talks to
`verlato <command>` (JSON on stdout, progress on stderr) and to `verlato service` (JSON lines),
so the Rust binary replaces the Python one command by command without extension changes.

## What runs where today

The native binary handles these command paths itself:

```
scan | generate | reconcile          (with any model provider; see below)
draft json | preview | apply | review (including --model-provider)
draft prompt
draft suggest --preview | --preflight (every strategy; see below)
draft suggest --id (applies a model summary to review state)
commit conventional
render
state show | mark-published | clear-working | clear-suggestion-cache
feedback record | export | recommend
model-logs export
doctor engine | ollama | model
service                              (every method: health, info, draft.* incl. model providers and intent
                                      inference, commit.conventional[_many], feedback.record, state.*,
                                      suggestion.*)
serve                                (MCP server on stdio; all tools, resources and prompts)
```

Every command and service method runs natively. An unknown command is a usage error (exit code
2, as Python's argparse), an unknown service method answers `invalid_request`, and a PR adapter
that fails to configure is a plain error.

The maintenance commands print Python's exact bytes (including `\uXXXX` escapes and key order).
`doctor ollama` probes the configured Ollama with the same requests (warmup, single and two
concurrent probes), reads `/api/ps` and `ollama ps`, and reports the same bottleneck analysis;
`doctor model` sends the same full-diff-json probe (prompt, evidence and response schema) to any
provider. Suggestion cache backends: Python's `register_suggestion_cache_backend` only works
inside the registering interpreter, so a CLI or service process has just `json`, and both
runtimes report `Unknown suggestion cache backend '<name>'; available: json` for any other name
(`suggestion_cache::open_backend` is the one place to add a native backend; previews and
`clear-suggestion-cache` both use it).

`scan`/`generate`/`reconcile` (and drafts or reviews with `--model-provider`) ask the provider
to enrich summaries and, on the diff-fallback path, classifications. This runs natively for every
provider, with the same prompts, evidence payloads, request audits and hashes, `[verlato:model]`
trace lines and `VERLATO_MODEL_LOG_PATH` records as Python. The parity suite checks it with a
recording plugin, fake Ollama (plain and streaming), OpenAI-compatible and GitHub Models servers,
fake Codex/Claude executables, and the deterministic mock provider (which reproduces Python's
Mersenne Twister). The `github` PR adapter (`VERLATO_PR_ADAPTER=github` with `VERLATO_GITHUB_REPO`
and `GITHUB_TOKEN`/`GH_TOKEN`; `VERLATO_GITHUB_API_URL` for GitHub Enterprise) is native too.

Commit intent inference (`--infer-commit-intents`, `VERLATO_COMMIT_INTENT_INFERENCE`, or
`"commit_intent_inference": "low-signal"` in config) is native for every command that collects
groups and for the `draft.*` service methods: it derives a Conventional Commit from each commit's
diff (task `derive-conventional`, with the same fan-out guard, privacy downgrade, progress events
and request audits), uses the derived type/scope for low-signal commits when grouping and
classifying, and reports `derived_conventional_commits` on items. `commit conventional` and the
`commit.conventional`/`commit.conventional_many` service methods draft messages natively in both
evidence modes (`diff-only`, `subject-hint`).

`draft suggest --preview`/`--preflight` runs natively for every strategy: `single`; `paired` and
`full-diff-json` batches (batch prompts and JSON response schema, per-item and whole-batch
fallback to single calls, the 80k-character full-diff guard, `--batch-size`, parallel chunks and
the batch counters in `performance`); and `tool-retrieval`, where Ollama calls the suggestion
evidence tools (`verlato_get_suggestion_item`, the IntentumDiff semantic-diff tools, or bounded
patches with the `git` engine) for up to four rounds with the same `tool-call` trace lines and
tool metadata. `--adaptive-recommendations` (also `doctor ollama`) is native. `draft suggest --id` without
`--preview` (asks the provider for one summary and records it as an edited, model-generated summary
with provenance and a `suggestion_applied` feedback event) is native too. The parity suite compares outputs, cache files,
recorded model requests and trace lines against Python for both engines, a recording plugin and a
fake Ollama server (including tool calls and tool errors). Under IntentumDiff the structured
strategies embed engine-derived fields (version, sizes) in their prompts, so prompt hashes and
cache keys legitimately differ between the runtimes there; the suite masks only those.

## The Python package

The `verlato` PyPI package (`pyproject.toml`, built with maturin) holds:

- **`verlato._native`** (`crates/verlato-python`): a PyO3 extension module, built for the stable
  ABI (`abi3-py312`: one wheel per platform for CPython 3.12+). It exposes `run(argv)`, which
  runs `verlato::cli::run` in-process without the GIL and returns the exit code;
  `call(method, params_json, repo)`, which answers one service request through
  `verlato::service::call` (the same dispatch as `verlato service`); `version()` and
  `engine_version()`.
- **`python/verlato`**: `verlato.run`, `verlato.call` (parses the response, raising
  `ServiceError` on an error answer), `verlato.engine_version`, `verlato.parser_bundle`,
  `__version__`, the `verlato` and `verlato-mcp` console scripts (`verlato-mcp` is
  `verlato serve`) and `python -m verlato`.
- **`verlato/wasm`**: the IntentumDiff parser bundle, provisioned at build time. Before running
  a command the wrapper sets `VERLATO_INTENTUMDIFF_WASM_DIR` to it unless that or
  `INTENTUMDIFF_WASM_DIR` is already set, so the package has no runtime dependencies.

The command line never exits the process: `verlato::cli::run` prints clap's usage errors and
`--help`/`--version` and returns clap's exit code, prints command errors and returns 1, and
flushes stdout and stderr before returning. The console scripts restore the default Ctrl-C
handler before running, because a command running without the GIL cannot see Python's
`KeyboardInterrupt`.

Versions: `[workspace.package] version` in `Cargo.toml` is the only version. Both crates inherit
it and `pyproject.toml` declares `dynamic = ["version"]`, so maturin writes it into the wheel.

## Engine and parser bundle

`crates/verlato` depends on `intentumdiff-core` pinned to a commit (see `Cargo.toml`) and calls
the supported `api::review_sources` per changed file. Each commit is reviewed as
`parent..sha`: changed paths from `git diff --raw`, both blob versions from one
`git cat-file --batch`, files reviewed in parallel. Files the engine cannot review are recorded
as unreviewed with the engine's reason; a failing file never fails the commit. Digests use the
same JSON format as the Python implementation and are cached in `.verlato_semantic_cache.json`
(cache keys include the engine build, so the two runtimes do not share entries).

The engine loads its language parsers (WebAssembly components) from a parser bundle:
`parser_manifest.json` plus `*.wasm`. The runtime looks in `VERLATO_INTENTUMDIFF_WASM_DIR`,
`INTENTUMDIFF_WASM_DIR`, then `wasm/` next to the executable. The Python package and the release
bundles ship it (`verlato/wasm` in the wheel, `wasm/` beside the executable in a bundle).
Provision it with:

```bash
scripts/fetch_intentumdiff_parsers.sh                       # -> target/release/wasm
scripts/fetch_intentumdiff_parsers.sh python/verlato/wasm   # for the Python package
```

The script is the bundle's single pinned source. It extracts the components from the
checksum-pinned `intentumdiff-python` 0.0.3b1 wheel on PyPI (the components are platform
independent), checks that every component the manifest names is present, and checks each
against a SHA-256 recorded in the manifest when there is one (0.0.3b1's manifest records none).
The wheel is built from `intentumdiff-core` `1c4bdbc5`, the revision `Cargo.toml` pins.
**The pin must move together with the `intentumdiff-core` revision in `Cargo.toml`**: the
parsers are built for that engine. When IntentumDiff publishes native releases with a standalone
parser bundle, the script should switch to that artifact.

## Parity

`tests/test_rust_parity.py` builds repositories with conventional commits, PR merges, area
splits, docs/test-only changes, formatting-only edits, added/removed functions and breaking
markers, and runs every command, the stdio service and the MCP server against them, comparing
with the Python implementation: identical JSON, Markdown, changelog and state files, model
requests, traces and telemetry (with `--diff-engine git` exactly; with IntentumDiff, ignoring
only engine build identity).

The Python implementation is retired, so its side of every comparison is replayed from
recordings in `tests/golden/parity`, made from the last commit that had it (see
`tests/golden/README.md`): each Python invocation's exit code, stdout, stderr, the files it
changed, the requests fake servers received from it and its MCP traffic. The native side runs
live, through the installed Python package (`python -m verlato`) or the executable in
`VERLATO_RUST_BIN`. A replay fails if a test asks anything that was not recorded.

Known, intentional differences:

- JSON output escapes: Python writes `\uXXXX` for non-ASCII characters, Rust writes UTF-8.
  Both parse to the same values.
- `health`/`info` service responses add `"runtime": "rust"` (and `info` reports the parser
  bundle).
- Cross-file move detection (`semantic_cross_file`) runs natively through the engine's commit
  index (since IntentumDiff 0.0.3); it is opt-in and off by default, as in Python, and the
  parity scenarios leave it off.

A changelog re-apply bug found while porting (re-applying a version left that version's old
subsections behind) was fixed in both implementations.

## Upstream IntentumDiff issues

Engine gaps found while building on IntentumDiff. IntentumDiff 0.0.3 (core `1c4bdbc5`) fixed all
of them, and Verlato dropped the workarounds it carried. Both runtimes were changed the same
way before the goldens were re-recorded.

| Issue | Gap | Status in Verlato |
|---|---|---|
| [core#297](https://github.com/buchochelliq-labs/intentumdiff-core/issues/297) | No definition-level signature change; a new required parameter was only an `identifier` addition | **Removed.** Signatures come from the engine's `REFACTORING`/`CHANGE_SIGNATURE` change. Its definition facts (`param_count`, `default_count`) decide whether the required-parameter count grew. A change is `extended` (compatible) only when the old parameter list is an exact prefix of the new one and every new parameter can be omitted, a check made on the definition's source text. Signature changes are behavioural, not refactorings, even though the engine files them under `REFACTORING`. |
| [core#298](https://github.com/buchochelliq-labs/intentumdiff-core/issues/298) | One file over the fuel budget failed cross-file analysis for the whole commit | **Removed.** The engine's commit index skips files it can't index and reports them in `index_errors`; Verlato records them as `excluded from cross-file index: …`. |
| [core#299](https://github.com/buchochelliq-labs/intentumdiff-core/issues/299) | Spurious `MOVED_CODE` for the surviving sub-expression of a modified statement | **Removed** (the move folding). The engine folds the sub-expression into the modification. |
| [core#300](https://github.com/buchochelliq-labs/intentumdiff-core/issues/300) | Change descriptions didn't say what changed (`text_diff` empty) | **Removed for modifications.** A modified statement carries the engine's bounded `old -> new` text instead of Verlato's `before`/`after` snippets. **Kept for added and deleted definitions:** the engine still describes those only by kind and name (`Insert -> function_definition('handle')`), so their bounded source text stays (`after` for an addition, `before` for a deletion). |
| [core#301](https://github.com/buchochelliq-labs/intentumdiff-core/issues/301) | Files over ~10k lines failed fixed size limits | **Fixed upstream as configurable limits.** A file over the limits is recorded as unreviewed with kind `too_large`; `semantic_max_nodes` raises the limits. |
| [core#250](https://github.com/buchochelliq-labs/intentumdiff-core/issues/250) | Spurious `module` pseudo-moves from duplicate names | **Fixed upstream.** The remaining part, the synthetic `module.` prefix on top-level names and root modules taking part in move matching, moved to [core#311](https://github.com/buchochelliq-labs/intentumdiff-core/issues/311) (0.0.4): Verlato strips the prefix and drops `module`-kind entries until then. |
| [python#332](https://github.com/buchochelliq-labs/intentumdiff-python/issues/332) | `finalize_commit_diff` crashed on `FileDiffError` results | **Not applicable.** The native engine calls the core's commit index directly. |

Gaps found while upgrading to 0.0.3, filed for 0.0.4:

| Issue | Gap | Verlato workaround |
|---|---|---|
| [core#330](https://github.com/buchochelliq-labs/intentumdiff-core/issues/330) | Added and deleted definitions are described only by kind and name, with no source text | Keeps its bounded `before`/`after` snippets for added and deleted definitions |
| [core#331](https://github.com/buchochelliq-labs/intentumdiff-core/issues/331) | `api::review_sources` returns size-limit failures as a plain `ReviewError::Engine` | Recognises them by message text to record `too_large` and point at `semantic_max_nodes` |
| [core#332](https://github.com/buchochelliq-labs/intentumdiff-core/issues/332) | `CHANGE_SIGNATURE`'s `fact_delta` reports only `param_count`, and `param_count` counts Python's bare `*` | Reads the parameter list from source to decide whether a change is `extended`. A bare `*` is handled like other `*` entries, so the inflated count cancels out |

IntentumDiff 0.0.3 also reports whether a review explains the whole change (core #252). A file
whose review is `incomplete` is recorded as unreviewed (kind `incomplete`) with the engine's
message, never as a clean review.

## LLM evidence

Model-backed features consume the IntentumDiff semantic diff instead of raw patches (see "What
the LLM sees" in the README). The semantic diff lives in the per-commit digest
(`FileSemantics.changes`, digest format `v2`), which the Rust engine produces in the format the
Python implementation used.

## Remaining phases

1. **Providers** (done): the `Provider` trait, request/prompt/evidence hashes (byte-identical
   canonical JSON), privacy records, every provider, fallback chains, model telemetry and the
   classifier's model assists.
2. **Suggestions** (done): `draft prompt`, `draft suggest --preview`/`--preflight` for every
   strategy (single, paired, full-diff-json, tool-retrieval with Ollama tool calling), the
   suggestion cache, adaptive recommendations, and applying a suggestion to review state
   (`draft suggest --id`).
3. **Commit intents** (done): inference for grouping/classification and Conventional Commit
   drafting (`commit conventional`, `commit.*` service methods).
4. **MCP server** (done): `verlato serve` speaks newline-delimited JSON-RPC on stdio without an
   MCP SDK. The tool/resource/prompt catalog is FastMCP's own output, embedded in
   `crates/verlato/src/mcp_catalog.json` (it was FastMCP's output from the Python server; edit it
   by hand now, and the parity suite fails on drift from the recorded catalog), and the legacy patch tools are
   listed only with the `git` engine. Arguments are validated against the catalog schemas with
   pydantic's lax coercions and error messages. Known differences: the stateless `2026-07-28`
   protocol is not offered (clients negotiate `2025-11-25`), requests are answered one at a time,
   and pydantic messages are reproduced only for the schema shapes the catalog uses. The GitHub
   adapter is done.
5. **Retire Python** (done): delegation is removed, the Python implementation is deleted (its
   answers are kept as golden recordings), and the `verlato` Python package is a thin PyO3
   wrapper that ships the native runtime and parser bundle. Releases (rab, see
   [Releasing](releasing.md)) publish the wheels and sdist to PyPI and per-platform native
   bundles (executable + `wasm/`) to GitHub Releases, which the VS Code extension can use.

Each phase moved commands from "delegated" to "native" behind the same CLI contract and extended
the parity suite before the Python code for them was deleted.

## Building

```bash
cargo build --release -p verlato                 # links the IntentumDiff engine (first build ~6 min)
scripts/fetch_intentumdiff_parsers.sh            # parser bundle next to the binary
cargo test --workspace                           # unit tests
scripts/fetch_intentumdiff_parsers.sh python/verlato/wasm
maturin develop --release                        # the Python package, into the active virtualenv
pytest                                           # package tests and parity (golden replay)
```

`cargo build -p verlato --no-default-features` builds without the engine (only
`--diff-engine git` works) for fast iteration on the pipeline.
