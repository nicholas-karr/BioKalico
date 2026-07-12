#!/bin/bash
# Deploy a biokalico_extras/moonraker/*.py component into Moonraker's
# components directory so it's picked up as an "unofficial component".
# Moonraker auto-updates itself via git, so these are kept as untracked
# drop-ins under moonraker/components/ rather than patches to Moonraker's
# own tracked source (see biokalico_extras/moonraker/home_root.py for why).
#
# home_root.py and firmware_build.py both just need a plain copy into
# place, so this lives here once instead of twice.
#
# Idempotent: safe to re-run (e.g. on every deploy-mainsail-kalico.sh run).
# Moonraker only picks up new components at startup, so restart the
# moonraker service after this runs.
#
# Usage: bash ~/klipper/scripts/patch-moonraker-component.sh <path-to-py-file>
#   e.g. bash ~/klipper/scripts/patch-moonraker-component.sh \
#          ~/klipper/biokalico_extras/moonraker/firmware_build.py

set -euo pipefail

SOURCE_PY="${1:?Usage: patch-moonraker-component.sh <path-to-py-file>}"
PY_NAME="$(basename "$SOURCE_PY")"
MOONRAKER_DIR="${MOONRAKER_DIR:-$HOME/moonraker}"
COMPONENTS_DIR="$MOONRAKER_DIR/moonraker/components"

if [[ ! -d "$COMPONENTS_DIR" ]]; then
    echo "Moonraker components directory not found: $COMPONENTS_DIR" >&2
    exit 1
fi

cp "$SOURCE_PY" "$COMPONENTS_DIR/$PY_NAME"
echo "Deployed $SOURCE_PY -> $COMPONENTS_DIR/$PY_NAME"
