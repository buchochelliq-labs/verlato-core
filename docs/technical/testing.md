# Testing

Run everything before opening a pull request:

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
ruff check .
mypy
pytest                                        # needs the package installed (maturin)
(cd vscode-extension && npm test)
python3 .github/rab/scripts/release.py check
python3 scripts/gen_cli_reference.py --check
zensical build --strict                       # the documentation
```

CI (`.github/workflows/ci.yml`) runs the same checks on every pull request, and the release
workflow reruns them on the tagged commit.

## What the suites cover

| Suite | Where | What |
|---|---|---|
| Rust unit tests | `crates/verlato/src/**` (`#[cfg(test)]`) | classification, grouping, rendering, state migrations, providers (with fake servers), the service and MCP protocols, `pyjson` |
| Extension points | `crates/verlato/tests/extensions.rs` | an out-of-crate extension registering one of every point |
| Python package | `tests/test_python_package.py` | the bindings: `run`, `call`, `ServiceError`, the packaged parser bundle, version agreement with Cargo |
| Parity | `tests/test_rust_parity.py` + `tests/golden/parity/` | the native runtime against recorded answers of the retired Python implementation |
| Extension | `vscode-extension/test/*.test.js` | webview markup, CLI resolution, model environment, semantic evidence rendering, cancellation |

## The golden parity suite

Verlato was first written in Python. Before the Python implementation was retired, its
answers to every scenario in `tests/test_rust_parity.py` (command output, files written,
requests sent to fake model servers, MCP sessions) were recorded in `tests/golden/parity/`.
The suite now replays each scenario against the native runtime and requires the same answers,
byte for byte, apart from documented differences (engine version strings, and the repository
README that the MCP guidance resources serve). See `tests/golden/README.md` for the format.

New behaviour cannot be recorded there any more: test it with expected values in Rust unit
tests or `tests/test_python_package.py`.

## Determinism

Tests make no network calls: model providers are tested against local fake servers and the
`mock` provider; git fixtures use fixed authors and dates, so commit IDs are stable. The
IntentumDiff parser bundle is needed for engine tests (`scripts/fetch_intentumdiff_parsers.sh`).
