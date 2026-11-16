# Building from source

You need a stable Rust toolchain, git, Python 3.12+ (for the Python package) and Node 22+
(for the VS Code extension and screenshots).

```sh
git clone https://github.com/buchochelliq-labs/verlato-core
cd verlato-core
```

## The native executable

```sh
cargo build --release -p verlato
scripts/fetch_intentumdiff_parsers.sh          # parser bundle -> target/release/wasm
target/release/verlato doctor engine
```

`fetch_intentumdiff_parsers.sh [DEST]` downloads the pinned, checksum-verified IntentumDiff
parser bundle (by default next to the release binary, where Verlato finds it). The engine
itself is a git dependency on
[`intentumdiff-core`](https://github.com/buchochelliq-labs/intentumdiff-core), fetched by
Cargo. To build without the engine (git evidence only), use `--no-default-features`.

A release-style bundle (executable, `wasm/`, licence and README in one archive) is made by
`scripts/package_native_bundle.sh` and checked by `scripts/test_native_bundle.sh`; see
[Releasing](releasing.md).

## The Python package

The package is built with [maturin](https://www.maturin.rs) from `crates/verlato-python` (the
`verlato._native` extension) and `python/verlato`:

```sh
python -m venv .venv && . .venv/bin/activate
scripts/fetch_intentumdiff_parsers.sh python/verlato/wasm   # the bundle the wheel ships
python -m pip install -e '.[dev]'                            # builds the extension
verlato --version
```

After changing Rust code, rebuild with `maturin develop --release` (or reinstall). A wheel:
`maturin build --release`.

## The VS Code extension

```sh
cd vscode-extension
npm ci
npm run build                     # TypeScript -> dist/
npx @vscode/vsce package          # verlato-vscode-<version>.vsix
code --install-extension verlato-vscode-*.vsix --force
```

For development, open the repository in VS Code and press **F5**: `.vscode/launch.json`
starts an Extension Development Host with the extension loaded. To use your build of the
native executable, the extension finds `target/release/verlato` next to `vscode-extension/`
automatically, or set `verlato.cliPath`.

On Windows, `scripts\install_vscode_extension.cmd` does all of it: installs the package into
the repository's `.venv`, builds and packages the extension, installs it, and writes
`verlato.cliPath` (options `-SkipPythonInstall`, `-SkipNpmInstall`, `-SkipPackage`,
`-SkipConfigureCliPath`, `-PreRelease`).

## This documentation

```sh
python -m pip install "zensical==0.0.74"
zensical serve           # live preview at http://127.0.0.1:8000
zensical build --strict  # the site in site/
```

The CLI reference page is generated from the binary: after changing commands or flags, run
`scripts/gen_cli_reference.py` (CI runs it with `--check`). Screenshots are regenerated with
`scripts/screenshots/capture.sh`; see [Screenshots](screenshots.md). The site is deployed to
GitHub Pages from `main` by `.github/workflows/docs.yml`.
