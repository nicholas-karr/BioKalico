#!/usr/bin/env bash
# Sets up lightdm autologin, installs the image-display systemd service,
# and wires them together so the display server starts on boot.
set -euo pipefail

SLADIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SLADIR/../.." && pwd)
VENV="$REPO_DIR/.venv"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-}}"
SERVICE=bioslicer-image-display.service

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
fi

# lightdm logs this user in automatically on every boot, so never root.
if [[ -z "${SERVICE_USER}" || "${SERVICE_USER}" == "root" ]]; then
    echo "Refusing to set up lightdm autologin/display service for root."
    echo "Set SERVICE_USER (or run via 'sudo' as a real user), e.g.:"
    echo "  sudo SERVICE_USER=pi $0"
    exit 1
fi
SERVICE_USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
CONFIG_PATH="$SERVICE_USER_HOME/printer_data/config/image-display-config.yaml"

# Step 0: Mesa OpenGL (required for pyglet GL context on Pi)
apt-get update
apt-get install -y --no-install-recommends libgl1-mesa-dri libglx-mesa0 libgles2

# Step 1: lightdm autologin
mkdir -p /etc/lightdm/lightdm.conf.d
cat > /etc/lightdm/lightdm.conf.d/50-bioslicer.conf << EOF
[Seat:*]
autologin-user=$SERVICE_USER
autologin-user-timeout=0
autologin-session=bioslicer
EOF

# Step 2: Minimal X session (keeps Xorg on :0 alive for the service)
mkdir -p /usr/share/xsessions
cat > /usr/share/xsessions/bioslicer.desktop << 'EOF'
[Desktop Entry]
Name=BioSlicer
Comment=Headless X session for bioslicer-image-display
Exec=/bin/sleep infinity
Type=Application
EOF

# Step 3: Config file
if [[ ! -f "$CONFIG_PATH" ]]; then
    mkdir -p "$(dirname "$CONFIG_PATH")"
    cp "$SLADIR/image-display-config.yaml.template" "$CONFIG_PATH"
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_PATH"
    echo "Created $CONFIG_PATH from template"
fi

# Step 4: Install systemd service (installer checks config + python exist)
# Pin the repo venv when there is one; otherwise the service resolves an
# interpreter itself (see image-display-launcher.sh).
INSTALL_ENV=(
    SCRIPTS_DIR="$SLADIR"
    CONFIG_PATH="$CONFIG_PATH"
    SERVICE_USER="$SERVICE_USER"
)
if [[ -x "$VENV/bin/python3" ]]; then
    INSTALL_ENV+=(PYTHON_BIN="$VENV/bin/python3")
fi
env "${INSTALL_ENV[@]}" bash "$SLADIR/install-image-display-service.sh"

# Step 5: Enable the service (step 4 normally has already)
systemctl daemon-reload
systemctl enable "$SERVICE"

# Step 6: Enable and start lightdm
systemctl enable lightdm
systemctl start lightdm

echo
echo "Done. lightdm will auto-login $SERVICE_USER and start Xorg on :0 on every boot."
echo "The image-display service starts after Xorg is ready."
echo
echo "Check status:"
echo "  systemctl status lightdm"
echo "  systemctl status $SERVICE"

