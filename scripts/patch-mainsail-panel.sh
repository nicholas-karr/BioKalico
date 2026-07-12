#!/bin/bash
# Deploy a biokalico_extras/mainsail/*.js panel into Mainsail and re-inject its
# <script> tag into index.html. Moonraker's [update_manager mainsail]
# persistent_files list preserves the .js file itself across Mainsail
# updates, but not index.html, so the script tag must be re-added every
# time Mainsail updates.
#
# projector-panel.js and firmware-panel.js both need this same copy +
# idempotent <script>-tag-injection logic, so it lives here once instead
# of twice.
#
# home-root-throttle.js is special-cased: it works by replacing
# window.WebSocket, and needs to be in place before Mainsail's own bundle
# opens its first one - so unlike the other panels (which are fine loading
# late, right before </body>, with defer), it's injected as a plain,
# non-deferred <script> right after <head> opens, so it runs before
# Mainsail's bundle has a chance to.
#
# Usage: bash ~/klipper/scripts/patch-mainsail-panel.sh <path-to-js-file>
#   e.g. bash ~/klipper/scripts/patch-mainsail-panel.sh \
#          ~/klipper/biokalico_extras/mainsail/firmware-panel.js

set -euo pipefail

SOURCE_JS="${1:?Usage: patch-mainsail-panel.sh <path-to-js-file>}"
JS_NAME="$(basename "$SOURCE_JS")"
MAINSAIL_DIR="${MAINSAIL_DIR:-$HOME/mainsail}"
INDEX="$MAINSAIL_DIR/index.html"

cp "$SOURCE_JS" "$MAINSAIL_DIR/$JS_NAME"
echo "Deployed $SOURCE_JS -> $MAINSAIL_DIR/$JS_NAME"

if grep -q "$JS_NAME" "$INDEX"; then
    echo "$JS_NAME already referenced in index.html, nothing to do."
    exit 0
fi

# Back up index.html before modifying it in place, so there's a manual
# rollback path. A previous run's .bak may still be sitting there (each
# invocation of this script only patches one JS file, so it's normal for
# several runs in a row to each want to back up the same index.html) -
# don't clobber it, fall back to a timestamped name instead.
BACKUP="$INDEX.bak"
if [ -e "$BACKUP" ]; then
    BACKUP="$INDEX.bak.$(date +%Y%m%d%H%M%S)"
fi
cp "$INDEX" "$BACKUP"
echo "Backed up $INDEX -> $BACKUP"

if [ "$JS_NAME" = "home-root-throttle.js" ]; then
    SCRIPT_TAG="<script src=\"/$JS_NAME\"></script>"
    sed -i "s|<head>|<head>\n        $SCRIPT_TAG|" "$INDEX"
else
    SCRIPT_TAG="<script src=\"/$JS_NAME\" defer></script>"
    sed -i "s|</body>|        $SCRIPT_TAG\n    </body>|" "$INDEX"
fi

if ! grep -q "$JS_NAME" "$INDEX"; then
    echo "ERROR: sed did not find its injection point in $INDEX - $JS_NAME script tag was NOT added (Mainsail's markup may have changed). $INDEX is unmodified; a backup was still written to $BACKUP." >&2
    exit 1
fi

echo "Patched $INDEX: $JS_NAME script tag added."
