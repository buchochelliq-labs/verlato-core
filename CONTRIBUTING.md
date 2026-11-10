# Contributing to Verlato

Thanks for helping. The full developer documentation is in the technical guide:
[building](docs/technical/building.md), [testing](docs/technical/testing.md),
[architecture](docs/technical/architecture.md) and [releasing](docs/technical/releasing.md)
(published at <https://buchochelliq-labs.github.io/verlato-core/technical/>).

## Development workflow

Verlato is Rust (`crates/verlato`, plus the PyO3 bindings in `crates/verlato-python`) with a
thin Python package (`python/verlato`), a VS Code extension (`vscode-extension/`) and a
documentation site (`docs/`, built with Zensical). You need a stable Rust toolchain, Python
3.12+ and Node 22+.

1. Create a branch for your change.
2. Set up a virtualenv with the package built from your checkout:
   ```bash
   python -m venv .venv && . .venv/bin/activate
   scripts/fetch_intentumdiff_parsers.sh python/verlato/wasm   # the parser bundle the wheel ships
   python -m pip install -e '.[dev]'                            # builds the extension with maturin
   ```
   After changing Rust code, rebuild with `maturin develop --release` (or reinstall). The native
   executable alone: `cargo build --release -p verlato && scripts/fetch_intentumdiff_parsers.sh`.
3. Run the checks before committing:
   ```bash
   cargo fmt --all --check
   cargo clippy --workspace --all-targets -- -D warnings
   cargo test --workspace
   ruff check .
   mypy
   pytest                                   # package tests + parity replay (tests/golden)
   (cd vscode-extension && npm test)
   python3 .github/rab/scripts/release.py check
   python3 scripts/gen_cli_reference.py --check
   zensical build --strict                  # pip install zensical==0.0.74
   ```
4. Keep the documentation current. If you change commands or flags, regenerate the CLI
   reference (`scripts/gen_cli_reference.py`); if you change the views or CLI output, consider
   regenerating the screenshots (`scripts/screenshots/capture.sh`).

The parity suite compares the native runtime with recorded answers of the retired Python
implementation (`tests/golden/README.md`). New behaviour cannot be recorded there: test it with
expected values instead (Rust unit tests or `tests/test_python_package.py`).

Releases are cut with rab; see [Releasing](docs/technical/releasing.md). Do not edit
`.github/rab/` (reinstall rab instead).

## Decision log

When landing notable architecture changes, capture:

- **Context**: what problem was observed?
- **Decision**: what was chosen?
- **Consequences**: what trade-offs were accepted?

Keep entries in pull request descriptions. Changes to the open-core boundary are recorded in
[docs/open-core.md](docs/open-core.md).
