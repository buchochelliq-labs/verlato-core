# Changelog

Notable changes to Verlato, newest first. The repository's own `CHANGELOG.md` also holds the
release notes Verlato drafted for itself while it was developed.

## 0.2.0 (unreleased)

### Changed
- Verlato is now licensed under Apache-2.0 (previously MIT), and the repository is `buchochelliq-labs/verlato-core`.
- Verlato is now a native (Rust) program, with the IntentumDiff engine linked in-process. The Python
  implementation is retired; every command, the editor service and the MCP server run natively.
- The `verlato` Python package is a thin wrapper over the native runtime (PyO3 bindings, abi3 wheels for
  CPython 3.12+). It ships the IntentumDiff parser bundle and has no runtime dependencies
  (`intentumdiff-python` and `fastmcp` are no longer needed).
- Unknown commands are usage errors (exit code 2) instead of being handed to `verlato-py`.

### Added
- A documentation site (Zensical, `mkdocs.yml`, published to GitHub Pages): getting started, a user guide
  from the CLI to the VS Code extension, a reference (CLI, `verlato.json`, environment variables, service
  and MCP protocol, Python API, output formats) and a technical guide. The CLI reference is generated
  from `verlato --help` (`scripts/gen_cli_reference.py`), and the screenshots from the real CLI and the
  extension's view code (`scripts/screenshots/`).
- Every command and option now has a description in `verlato --help`.
- Python API: `verlato.run(argv)`, `verlato.call(method, params, repo=...)`, `verlato.engine_version()`,
  `verlato.parser_bundle()`.
- Release bundles on GitHub Releases: `verlato-v<version>-<target>.tar.gz` (`.zip` on Windows) with the
  `verlato` executable and the parser bundle in `wasm/`, plus `SHA256SUMS`. Usable from the VS Code extension
  via `verlato.cliPath`.
- Releases are made with rab (`rab.yml`, `.github/workflows/release.yml`); see [Releasing](technical/releasing.md).
- Extension points for embedding binaries (`verlato::cli::run_with` and `verlato::extensions`): forge adapters,
  model providers, a policy hook, event sinks, extra subcommands and service methods. Verlato is open core; see
  [Open core](open-core.md).

### Removed
- The `verlato-py` command, `VERLATO_PYTHON_CLI`/`VERLATO_TRACE_DELEGATION`, the `mcp` extra, and
  in-process registration of suggestion cache backends (only `json` was ever available to the CLI).

## 0.1.x

The original Python implementation (the `verlato` package before the Rust runtime, earlier
named ReleasePilot): PR-first grouping, Conventional Commit parsing, deterministic
classification, IntentumDiff semantic evidence, model providers, suggestions, the stdio
service, the MCP server and the VS Code extension. Its behaviour is preserved by the
[golden parity suite](technical/testing.md#the-golden-parity-suite).
