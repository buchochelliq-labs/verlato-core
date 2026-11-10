#!/usr/bin/env bash
# Provision the IntentumDiff parser bundle (parser_manifest.json + *.wasm components) that the
# native verlato engine loads. This script is the single pinned source of the bundle: the
# components are extracted from the pinned, checksum-verified intentumdiff-python wheel on PyPI
# (the .wasm components are platform independent). The pin must match the intentumdiff-core
# revision in Cargo.toml (see docs/RELEASING.md).
#
# Usage: scripts/fetch_intentumdiff_parsers.sh [DEST_DIR]
#   DEST_DIR defaults to target/release/wasm (next to the release binary). The Python package
#   build uses python/verlato/wasm; release bundles use wasm/ beside the binary.
#
# Every component the manifest names must be present, and when the manifest records a SHA-256
# for a component (`sha256`), the component must match it. Needs bash, curl and python3; works
# on Linux, macOS and Windows (Git Bash).
set -euo pipefail

WHEEL_URL="https://files.pythonhosted.org/packages/c7/04/a9ae43b301e6aa759a57bba25accb3ddc27b6187c1cc2fcba5cb238bdd83/intentumdiff_python-0.0.2b2-py3-none-manylinux_2_39_x86_64.whl"
WHEEL_SHA256="8bf9cf778a315e54b6c62fd00029c1c73c555d30fe8d39acd7a190e098dc0319"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:-$repo_root/target/release/wasm}"
python="$("$repo_root/scripts/find_python.sh")"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Downloading IntentumDiff 0.0.2b2 parser bundle..." >&2
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
