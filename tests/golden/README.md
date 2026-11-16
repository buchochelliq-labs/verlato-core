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
  date (`<TODAY>`) are stored as placeholders and replaced with the current run's values. The
  test repositories use fixed commit dates, so their commit SHAs are stable.

Recording needs the Python implementation, so it is only possible from that commit:

```sh
git checkout 9098ce2
pip install -e .[dev,mcp]
cargo build --release -p verlato && scripts/fetch_intentumdiff_parsers.sh
VERLATO_GOLDEN_RECORD=1 VERLATO_RUST_BIN=$PWD/target/release/verlato pytest tests/test_rust_parity.py
```

A replay asserts that each test asks the oracle exactly the recorded questions in the recorded
order, so a changed scenario fails loudly instead of comparing against the wrong answer. A new
scenario cannot be recorded any more; compare the native runtime against expected values in
the test instead.
