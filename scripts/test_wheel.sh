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
"$bin/verlato" --version | grep -F "${VERSION:-}"
env -u VERLATO_INTENTUMDIFF_WASM_DIR -u INTENTUMDIFF_WASM_DIR "$bin/verlato" doctor engine \
  | "$bin/python" -c 'import json,sys; info=json.load(sys.stdin); assert info["available"], info; print("engine", info["version"])'
"$bin/python" -c 'import verlato; assert verlato.parser_bundle(); assert verlato.call("health")["runtime"] == "rust"; print("verlato", verlato.__version__)'
