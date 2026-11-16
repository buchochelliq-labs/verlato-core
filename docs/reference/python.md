# Python API

The [`verlato` package](https://pypi.org/project/verlato/) wraps the native runtime with
PyO3 bindings. Everything runs in-process: no subprocess, and no Python reimplementation.

```python
import verlato

# The command line, in this process. Output goes to stdout/stderr; returns the exit code.
code = verlato.run(["draft", "preview", "--from-latest-tag", "--version", "v1.1.0"])

# One editor-service request, answered in-process; returns the parsed result.
draft = verlato.call("draft.get", {"request": {"base": "v1.0.0", "version": "v1.1.0"}}, repo=".")
for section in draft["sections"]:
    for item in section["items"]:
        print(section["title"], item["id"], item["status"], item["summary"])

verlato.__version__        # "0.2.0"
verlato.engine_version()   # "intentumdiff-core@7ae58e9237e0"
verlato.parser_bundle()    # Path to the packaged parser bundle, or None
```

## Functions

`verlato.run(argv: Sequence[str]) -> int`
:   Run the `verlato` command line with `argv` (without the program name) in the current
    directory. Returns the exit code; never raises `SystemExit`. The command runs without
    holding the GIL.

`verlato.call(method: str, params: dict | None = None, *, repo: str | PathLike | None = None) -> Any`
:   Answer one [editor service](protocol.md#the-editor-service) request (`draft.get`,
    `draft.review`, `suggestion.items`, `commit.conventional`, ...) for the repository at
    `repo` (default: the current directory) and return its `result`. Raises
    `verlato.ServiceError` when the service answers with an error.

`verlato.ServiceError`
:   A `RuntimeError` with `.code` (for example `invalid_request` or `handler_error`) and
    `.message`.

`verlato.engine_version() -> str`
:   The IntentumDiff engine build linked into the package.

`verlato.parser_bundle() -> Path | None`
:   The parser bundle shipped in the package. `run` and `call` use it automatically unless
    `VERLATO_INTENTUMDIFF_WASM_DIR` or `INTENTUMDIFF_WASM_DIR` is set.

`verlato.__version__`
:   The package version (the same as `verlato --version`).

## Commands

The package installs two commands, and `python -m verlato` works too:

- `verlato`: the command line.
- `verlato-mcp`: the MCP server, the same as `verlato serve`.

Ctrl-C stops a running command immediately, as it does for the native executable.

## Low-level module

`verlato._native` is the compiled extension (`run(argv)`, `call(method, params_json, repo)`
returning the raw JSON response envelope, `version()` and `engine_version()`). Prefer the
functions above; `_native` may change between releases.
