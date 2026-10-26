#!/usr/bin/env bash
# install-script.sh — adds/updates one of the web/*.js scripts in Jellyfin's "JavaScript Injector" plugin.
#
# Idempotent: replaces the entry with the same name if it exists, otherwise appends it; the plugin's
# other scripts stay as they are. The plugin's configuration endpoint does NOT merge: the whole object
# is read, changed and sent back. No Jellyfin restart needed — reload the page in the browser.
#
#   JELLYFIN_URL=http://localhost:8096 JELLYFIN_API_KEY=… bash web/install-script.sh web/search.js "On demand: search"
#   … bash web/install-script.sh --remove "On demand: search"
#
# Needs: curl, python3, and node (only to syntax-check the script before publishing it).
set -euo pipefail

: "${JELLYFIN_URL:?set JELLYFIN_URL (e.g. http://localhost:8096)}"
: "${JELLYFIN_API_KEY:?set JELLYFIN_API_KEY (Dashboard → API Keys)}"
PLUGIN=f5a34f7b2e8a4e6aa7223a216a81b374          # JavaScript Injector's plugin id

if [ "${1:-}" = "--remove" ]; then
  ACTION=remove; NAME="${2:?usage: install-script.sh --remove \"<name>\"}"; SOURCE=""
else
  ACTION=install; SOURCE="${1:?usage: install-script.sh <script.js> \"<name>\"}"; NAME="${2:?missing the entry name}"
  [ -f "$SOURCE" ] || { echo "no such file: $SOURCE" >&2; exit 1; }
  if command -v node >/dev/null; then
    node --check "$SOURCE"                       # never publish a broken script
  else
    echo "(node not found: skipping the syntax check)" >&2
  fi
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
auth="Authorization: MediaBrowser Token=$JELLYFIN_API_KEY"

curl -sf "$JELLYFIN_URL/Plugins/$PLUGIN/Configuration" -H "$auth" > "$tmp/current.json" \
  || { echo "could not read the JavaScript Injector configuration (is the plugin installed? is the key right?)" >&2; exit 1; }

python3 -I - "$tmp/current.json" "$SOURCE" "$NAME" "$ACTION" > "$tmp/new.json" <<'PY'
import json, sys
current, source, name, action = sys.argv[1:5]
cfg = json.load(open(current, encoding="utf-8"))
scripts = [s for s in cfg.get("CustomJavaScripts", []) if s.get("Name") != name]
if action == "install":
    scripts.append({"Name": name, "Script": open(source, encoding="utf-8").read(),
                    "Enabled": True, "RequiresAuthentication": True})
cfg["CustomJavaScripts"] = scripts
print(json.dumps(cfg))
PY

curl -sf -X POST "$JELLYFIN_URL/Plugins/$PLUGIN/Configuration" -H "$auth" \
     -H "Content-Type: application/json" --data @"$tmp/new.json"

# check what the browser will receive (no pipe straight into grep -q, because of pipefail)
served=$(curl -sf "$JELLYFIN_URL/JavaScriptInjector/private.js" -H "$auth" || true)
n=$(grep -c "window.__" <<<"$served" || true)
echo "$ACTION \"$NAME\": ok ($n script marker(s) in private.js). Reload the Jellyfin page in the browser."
