"""Console entry points: ``verlato`` and ``verlato-mcp``."""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Sequence
from typing import NoReturn

from verlato import run


def _exit(argv: Sequence[str]) -> NoReturn:
    # The native command runs without the GIL, so Python's KeyboardInterrupt could not stop it:
    # let Ctrl-C terminate the process as it would the native executable.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(run(argv))


def main(argv: Sequence[str] | None = None) -> NoReturn:
    """``verlato``: the Verlato command line."""
    _exit(sys.argv[1:] if argv is None else argv)


def mcp(argv: Sequence[str] | None = None) -> NoReturn:
    """``verlato-mcp``: the Verlato MCP server on stdio (``verlato serve``)."""
    _exit(["serve", *(sys.argv[1:] if argv is None else argv)])
