"""Verlato: semantic release notes powered by IntentumDiff.

This package is a thin wrapper over Verlato's native (Rust) implementation, exposed through the
``verlato._native`` extension module. The ``verlato`` and ``verlato-mcp`` commands run it; the
functions below run it from Python.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from verlato import _native

__all__ = ["ServiceError", "__version__", "call", "engine_version", "parser_bundle", "run"]

__version__: str = _native.version()

_WASM_ENV = ("VERLATO_INTENTUMDIFF_WASM_DIR", "INTENTUMDIFF_WASM_DIR")


class ServiceError(RuntimeError):
    """A service request failed; ``code`` is the service's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def parser_bundle() -> Path | None:
    """The IntentumDiff parser bundle shipped in this package, if the build included it."""
    bundled = Path(__file__).with_name("wasm")
    return bundled if (bundled / "parser_manifest.json").is_file() else None


def _configure() -> None:
    """Point the native engine at the packaged parser bundle unless the caller chose one."""
    if any(os.environ.get(name) for name in _WASM_ENV):
        return
    bundled = parser_bundle()
    if bundled is not None:
        os.environ["VERLATO_INTENTUMDIFF_WASM_DIR"] = str(bundled)


def run(argv: Sequence[str]) -> int:
    """Run the ``verlato`` command line with ``argv`` (without the program name) in this process.

    Output goes to this process's stdout and stderr; returns the exit code. The command runs in
    the current directory, without holding the GIL.
    """
    _configure()
    sys.stdout.flush()
    sys.stderr.flush()
    return _native.run([str(arg) for arg in argv])


def call(method: str, params: dict[str, Any] | None = None, *, repo: str | os.PathLike[str] | None = None) -> Any:
    """Answer one ``verlato service`` request in-process and return its result.

    ``repo`` is the repository directory (default: the current directory). Raises
    :class:`ServiceError` when the service answers with an error.
    """
    _configure()
    response = json.loads(_native.call(method, json.dumps(params or {}), None if repo is None else os.fspath(repo)))
    if not response.get("ok"):
        error = response.get("error") or {}
        raise ServiceError(str(error.get("code", "error")), str(error.get("message", "")))
    return response.get("result")


def engine_version() -> str:
    """The IntentumDiff engine build linked into this package."""
    return _native.engine_version()
