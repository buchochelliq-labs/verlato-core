#!/usr/bin/env bash
# Build, test and package the VS Code extension as a Marketplace / Open VSX pre-release.
#
# rab runs this as the `command` artifact of the extension's package (rab.yml), with VERSION (the
# extension's version, plain x.y.z) and OUT (the output directory) set. It writes
# "$OUT/verlato-vscode-$VERSION.vsix", packaged with `vsce package --pre-release`: the Marketplace
# and Open VSX take only x.y.z versions, so a pre-release is marked in the vsix manifest
# (Microsoft.VisualStudio.Code.PreRelease) rather than in the version.
#
# Usage: scripts/package_extension.sh [VERSION] [OUT]   (defaults: package.json's version, ./dist)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ext="$repo_root/vscode-extension"
version="${1:-${VERSION:-$(node -p "require('$ext/package.json').version")}}"
out="${2:-${OUT:-$repo_root/dist}}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"

manifest_version="$(node -p "require('$ext/package.json').version")"
if [ "$manifest_version" != "$version" ]; then
  echo "package.json has version $manifest_version, but $version was asked for" >&2
  exit 1
fi

cd "$ext"
npm ci --no-audit --no-fund
npm test
npx --yes @vscode/vsce@4.1.0 package --pre-release --out "$out/verlato-vscode-$version.vsix"
echo "Packaged $out/verlato-vscode-$version.vsix (pre-release)" >&2
