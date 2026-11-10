#!/usr/bin/env python3
"""Generate docs/reference/cli.md from the real `verlato --help` output.

Usage: scripts/gen_cli_reference.py [--bin PATH] [--check]

Walks every subcommand clap lists, so the reference always matches the binary. `--check`
fails (exit 1) when the committed page is out of date instead of rewriting it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "reference" / "cli.md"

HEADER = """# CLI reference

This page is generated from the `--help` output of the `verlato` executable by
`scripts/gen_cli_reference.py`. Do not edit it by hand: change the command definitions in
`crates/verlato/src/cli.rs`, rebuild, and run the script again (CI-style check:
`scripts/gen_cli_reference.py --check`).

The [CLI walkthrough](../guide/cli.md) explains what the commands are for and how they fit
together. Options that every range command shares (`--base`, `--head`, `--from-latest-tag`,
`--from-latest-published`) are described in [Concepts](../guide/concepts.md#ranges).

`verlato-mcp` (from the Python package) is the same as `verlato serve`.
"""


def find_binary(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("VERLATO_BIN"), str(ROOT / "target" / "release" / "verlato"), shutil.which("verlato")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    sys.exit("verlato executable not found: build it (cargo build --release -p verlato) or pass --bin")


def help_text(binary: str, path: list[str]) -> str:
    env = {**os.environ, "COLUMNS": "100", "NO_COLOR": "1"}
    result = subprocess.run([binary, *path, "--help"], capture_output=True, text=True, env=env, check=True)
    return result.stdout.rstrip() + "\n"


def subcommands(text: str) -> list[str]:
    names: list[str] = []
    in_commands = False
    for line in text.splitlines():
        if line.strip() == "Commands:":
            in_commands = True
            continue
        if in_commands:
            if not line.startswith("  "):
                break
            match = re.match(r"^  ([a-z][a-z0-9-]*)\b", line)
            if match and match.group(1) != "help":
                names.append(match.group(1))
    return names


def walk(binary: str, path: list[str], out: list[str]) -> None:
    text = help_text(binary, path)
    level = min(len(path) + 1, 4)
    title = "verlato" + ("" if not path else " " + " ".join(path))
    out.append(f"{'#' * (level + 1)} `{title}`\n\n```text\n{text}```\n")
    for name in subcommands(text):
        walk(binary, [*path, name], out)


def render(binary: str) -> str:
    out: list[str] = [HEADER]
    walk(binary, [], out)
    # The binary's own name in usage lines is stable (`verlato`), so no path leaks in.
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", help="path to the verlato executable")
    parser.add_argument("--check", action="store_true", help="fail if docs/reference/cli.md is out of date")
    args = parser.parse_args()
    content = render(find_binary(args.bin))
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date; run scripts/gen_cli_reference.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
