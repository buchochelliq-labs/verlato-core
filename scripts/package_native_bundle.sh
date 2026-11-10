#!/usr/bin/env bash
# Build the native release bundle for one target: an archive holding the `verlato` executable,
# the IntentumDiff parser bundle in `wasm/` beside it (where the executable looks for it), and
# LICENSE.md and README.md.
#
# Usage: scripts/package_native_bundle.sh TARGET [VERSION]
#   Writes target/verlato-bundle/verlato-v<VERSION>-<TARGET>.tar.gz (.zip for Windows targets).
#   VERSION defaults to $VERSION (set by the release build), else the workspace version.
# rab's release build runs it as a `command` artifact (see rab.yml); it also works locally.
set -euo pipefail

target="${1:?usage: package_native_bundle.sh TARGET [VERSION]}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python="$(scripts/find_python.sh)"
version="${2:-${VERSION:-$("$python" -c 'import tomllib;print(tomllib.load(open("Cargo.toml","rb"))["workspace"]["package"]["version"])')}}"

rustup target add "$target" >/dev/null 2>&1 || true
cargo build --release --locked -p verlato --bin verlato --target "$target"

exe="verlato"
case "$target" in *windows*) exe="verlato.exe" ;; esac
name="verlato-v$version-$target"
stage="target/verlato-bundle/$name"
rm -rf "$stage"
mkdir -p "$stage"
cp "target/$target/release/$exe" "$stage/$exe"
cp LICENSE.md README.md "$stage/"
scripts/fetch_intentumdiff_parsers.sh "$stage/wasm"

"$python" - "$stage" "$exe" <<'PY'
import os
import sys
import tarfile
import zipfile
from pathlib import Path

stage, exe = Path(sys.argv[1]), sys.argv[2]
name = stage.name
files = sorted(p for p in stage.rglob("*") if p.is_file())
if exe.endswith(".exe"):
    target = stage.with_name(f"{name}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, f"{name}/{file.relative_to(stage).as_posix()}")
else:
    target = stage.with_name(f"{name}.tar.gz")
    with tarfile.open(target, "w:gz") as archive:
        for file in files:
            info = archive.gettarinfo(str(file), f"{name}/{file.relative_to(stage).as_posix()}")
            info.mode = 0o755 if file.name == exe else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(file, "rb") as handle:
                archive.addfile(info, handle)
print(f"{target} ({os.path.getsize(target) / 1e6:.1f} MB)", file=sys.stderr)
PY
if [ -n "${OUT:-}" ]; then
  mkdir -p "$OUT"
  cp target/verlato-bundle/"$name".tar.gz target/verlato-bundle/"$name".zip "$OUT/" 2>/dev/null || true
fi
