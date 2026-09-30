#!/usr/bin/env bash
set -euo pipefail

SCRIPTS_DIR="${SCRIPTS_DIR:-$(cd "$(dirname "$0")" && pwd)}"
REPO_DIR="${REPO_DIR:-$(cd "${SCRIPTS_DIR}/../.." && pwd)}"
CONFIG_PATH="${CONFIG_PATH:-$HOME/printer_data/config/image-display-config.yaml}"
PYTHON_BIN="${PYTHON_BIN:-}"
DISPLAY_VALUE="${DISPLAY_VALUE:-}"
XAUTHORITY_PATH="${XAUTHORITY_PATH:-}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"

detect_display_value() {
    local line value
    line="$(pgrep -u "$(id -u)" -af Xwayland | head -n1 || true)"
    if [[ -n "$line" ]]; then
        value="$(printf '%s\n' "$line" | grep -oE ':[0-9]+' | head -n1 || true)"
        if [[ -n "$value" ]]; then
            printf '%s\n' "$value"
            return 0
        fi
    fi

    for socket in /tmp/.X11-unix/X*; do
        [[ -e "$socket" ]] || continue
        printf ':%s\n' "${socket##*/X}"
        return 0
    done

    return 1
}

detect_xauthority_path() {
    local runtime_dir file

    # Xorg: standard cookie file
    if [[ -f "$HOME/.Xauthority" ]]; then
        printf '%s\n' "$HOME/.Xauthority"
        return 0
    fi

    # Wayland/mutter Xwayland: session-scoped cookie
    runtime_dir="/run/user/$(id -u)"
    file="$(ls -t "$runtime_dir"/.mutter-Xwaylandauth.* 2>/dev/null | head -n1 || true)"
    if [[ -n "$file" ]]; then
        printf '%s\n' "$file"
        return 0
    fi

    return 1
}

resolve_display() {
    if [[ -n "$DISPLAY_VALUE" ]]; then
        return 0
    fi

    DISPLAY_VALUE="$(detect_display_value || true)"
    [[ -n "$DISPLAY_VALUE" ]]
}

resolve_xauthority() {
    if [[ -n "$XAUTHORITY_PATH" && "$XAUTHORITY_PATH" != "/dev/null" ]]; then
        return 0
    fi

    XAUTHORITY_PATH="$(detect_xauthority_path || true)"
    [[ -n "$XAUTHORITY_PATH" ]]
}

# The first interpreter that exists wins: a venv in the repo root, one beside
# this script (see "Running the server by hand" in README.md), then the
# Klipper venv that biokalico-installer.sh keeps up to date with pyglet,
# PyYAML and Pillow.
resolve_python() {
    local candidate
    if [[ -n "$PYTHON_BIN" ]]; then
        return 0
    fi
    for candidate in \
        "${REPO_DIR}/.venv/bin/python" \
        "${SCRIPTS_DIR}/.venv/bin/python" \
        "${HOME}/klippy-env/bin/python"; do
        if [[ -x "$candidate" ]]; then
            PYTHON_BIN="$candidate"
            return 0
        fi
    done
    return 1
}

if ! resolve_python; then
    echo "No Python interpreter with the display server's packages found." >&2
    echo "Looked for ${REPO_DIR}/.venv, ${SCRIPTS_DIR}/.venv and ${HOME}/klippy-env;" >&2
    echo "create one as described in scripts/sla/README.md or set PYTHON_BIN." >&2
    exit 1
fi

deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
    if resolve_display && resolve_xauthority; then
        break
    fi
    sleep 1
done

if [[ -z "$DISPLAY_VALUE" || -z "$XAUTHORITY_PATH" ]]; then
    echo "Unable to determine active X display/auth path" >&2
    echo "DISPLAY_VALUE=${DISPLAY_VALUE:-<unset>}" >&2
    echo "XAUTHORITY_PATH=${XAUTHORITY_PATH:-<unset>}" >&2
    exit 1
fi

export DISPLAY="$DISPLAY_VALUE"
export XAUTHORITY="$XAUTHORITY_PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

exec "$PYTHON_BIN" "$SCRIPTS_DIR/image-display.py" "$CONFIG_PATH"