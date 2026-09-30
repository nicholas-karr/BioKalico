#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-bioslicer-image-display.service}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-}}"
SCRIPTS_DIR="${SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
DISPLAY_VALUE="${DISPLAY_VALUE:-}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
fi

# The display server can listen on the network, so it must not run as root.
if [[ -z "${SERVICE_USER}" || "${SERVICE_USER}" == "root" ]]; then
    echo "Refusing to install ${SERVICE_NAME} to run as root."
    echo "Set SERVICE_USER to the account that should run it, e.g.:"
    echo "  sudo SERVICE_USER=pi $0"
    exit 1
fi
SERVICE_USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
CONFIG_PATH="${CONFIG_PATH:-${SERVICE_USER_HOME}/printer_data/config/image-display-config.yaml}"
# Optional. When unset, image-display-launcher.sh picks an interpreter each
# time the service starts (see its resolve_python function).
PYTHON_BIN="${PYTHON_BIN:-}"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is not available on this host. Install/start image-display.py manually."
    exit 1
fi

if [[ ! -f "${SCRIPTS_DIR}/image-display.py" ]]; then
    echo "image-display.py not found in ${SCRIPTS_DIR}"
    exit 1
fi

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo "Config not found: ${CONFIG_PATH}"
    echo "Create one from template first:"
    echo "  mkdir -p $(dirname "${CONFIG_PATH}")"
    echo "  cp ${SCRIPTS_DIR}/image-display-config.yaml.template ${CONFIG_PATH}"
    exit 1
fi

if [[ -n "${PYTHON_BIN}" && ! -x "${PYTHON_BIN}" ]]; then
    echo "Python executable not found: ${PYTHON_BIN}"
    exit 1
fi

CACHE_DIR="${SERVICE_USER_HOME}/printer_data/cache/bioslicer-sla-video-cache"
mkdir -p "${CACHE_DIR}" || true
chown "${SERVICE_USER}:${SERVICE_USER}" "${CACHE_DIR}" || true

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
cat >"${UNIT_PATH}" <<EOF
[Unit]
Description=BioSlicer image display server
After=graphical.target
# Keep retrying if the graphical session or display driver is temporarily
# unavailable. Start-limit directives belong in [Unit], not [Service].
StartLimitIntervalSec=0

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${SCRIPTS_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=CONFIG_PATH=${CONFIG_PATH}
EOF
if [[ -n "${DISPLAY_VALUE}" ]]; then
    echo "Environment=DISPLAY_VALUE=${DISPLAY_VALUE}" >>"${UNIT_PATH}"
fi
if [[ -n "${PYTHON_BIN}" ]]; then
    echo "Environment=PYTHON_BIN=${PYTHON_BIN}" >>"${UNIT_PATH}"
fi
cat >>"${UNIT_PATH}" <<EOF
ExecStart=/usr/bin/env bash "${SCRIPTS_DIR}/image-display-launcher.sh"
Restart=always
RestartSec=2

[Install]
WantedBy=graphical.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}" || true

echo
echo "Installed and started ${SERVICE_NAME}"
echo "If your display server is not auto-detected, rerun with DISPLAY_VALUE set, for example:"
echo "  sudo DISPLAY_VALUE=:1 $0"
