# Public-readiness review (verlato-core)

This review was done before making `buchochelliq-labs/verlato-core` public, on 2026-11-10 at commit `7212792`.
Making the repository public publishes **its whole history**, so the scans below cover every reachable commit
(`git log --all -p`, 101 commits), not only the working tree.

## How it was scanned

- **Regex scan over the full history:** looked for GitHub tokens (`ghp_`, `gho_`, `github_pat_`), OpenAI and
  Anthropic keys (`sk-`, `sk-ant-`), AWS keys (`AKIA…`), Slack tokens (`xox?-`), Google API keys (`AIza…`),
  Hugging Face tokens (`hf_…`), PEM private keys, and quoted `password` / `secret` / `token` / `api_key`
  assignments.
- **Files ever added:** searched every file ever added for `.env`, `*.pem`, `*.key`, `id_rsa`, `.npmrc`,
  `.pypirc` and anything named like a secret or credential.
- **History content:** searched for email addresses, URLs and hosts, personal home-directory paths, and
  personal names or handles.
- **Working tree:** ran `detect-secrets` 1.5.0 over the whole working tree, with build output and caches
  excluded.

## Findings

| # | Finding | Where | Verdict | Action |
|---|---|---|---|---|
| 1 | Token-shaped strings (`ghp_abcdefghijklmnopqrstuvwxyz123456`, `sk-testsecretvalue123456`, `AKIAABCDEFGHIJKLMNOP`, `sk-abcdefghijklmnopqrstuvwx`) | Redaction tests (history and `tests/test_rust_parity.py`) | Obvious fakes that test the model-log redactor | None |
| 2 | High-entropy hex strings | `.github/rab/manifest.json`, `scripts/fetch_intentumdiff_parsers.sh`, `crates/verlato/src/engine.rs`, extension tests, parser provenance | SHA-256 checksums and git revisions, not secrets | None |
| 3 | No `.env`, key, credential or registry-config file was ever committed | History | Clean | None |
| 4 | Hosts are public: GitHub, PyPI, npm, crates.io, Open VSX, Marketplace, `models.github.ai`, `models.inference.ai.azure.com`; the rest are loopback or `*.test` / `example.*` test fixtures | History | Clean | None |
| 5 | Personal Windows path in the extension README's `verlato.cliPath` example | `vscode-extension/README.md` | Personal, low sensitivity | **Replaced** with `C:\path\to\verlato.exe`, and scrubbed from history |
| 6 | The maintainer's personal commit email (69 commits) and an earlier GitHub handle in old merge-commit titles, `CHANGELOG.md` and `.github/release.md` | History, changelog | The maintainer's own identity | **Rewritten in history:** authorship now uses the maintainer's GitHub `noreply` address, and the old handle is replaced with the current one |
| 7 | `verlato_memory.md`, an AI-assistant conversation memory describing internal decisions | Working tree and history | Internal notes, nothing secret | **Removed** from the working tree and from every commit |
| 8 | `.verlato_state.json`, Verlato's own local state from dogfooding | Working tree and history | Local tool state that shouldn't be versioned | **Removed** from every commit and added to `.gitignore` |
| 9 | `tests/golden/parity` (18 MB) | Working tree | Recorded outputs of the retired Python implementation over synthetic fixture repos. No absolute paths, home directories or personal data | Keep. It's the parity oracle. |
| 10 | `AGENT.md`, `BACKLOG.md`, `performance_backlog.md`, `.github/copilot-instructions.md`, `.codex/skills/` | Working tree | Ordinary contributor and agent guidance. No paid-feature plans | Keep. Paid features are described only in [Open core](../open-core.md) |
| 11 | `.github/release.md` and `CHANGELOG.md` | Working tree | Dogfooded release-note drafts, including old ReleasePilot-era entries | Keep. They're the product's default draft targets. |

## History scrub

The public repository was created from a rewritten copy of the private history, using `git filter-repo`:

- **Authorship:** commits authored with the personal email now use the maintainer's GitHub `noreply` address.
- **Old handle:** replaced with the current handle in commit messages and file contents.
- **Personal path:** the Windows home-directory path is replaced.
- **Files:** `verlato_memory.md` and `.verlato_state.json` are removed from every commit.

None of the original pull-request refs were pushed. Afterwards, a fresh clone of every branch was checked for each of these
strings and files and none was found. The private pre-scrub repository is kept as an archive.

After going public, turn on secret scanning and push protection (Settings → Code security).
