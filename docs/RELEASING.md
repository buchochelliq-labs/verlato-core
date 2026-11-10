# Releasing Verlato

Verlato releases with **rab**, the buchochelliq-labs Release Asset Bundle
([buchochelliq-labs/release-asset-bundle](https://github.com/buchochelliq-labs/release-asset-bundle)).
`rab.yml` declares what is released; `python3 .github/rab/scripts/release.py tag` starts a release; the
`release` workflow (`.github/workflows/release.yml`) checks it, reruns CI on the tagged commit, builds every
artifact, publishes what is not yet published, and installs each package as a stranger would.

## What a release publishes

One version for everything: `[workspace.package] version` in `Cargo.toml`. The crates inherit it, and
`pyproject.toml` declares `dynamic = ["version"]` with `[tool.maturin] manifest-path`, so maturin (and rab)
read it from Cargo.

| Package (`rab.yml`) | Registry | Record tag | What |
|---|---|---|---|
| `verlato-native` | GitHub Releases | `v<version>` | per-platform native bundles and `SHA256SUMS` |
| `verlato` | PyPI | `verlato-v<version>` | abi3 wheels (CPython 3.12+) and the sdist |

The release itself is tagged `release-<version>` (rab's default), named after the `verlato` package.

### Native bundles (GitHub release assets)

One archive per target, built by `scripts/package_native_bundle.sh` (a rab `command` artifact per runner)
and checked by `scripts/test_native_bundle.sh`:

```text
verlato-v<version>-<target>.tar.gz            (.zip for Windows)
└── verlato-v<version>-<target>/
    ├── verlato                               (verlato.exe on Windows; mode 0755)
    ├── wasm/                                 the IntentumDiff parser bundle
    │   ├── parser_manifest.json
    │   └── *_parser.wasm
    ├── LICENSE.md
    └── README.md
SHA256SUMS                                    sha256sum format, over every asset
```

Targets: `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`, `aarch64-apple-darwin`,
`x86_64-apple-darwin` (cross-compiled on Apple Silicon; its test checks the layout but cannot run it) and
`x86_64-pc-windows-msvc`. The executable finds `wasm/` beside itself, so a bundle works unpacked anywhere.
Each asset also gets a `<file>.sha256` from rab.

To use a bundle: unpack it, then put its directory on `PATH` or point the VS Code extension's
`verlato.cliPath` setting at its `verlato` executable. The extension also picks up an unpacked bundle placed in
its own `bin/` directory (executable and `wasm/` side by side). It does not download bundles itself.

### Python package (PyPI)

rab's `maturin` artifact builds one abi3 wheel per target (the same five) with `PyO3/maturin-action`, plus
the sdist. Its `prepare` step runs `scripts/fetch_intentumdiff_parsers.sh python/verlato/wasm` before
maturin, so the wheels (and the sdist) carry the parser bundle at `verlato/wasm`. Each wheel's `test`
(`scripts/test_wheel.sh`) installs it into a fresh virtualenv and runs `verlato --help`, `verlato --version`,
`verlato doctor engine` (which must find the packaged bundle) and the Python API. The `x86_64-apple-darwin`
wheel is not tested (cross-compiled).

Size: the parser bundle is about 63 MB uncompressed. A locally built `x86_64` Linux wheel is 25.6 MB and the
sdist 11.9 MB, both well under PyPI's 100 MB per-file limit (a native bundle is about 22 MB).

An sdist builds the extension from source: it needs a Rust toolchain and network access to fetch the
pinned `intentumdiff-core` git dependency from github.com (a public repository).

### The parser bundle's pin

`scripts/fetch_intentumdiff_parsers.sh` is the single pinned source of the parser bundle: a URL and SHA-256
of the `intentumdiff-python` wheel the components are extracted from. It also checks that every component
`parser_manifest.json` names is present, and each component's SHA-256 when the manifest records one. The
parsers must match the engine: **when the `intentumdiff-core` revision in `Cargo.toml` changes, update the
pin in the same commit.**

## One-time setup

1. **Default branch.** rab releases only from the branch in `rab.yml` (`release: branch: main`); every tag
   must point at a commit on it. Merge the release work into `main` first.
2. **PyPI trusted publisher.** Create a `pypi` environment in the repository settings (Settings →
   Environments; optionally limit it to `main` and `release-*` tags, and add required reviewers). On PyPI, the
   `verlato` project does not exist yet, so add a *pending* trusted publisher from your account's publishing
   page (<https://pypi.org/manage/account/publishing/>): owner `buchochelliq-labs`, repository `verlato`,
   workflow `release.yml`, environment `pypi`. The first upload creates the project. No token is stored.
3. **GitHub Releases** need nothing: the `publish-github` job uses the workflow's token with
   `contents: write`.
4. **GitHub Actions** must be able to run jobs (the organisation's Actions billing), including the
   `ubuntu-24.04-arm`, `macos-latest` and `windows-latest` runners the builds use.

## Cutting a release

1. Bump `[workspace.package] version` in `Cargo.toml`, run `cargo update -w` (so `Cargo.lock` agrees; the
   builds use `--locked`), then copy the version into `rab.yml`:
   ```sh
   python3 .github/rab/scripts/release.py sync
   python3 .github/rab/scripts/release.py check
   ```
   Add the version's section to `CHANGELOG.md`. Merge the PR into `main`.
2. On `main`:
   ```sh
   git switch main && git pull
   python3 .github/rab/scripts/release.py status
   python3 .github/rab/scripts/release.py tag --dry-run
   python3 .github/rab/scripts/release.py tag
   ```
   `tag` pushes the release and record tags and follows the run (it needs the GitHub CLI, `gh auth login`).

A published version is permanent: to fix a failed release, fix the cause and run `release.py tag` again (rab
publishes only what is missing). See rab's
[recovery guide](https://github.com/buchochelliq-labs/release-asset-bundle/blob/main/docs/recovery.md).

## Keeping rab current

`.github/rab/` and `.github/workflows/rab-build.yml` are an installed, pinned copy; do not edit them (CI runs
`release.py vendored`, which fails on any change). To update rab, pull its repository and run
`python3 ../release-asset-bundle/scripts/release.py install` from the repository root, then open a PR.
`.github/workflows/release.yml` is Verlato's own file: it keeps only the `publish-github` and `publish-pypi`
jobs; the template's crates.io, npm, VS Code Marketplace, Homebrew and Chocolatey jobs were removed because
Verlato publishes none of those. The VS Code extension is not published by rab yet (its marketplace
publisher and packaging of the native runtime are still open).
