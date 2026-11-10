#!/usr/bin/env bash
# Check a native release bundle built by package_native_bundle.sh: its layout, and (when the
# runner can execute it) that the executable runs and finds the parser bundle beside it.
#
# Usage: scripts/test_native_bundle.sh ARCHIVE... (rab passes the built files in $FILES)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$("$repo_root/scripts/find_python.sh")"
# shellcheck disable=SC2086 # FILES is a space-separated list
set -- "$@" ${FILES:-}
[ "$#" -gt 0 ] || { echo "no bundle to test" >&2; exit 1; }

for archive in "$@"; do
  work="$(mktemp -d)"
  "$python" - "$archive" "$work" <<'PY'
import shutil
import sys
from pathlib import Path

archive, work = Path(sys.argv[1]), Path(sys.argv[2])
shutil.unpack_archive(str(archive), str(work))
roots = [p for p in work.iterdir() if p.is_dir()]
assert len(roots) == 1, f"{archive.name}: expected one top-level directory, found {roots}"
root = roots[0]
assert archive.name.startswith(root.name), f"{archive.name}: top-level directory {root.name}"
exe = root / ("verlato.exe" if archive.name.endswith(".zip") else "verlato")
for required in (exe, root / "LICENSE.md", root / "README.md", root / "wasm" / "parser_manifest.json"):
    assert required.is_file(), f"{archive.name}: missing {required.relative_to(work)}"
assert len(list((root / "wasm").glob("*.wasm"))) > 10, f"{archive.name}: parser components missing"
print(root)
PY
  root="$(find "$work" -mindepth 1 -maxdepth 1 -type d)"
  exe="$root/verlato"
  [ -f "$exe.exe" ] && exe="$exe.exe"
  target="${archive##*/}"
  case "$target" in
    *x86_64-apple-darwin*) [ "$(uname -m)" = "x86_64" ] || { echo "skip running $target on $(uname -m)"; continue; } ;;
  esac
  "$exe" --version | grep -F "${VERSION:-}"
  (cd "$work" && env -u VERLATO_INTENTUMDIFF_WASM_DIR -u INTENTUMDIFF_WASM_DIR "$exe" doctor engine) \
    | "$python" -c 'import json,sys; info=json.load(sys.stdin); assert info["available"], info; print("engine", info["version"], "from", info["parser_bundle"])'
done
