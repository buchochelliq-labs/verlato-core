#!/usr/bin/env bash
# Provision the IntentumDiff parser bundle (parser_manifest.json + *.wasm components) that the
# native verlato engine loads. This script is the single pinned source of the bundle: the
# components are extracted from the pinned, checksum-verified intentumdiff-python wheel on PyPI
# (the .wasm components are platform independent). The pin must match the intentumdiff-core
# revision in Cargo.toml (see docs/technical/releasing.md).
#
# Usage: scripts/fetch_intentumdiff_parsers.sh [DEST_DIR]
#   DEST_DIR defaults to target/release/wasm (next to the release binary). The Python package
#   build uses python/verlato/wasm; release bundles use wasm/ beside the binary.
#
# Every component the manifest names must be present, and when the manifest records a SHA-256
# for a component (`sha256`), the component must match it. Needs bash, curl and python3; works
# on Linux, macOS and Windows (Git Bash).
set -euo pipefail

WHEEL_URL="https://files.pythonhosted.org/packages/92/d1/7978e6627015936e593c7a73ff60df3a53411c929f8f13ad130ba4775f08/intentumdiff_python-0.0.3b1-py3-none-manylinux_2_39_x86_64.whl"
WHEEL_SHA256="42f473ae34dce6d78dfe19c86c3bf1c8967ba658b6059c8185b669df78ae34ec"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:-$repo_root/target/release/wasm}"
python="$("$repo_root/scripts/find_python.sh")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Downloading IntentumDiff 0.0.3b1 parser bundle..." >&2
curl -fsSL --retry 3 "$WHEEL_URL" -o "$work/intentumdiff.whl"

"$python" - "$work/intentumdiff.whl" "$WHEEL_SHA256" "$dest" <<'PY'
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

wheel, expected, dest = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
if actual != expected:
    sys.exit(f"checksum mismatch: expected {expected}, got {actual}")

prefix = "intentumdiff/wasm/"
with zipfile.ZipFile(wheel) as archive:
    members = [name for name in archive.namelist() if name.startswith(prefix) and not name.endswith("/")]
    if prefix + "parser_manifest.json" not in members:
        sys.exit("parser_manifest.json missing from wheel")
    staging = dest.with_name(dest.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    for name in members:
        target = staging / name[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))

manifest = json.loads((staging / "parser_manifest.json").read_text(encoding="utf-8"))
parsers = manifest.get("parsers") or {}
entries = parsers.values() if isinstance(parsers, dict) else parsers
verified = 0
for entry in entries:
    component = staging / entry["wasm"]
    if not component.is_file():
        sys.exit(f"parser_manifest.json names {entry['wasm']}, which the bundle lacks")
    recorded = entry.get("sha256")
    if recorded:
        digest = hashlib.sha256(component.read_bytes()).hexdigest()
        if digest != recorded:
            sys.exit(f"{entry['wasm']}: checksum mismatch: manifest {recorded}, got {digest}")
        verified += 1

shutil.rmtree(dest, ignore_errors=True)
dest.parent.mkdir(parents=True, exist_ok=True)
staging.rename(dest)
count = len(list(dest.glob("*.wasm")))
note = f"{verified} checked against manifest checksums" if verified else "the manifest records no checksums"
print(f"Installed {count} parser components into {dest} ({note})", file=sys.stderr)
PY
