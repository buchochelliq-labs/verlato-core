#!/usr/bin/env bash
# Provision the IntentumDiff parser bundle (parser_manifest.json + *.wasm components) that the
# native verlato binary loads. The components are extracted from the pinned, checksum-verified
# intentumdiff-python wheel on PyPI (the .wasm components are platform independent).
#
# Usage: scripts/fetch_intentumdiff_parsers.sh [DEST_DIR]
#   DEST_DIR defaults to target/release/wasm (next to the release binary).
set -euo pipefail

WHEEL_URL="https://files.pythonhosted.org/packages/c7/04/a9ae43b301e6aa759a57bba25accb3ddc27b6187c1cc2fcba5cb238bdd83/intentumdiff_python-0.0.2b2-py3-none-manylinux_2_39_x86_64.whl"
WHEEL_SHA256="8bf9cf778a315e54b6c62fd00029c1c73c555d30fe8d39acd7a190e098dc0319"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:-$repo_root/target/release/wasm}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Downloading IntentumDiff 0.0.2b2 parser bundle..." >&2
curl -fsSL "$WHEEL_URL" -o "$work/intentumdiff.whl"
actual="$(sha256sum "$work/intentumdiff.whl" | cut -d' ' -f1)"
if [ "$actual" != "$WHEEL_SHA256" ]; then
  echo "checksum mismatch: expected $WHEEL_SHA256, got $actual" >&2
  exit 1
fi
unzip -q "$work/intentumdiff.whl" 'intentumdiff/wasm/*' -d "$work/extract"
test -f "$work/extract/intentumdiff/wasm/parser_manifest.json" || { echo "parser_manifest.json missing from wheel" >&2; exit 1; }
mkdir -p "$dest"
cp -R "$work/extract/intentumdiff/wasm/." "$dest/"
echo "Installed $(ls "$dest"/*.wasm | wc -l | tr -d ' ') parser components into $dest" >&2
