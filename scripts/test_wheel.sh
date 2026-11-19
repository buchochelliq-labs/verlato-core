#!/usr/bin/env bash
# Install a built wheel into a fresh virtualenv and use it as a stranger would: the `verlato`
# command, the packaged parser bundle, and the Python API.
#
# Usage: scripts/test_wheel.sh [WHEEL]   (default: the one wheel in $DIST, as rab sets it)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$("$repo_root/scripts/find_python.sh")"
wheel="${1:-$(ls "${DIST:?set DIST or pass a wheel}"/*.whl | head -n 1)}"
venv="$(mktemp -d)/venv"
"$python" -m venv "$venv"
bin="$venv/bin"
[ -d "$venv/Scripts" ] && bin="$venv/Scripts"
"$bin/python" -m pip install --quiet --no-index --no-deps "$wheel"

work="$(mktemp -d)"
cd "$work"
"$bin/verlato" --help >/dev/null
# rab's VERSION is the PyPI (PEP 440) version, e.g. 0.2.0b1: the package's __version__. The
# command prints the native SemVer version (verlato 0.2.0-beta.1).
"$bin/verlato" --version | grep -E '^verlato [0-9]+\.[0-9]+\.[0-9]+'
"$bin/python" -c 'import sys, verlato; assert not sys.argv[1] or verlato.__version__ == sys.argv[1], (verlato.__version__, sys.argv[1])' "${VERSION:-}"
env -u VERLATO_INTENTUMDIFF_WASM_DIR -u INTENTUMDIFF_WASM_DIR "$bin/verlato" doctor engine \
  | "$bin/python" -c 'import json,sys; info=json.load(sys.stdin); assert info["available"], info; print("engine", info["version"])'
"$bin/python" -c 'import verlato; assert verlato.parser_bundle(); assert verlato.call("health")["runtime"] == "rust"; print("verlato", verlato.__version__)'
