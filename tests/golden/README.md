# Golden recordings of the retired Python implementation

`parity/` holds what the Python implementation of Verlato answered in every scenario of
`tests/test_rust_parity.py`, so the native runtime keeps being checked against it after the
Python implementation was removed.

- **Recorded from:** commit `9098ce2` ("Share suggestion batch helpers with the doctor; native
  doctor adaptive advice"), the last commit with the Python implementation (`verlato/`,
  version 0.1.0), with intentumdiff-python 0.0.2b2.
- **One file per test:** each holds, in order, every Python invocation the test made: its
  arguments, working directory and stdin; its exit code, stdout and stderr; the files it
  changed under the test's temporary directory; the requests the test's fake model servers
  received from it; and, for MCP tests, each request and answer of the session.
- **Placeholders:** the test's temporary directory (`<TMP>`), the repository root (`<ROOT>`),
  the Python interpreter (`<PYTHON>`), fake server ports (`<PORTn>`) and the recording day's
  date (`<TODAY>`) are stored as placeholders and replaced with the current run's values.
- **Reproducible commits:** the test repositories use a fixed identity and fixed commit dates,
  and their git runs ignore the host's git configuration and `GIT_*` variables. Commit SHAs (and
  the item ids derived from them) are therefore the same on every machine. The first recording
  didn't isolate git: it picked up a host setting that signed every commit, so its SHAs
  couldn't be reproduced in CI. The recordings were made again with isolated git on 2026-11-17.
- **IntentumDiff 0.0.3:** the 21 scenarios that use the IntentumDiff engine were recorded again
  on 2026-11-19 with intentumdiff-python 0.0.3b1 (core `1c4bdbc5`). The Python implementation
  was first given the same changes the native runtime made for that release: the workarounds
  for core#297, #299 and #300 removed, signatures classified from `CHANGE_SIGNATURE`, signature
  changes made behavioural, incomplete reviews recorded as unreviewed and size limits as
  `too_large`. Python and Rust agreed on every scenario while recording. The git-engine
  scenarios keep their 0.0.2b2-era recordings, since that engine doesn't involve IntentumDiff.
  Those Python changes are kept as `intentumdiff-0.0.3.patch` next to this file.
- **Not replayed:** the Python MCP server's stderr is recorded but never compared. Its rich
  traceback box, which wraps paths so they can't be replaced by placeholders, is omitted.

Recording needs the Python implementation. `33ca31a`, the commit that added the recorder, still
has it. Record from a worktree of that commit, with the current test file, the current
repository files the MCP server serves as resources, the 0.0.3 patch and the native executable
built from the current checkout (paths relative to the current checkout):

```sh
git worktree add ../recording 33ca31a
git -C ../recording checkout main -- tests/test_rust_parity.py README.md AGENT.md \
  docs/technical/architecture.md .github/copilot-instructions.md \
  .codex/skills/verlato/SKILL.md verlato.sample.json
git -C ../recording apply "$PWD/tests/golden/intentumdiff-0.0.3.patch"
cargo build --release -p verlato && scripts/fetch_intentumdiff_parsers.sh
cd ../recording
python -m venv .venv && .venv/bin/pip install -e '.[dev,mcp]' intentumdiff-python==0.0.3b1
VERLATO_GOLDEN_RECORD=1 VERLATO_RUST_BIN=../verlato-core/target/release/verlato \
  .venv/bin/pytest tests/test_rust_parity.py -k '<the scenarios to record>'
```

Record only the scenarios that need it, and copy just those files back. Leave `COLUMNS` unset,
and check the MCP recordings for local paths in the Python server's stderr before committing.

A replay asserts that each test asks the oracle exactly the recorded questions in the recorded
order, so a changed scenario fails loudly instead of comparing against the wrong answer. A new
scenario cannot be recorded any more; compare the native runtime against expected values in
the test instead.
