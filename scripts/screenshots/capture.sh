#!/usr/bin/env bash
# Regenerate every documentation screenshot in docs/assets/screenshots/ (and the extension's
# Marketplace image) from the real CLI and the extension's real view code.
#
# Usage: scripts/screenshots/capture.sh
# Needs: target/release/verlato (cargo build --release -p verlato), a parser bundle
# (VERLATO_INTENTUMDIFF_WASM_DIR, or target/release/wasm), Node 22+, Playwright with Chromium,
# and `npm ci` in vscode-extension/.
#
# The demo repository lives at /srv/demo/taskboard (override with DEMO_DIR) and the parser
# bundle is linked at /opt/verlato/wasm, so no machine-specific path appears in a screenshot.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
demo_parent="${DEMO_DIR:-/srv/demo}"
bundle="${VERLATO_INTENTUMDIFF_WASM_DIR:-$root/target/release/wasm}"
test -f "$bundle/parser_manifest.json" || { echo "parser bundle not found at $bundle" >&2; exit 1; }
mkdir -p /opt/verlato
ln -sfn "$bundle" /opt/verlato/wasm
export VERLATO_INTENTUMDIFF_WASM_DIR=/opt/verlato/wasm
export PATH="$root/target/release:$PATH"

(cd "$root/vscode-extension" && npm run build >/dev/null)
repo="$("$root/scripts/screenshots/make_demo_repo.sh" "$demo_parent")"

# Reviewer decisions, so the views show accepted, edited and hidden items.
cd "$repo"
item() { verlato draft json --base v1.0.0 | python3 -c "import json,sys; print([i['id'] for s in json.load(sys.stdin)['sections'] for i in s['items'] if i['title'].startswith(sys.argv[1])][0])" "$1"; }
verlato draft review --base v1.0.0 --id "$(item 'Merge pull request #12')" --status accepted --reviewer demo-dev \
  --summary "Export a board as CSV with export_csv(board), including a header row and each task's tags." >/dev/null
verlato draft review --base v1.0.0 --id "$(item 'docs:')" --status accepted --reviewer demo-dev \
  --summary "Documented CSV export in the README." >/dev/null
verlato draft review --base v1.0.0 --id "$(item 'style:')" --status hidden --reviewer demo-dev >/dev/null

out="$root/docs/assets/screenshots"
node "$root/scripts/screenshots/vscode_views.js" --repo "$repo" --out "$out"
node "$root/scripts/screenshots/cli.js" --repo "$repo" --out "$out"
cp "$out/vscode-semantic-review.png" "$root/vscode-extension/media/semantic-review.png"
echo "screenshots updated in $out"
