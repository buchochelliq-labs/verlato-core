# Contributing to Verlato

## Development workflow

Verlato is Rust (`crates/verlato`, plus the PyO3 bindings in `crates/verlato-python`) with a thin
Python package (`python/verlato`) and a VS Code extension (`vscode-extension/`). You need a stable
Rust toolchain, Python 3.12+ and Node 24.

1. Create a branch for your change.
2. Set up a virtualenv with the package built from your checkout:
   ```bash
   python -m venv .venv && . .venv/bin/activate
   scripts/fetch_intentumdiff_parsers.sh python/verlato/wasm   # the parser bundle the wheel ships
   python -m pip install -e '.[dev]'                            # builds the extension with maturin
   ```
   After changing Rust code, rebuild with `maturin develop --release` (or reinstall). The native
   executable alone: `cargo build --release -p verlato && scripts/fetch_intentumdiff_parsers.sh`.
3. Run local checks before committing:
   ```bash
   cargo fmt --all --check
   cargo clippy --workspace --all-targets -- -D warnings
   cargo test --workspace
   ruff check .
   mypy
   pytest                                   # package tests + parity replay (tests/golden)
   (cd vscode-extension && npm test)
   python3 .github/rab/scripts/release.py check
   ```
4. Update `ARCHITECTURE.md`, `BACKLOG.md` and `docs/RUST_MIGRATION.md` for structural changes.

The parity suite compares the native runtime with recorded answers of the retired Python
implementation (`tests/golden/README.md`). New behaviour cannot be recorded there: test it with
expected values instead (Rust unit tests or `tests/test_python_package.py`).

Releases are cut with rab; see [docs/RELEASING.md](docs/RELEASING.md). Do not edit `.github/rab/`
(reinstall rab instead).

## Decision log

When landing notable architecture changes, capture:

- **Context**: what problem was observed?
- **Decision**: what was chosen?
- **Consequences**: what trade-offs were accepted?

Keep entries in pull request descriptions or in an ADR if introduced later.
