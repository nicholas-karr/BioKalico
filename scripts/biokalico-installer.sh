#!/bin/bash
# Installs and updates the whole BioKalico printer-software stack on a host:
# Klipper (this repo), Moonraker, Mainsail, mainsail-config, and crowsnest.
# Replaces KIAUH. First-time install and every subsequent update go through
# this one script, invoked directly or via Moonraker's [update_manager
# klipper] install_script hook (a single "Update" button in Mainsail).
#
# Moonraker and Mainsail (BioKalico forks) and crowsnest (upstream) are git
# submodules under deps/. Mainsail is built from source here, which needs
# Node.js.
#
# Every sync function is safe to re-run, so `install` and `update` share the
# same code. Moonraker and crowsnest are set up by their own installers.
#
# Usage:
#   biokalico-installer.sh install              # first-time setup, all components
#   biokalico-installer.sh update [names...]    # sync components (default: all)
#   biokalico-installer.sh status               # show each component's current ref
#
# Bootstrap on a brand-new host (stock Debian, Raspbian, or MainsailOS):
#   git clone --recurse-submodules https://github.com/nicholas-karr/BioKalico.git ~/klipper
#   bash ~/klipper/scripts/biokalico-installer.sh install
#
# (--recurse-submodules is optional; `install` and `update` fetch the
# submodules themselves.)
#
# Called with no arguments (Moonraker's install_script convention), this
# defaults to `update` with no component filter, i.e. sync everything.

set -euo pipefail

BIOKALICO_ORIGIN="${BIOKALICO_ORIGIN:-https://github.com/nicholas-karr/BioKalico.git}"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
KLIPPY_ENV="${KLIPPY_ENV:-$HOME/klippy-env}"
PRINTER_DATA="${PRINTER_DATA:-$HOME/printer_data}"

MOONRAKER_DIR="${MOONRAKER_DIR:-$KLIPPER_DIR/deps/moonraker}"
MAINSAIL_DIR="${MAINSAIL_DIR:-$KLIPPER_DIR/deps/mainsail}"
CROWSNEST_DIR="${CROWSNEST_DIR:-$KLIPPER_DIR/deps/crowsnest}"

MAINSAIL_CONFIG_DIR="${MAINSAIL_CONFIG_DIR:-$HOME/mainsail-config}"
MAINSAIL_CONFIG_ORIGIN="${MAINSAIL_CONFIG_ORIGIN:-https://github.com/mainsail-crew/mainsail-config.git}"

ALL_COMPONENTS=(klipper moonraker mainsail mainsail-config crowsnest)

log() { echo "==> $*"; }
warn() { echo "==> WARNING: $*" >&2; }

require_not_root() {
    if [[ "$EUID" -eq 0 ]]; then
        echo "Run as a normal user (sudo is used internally where needed)." >&2
        exit 1
    fi
}

restart_service() {
    local svc="$1"
    # Check the unit file directly on disk rather than asking systemd
    # (`systemctl list-unit-files`/`is-enabled` etc.) whether the unit
    # exists. Two independent problems with asking systemd instead, both
    # found by running this against a real fresh install rather than just
    # reading the code: (1) as a regular user, `systemctl list-unit-files`
    # can silently come back empty in some environments even with a
    # working sudo (confirmed directly - `sudo systemctl list-unit-files`
    # sees everything, the same command without sudo sees nothing), and
    # (2) even with sudo, back-to-back `sudo systemctl ...` calls for
    # several different services in the same script run can - non-
    # deterministically, maybe fifteen unit-file-affecting apt package
    # installs and 5 different components' worth of systemctl calls
    # happening within one process is just enough contention to race -
    # miss a unit file some other component's installer wrote moments
    # earlier, even right after an explicit `daemon-reload`. Checking the
    # file directly sidesteps both: it's what every installer this script
    # drives (this one's own install_klipper_service, install-moonraker.sh,
    # crowsnest's install.sh) actually writes to, and reading it back needs
    # no systemd/D-Bus round-trip that could be mid-processing something
    # else.
    if [[ -f "/etc/systemd/system/${svc}.service" ]]; then
        log "restarting ${svc}.service"
        sudo systemctl daemon-reload
        sudo systemctl restart "$svc"
    fi
}

git_clone_or_pull() {
    # git_clone_or_pull <path> <origin> [branch]
    #
    # Self-heals the same way sync_klipper() does for this repo itself: a
    # dropped connection mid-clone can leave a `.git` dir that exists but is
    # corrupted/incomplete (wrong or missing origin, failing fetch/merge),
    # which would otherwise require a manual `rm -rf` before the next run of
    # this script could succeed. Back up and re-clone instead of failing
    # outright.
    local path="$1" origin="$2" branch="${3:-}"
    if [[ -d "$path/.git" ]]; then
        local current_origin
        current_origin="$(git -C "$path" remote get-url origin 2>/dev/null || true)"
        if [[ "$current_origin" != "$origin" ]]; then
            warn "$(basename "$path"): $path origin is '${current_origin:-none}', not '$origin', backing up and re-cloning"
            mv "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
            if [[ -n "$branch" ]]; then
                git clone --quiet --branch "$branch" "$origin" "$path"
            else
                git clone --quiet "$origin" "$path"
            fi
        else
            log "$(basename "$path"): pulling latest"
            git -C "$path" fetch --quiet origin
            if [[ -n "$branch" ]]; then
                git -C "$path" checkout --quiet "$branch"
            fi
            git -C "$path" merge --ff-only --quiet "@{u}"
        fi
    elif [[ -e "$path" ]]; then
        warn "$(basename "$path"): $path exists but isn't a git checkout, backing up and cloning"
        mv "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
        if [[ -n "$branch" ]]; then
            git clone --quiet --branch "$branch" "$origin" "$path"
        else
            git clone --quiet "$origin" "$path"
        fi
    else
        log "$(basename "$path"): cloning $origin -> $path"
        if [[ -n "$branch" ]]; then
            git clone --quiet --branch "$branch" "$origin" "$path"
        else
            git clone --quiet "$origin" "$path"
        fi
    fi
}

### klipper (self) ###########################################################

sync_klipper() {
    log "klipper: syncing $KLIPPER_DIR"
    if [[ -d "$KLIPPER_DIR/.git" ]]; then
        local origin
        origin="$(git -C "$KLIPPER_DIR" remote get-url origin 2>/dev/null || true)"
        if [[ "$origin" != "$BIOKALICO_ORIGIN" ]]; then
            warn "klipper: $KLIPPER_DIR origin is '${origin:-none}', not BioKalico, backing up and re-cloning"
            mv "$KLIPPER_DIR" "${KLIPPER_DIR}.bak.$(date +%Y%m%d%H%M%S)"
            git clone "$BIOKALICO_ORIGIN" "$KLIPPER_DIR"
        else
            git -C "$KLIPPER_DIR" pull --ff-only
        fi
    elif [[ -e "$KLIPPER_DIR" ]]; then
        warn "klipper: $KLIPPER_DIR exists but isn't a git checkout, backing up and cloning"
        mv "$KLIPPER_DIR" "${KLIPPER_DIR}.bak.$(date +%Y%m%d%H%M%S)"
        git clone "$BIOKALICO_ORIGIN" "$KLIPPER_DIR"
    else
        git clone "$BIOKALICO_ORIGIN" "$KLIPPER_DIR"
    fi

    install_klipper_packages
    install_klipper_venv
    install_klipper_service
    seed_printer_data_config

    restart_service klipper
}

install_klipper_packages() {
    command -v apt-get >/dev/null 2>&1 || { warn "klipper: apt-get not found, skipping package install"; return; }
    log "klipper: installing system packages"
    sudo apt-get update -qq
    # Same list as scripts/install-debian.sh, plus ccache and dfu-util for
    # firmware builds, python3-serial because flash_can.py runs under the
    # system python3 rather than klippy-env (see
    # biokalico_extras/firmware_flash.md), and curl/ca-certificates, which
    # later steps need for HTTPS downloads. Node.js is installed by
    # ensure_node() because the Debian package is too old.
    sudo apt-get install --yes \
        virtualenv python3-dev libffi-dev build-essential libncurses-dev libusb-dev \
        avrdude gcc-avr binutils-avr avr-libc \
        stm32flash libnewlib-arm-none-eabi gcc-arm-none-eabi binutils-arm-none-eabi \
        libusb-1.0 pkg-config \
        ccache dfu-util python3-serial \
        curl ca-certificates
}

install_klipper_venv() {
    if [[ ! -x "$KLIPPY_ENV/bin/python" ]]; then
        log "klipper: creating $KLIPPY_ENV"
        virtualenv -p python3 "$KLIPPY_ENV"
    fi
    "$KLIPPY_ENV/bin/pip" install -q -r "$KLIPPER_DIR/scripts/klippy-requirements.txt"
}

install_klipper_service() {
    local unit=/etc/systemd/system/klipper.service
    local exec_start="$KLIPPY_ENV/bin/python $KLIPPER_DIR/klippy/klippy.py $PRINTER_DATA/config/printer.cfg -l $PRINTER_DATA/logs/klippy.log -a $PRINTER_DATA/comms/klippy.sock"

    # Refuse to bake a throwaway path into a permanent unit. If a caller ran
    # us with PRINTER_DATA pointed at a temp dir (e.g. PRINTER_DATA=$(mktemp
    # -d) for a test), writing that path here would leave klipper.service
    # ExecStart'ing a directory that vanishes on the next tmp cleanup/reboot;
    # klipper then dies on FileNotFoundError and loops in 'activating'
    # forever. Bail loudly rather than persist a self-destructing unit.
    case "$PRINTER_DATA" in
        /tmp/* | /var/tmp/*)
            warn "klipper: refusing to write klipper.service with PRINTER_DATA under a temp dir ($PRINTER_DATA)"
            return 1
            ;;
    esac

    # Self-heal instead of blindly skipping when a unit already exists (see
    # the comment in restart_service() above for why this checks the unit
    # file directly rather than asking systemd). If the installed ExecStart
    # already matches what we'd write, there's nothing to do. Otherwise the
    # unit is missing, or stale -- pointing at an old/moved/temp printer_data
    # -- so rewrite it. -F: match the ExecStart as a fixed string, not a
    # regex.
    if [[ -f "$unit" ]] && grep -qF "ExecStart=$exec_start" "$unit"; then
        return
    fi
    if [[ -f "$unit" ]]; then
        log "klipper: existing klipper.service ExecStart is stale, rewriting"
    else
        log "klipper: installing klipper.service"
    fi
    sudo /bin/sh -c "cat > $unit" <<EOF
#Systemd service file for klipper
[Unit]
Description=Starts klipper on startup
After=network.target

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=$(id -un)
RemainAfterExit=yes
ExecStart=$exec_start
Restart=always
RestartSec=10
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable klipper.service
}

seed_printer_data_config() {
    mkdir -p "$PRINTER_DATA"/config "$PRINTER_DATA"/logs "$PRINTER_DATA"/comms "$PRINTER_DATA"/systemd

    # Moonraker's own installer (called from sync_moonraker below) writes a
    # generic default moonraker.conf if none exists yet - but its default
    # points klippy_uds_address at /tmp/klippy_uds, not the
    # $PRINTER_DATA/comms/klippy.sock path klipper.service actually uses
    # above, and it has none of the BioKalico [include ...] lines. Seeding
    # our own template here first (sync_klipper always runs before
    # sync_moonraker - see ALL_COMPONENTS) means Moonraker's installer sees
    # a config file already in place and skips writing its generic one, so
    # a from-scratch install ends up with a moonraker.conf that actually
    # matches this repo instead of a mismatched default. Never overwrites an
    # existing file - this only fires on a genuinely fresh printer_data.
    local moonraker_conf="$PRINTER_DATA/config/moonraker.conf"
    if [[ ! -e "$moonraker_conf" ]]; then
        log "printer_data: seeding $moonraker_conf from bio_config/moonraker.conf.example"
        cp "$KLIPPER_DIR/bio_config/moonraker.conf.example" "$moonraker_conf"
    fi

    # printer.cfg is deliberately NOT seeded here - unlike moonraker.conf,
    # there's no generic default that's actually correct (MCU serial paths,
    # kinematics, and thermistor types are specific to each printer). Until
    # one is written (see QUICKSTART.md section 9), klipper.service will
    # restart-loop every 10s reporting a missing config - harmless, but
    # expected, and does not block moonraker from coming up.
}

### submodules (deps/mainsail, deps/moonraker, deps/crowsnest) ###############

sync_submodules() {
    # git needs ca-certificates for https, and this can run before
    # install_klipper_packages().
    if command -v apt-get >/dev/null 2>&1 && [[ ! -e /etc/ssl/certs/ca-certificates.crt ]]; then
        log "submodules: installing ca-certificates (needed before any https git fetch)"
        sudo apt-get update -qq
        sudo apt-get install --yes ca-certificates
    fi
    log "submodules: syncing deps/{mainsail,moonraker,crowsnest}"
    git -C "$KLIPPER_DIR" submodule sync --recursive
    git -C "$KLIPPER_DIR" submodule update --init --recursive
}

### moonraker #################################################################

sync_moonraker() {
    log "moonraker: running its own installer"
    # -z: skip the installer's own daemon-reload/enable; done below instead.
    # -f: rewrite moonraker.service even if it exists, so a unit that points
    # at an old ~/moonraker checkout is replaced with one for deps/moonraker.
    bash "$MOONRAKER_DIR/scripts/install-moonraker.sh" -s -z -f
    sudo systemctl daemon-reload
    sudo systemctl enable moonraker
    restart_service moonraker
}

### mainsail (built from source) #############################################

ensure_node() {
    # Mainsail's build needs Node 20.19+ or 22.12+. Debian's nodejs package
    # is older (v18 on bookworm), so install from NodeSource when needed.
    local required_major=20
    local node_ok=0
    if command -v node >/dev/null 2>&1; then
        # Matches package.json's engines field (^20.19.0 || >=22.12.0).
        if node -e '
            const [major, minor] = process.versions.node.split(".").map(Number)
            process.exit(
                (major === 20 && minor >= 19) ||
                (major === 22 && minor >= 12) ||
                major > 22 ? 0 : 1
            )
        ' 2>/dev/null; then
            node_ok=1
        else
            warn "mainsail: system node ($(node -v)) does not satisfy ^20.19.0 or >=22.12.0"
        fi
    fi
    if [[ "$node_ok" -ne 1 ]]; then
        command -v apt-get >/dev/null 2>&1 || { warn "mainsail: compatible node/npm missing and no apt-get, cannot build"; return 1; }
        log "mainsail: installing Node.js ${required_major}.x from NodeSource"
        curl -fsSL "https://deb.nodesource.com/setup_${required_major}.x" | sudo -E bash -
        sudo apt-get install --yes nodejs
    fi

    # Mainsail's "build" script also runs `zip` to make mainsail.zip.
    if ! command -v zip >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || {
            warn "mainsail: zip missing and no apt-get, cannot build"
            return 1
        }
        sudo apt-get install --yes zip
    fi
}

sync_mainsail() {
    ensure_node

    # `npm run build` recreates dist/, so keep the instance's config.json.
    local cfg_backup=""
    if [[ -f "$MAINSAIL_DIR/dist/config.json" ]]; then
        cfg_backup="$(mktemp)"
        cp "$MAINSAIL_DIR/dist/config.json" "$cfg_backup"
    fi

    log "mainsail: building from source ($MAINSAIL_DIR)"
    (cd "$MAINSAIL_DIR" && npm ci && npm run build)

    if [[ ! -f "$MAINSAIL_DIR/dist/index.html" ]]; then
        [[ -n "$cfg_backup" ]] && rm -f "$cfg_backup"
        warn "mainsail: build did not produce dist/index.html, leaving nginx pointed at whatever was already there"
        return 1
    fi

    if [[ -n "$cfg_backup" ]]; then
        cp "$cfg_backup" "$MAINSAIL_DIR/dist/config.json"
        rm -f "$cfg_backup"
    fi

    install_mainsail_nginx
}

install_mainsail_nginx() {
    if ! command -v nginx >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || { warn "mainsail: nginx missing and no apt-get, skipping web server setup"; return; }
        log "mainsail: installing nginx"
        sudo apt-get install --yes nginx
    fi

    local fresh_install=1
    [[ -f /etc/nginx/sites-enabled/mainsail ]] && fresh_install=0

    if [[ "$fresh_install" -eq 1 ]]; then
        log "mainsail: writing nginx site config (this is what KIAUH used to generate for you)"
    else
        log "mainsail: refreshing nginx site config from the current template"
    fi

    # Render each file to a temp path first, then only sudo-copy it into
    # place (and only run nginx -t / reload below) if the rendered content
    # actually differs from what's already on disk. Comparing content
    # instead of early-returning once the site file exists means `update`
    # re-applies the current template every time - so a template change in
    # this repo (e.g. a new location block) always reaches an
    # already-installed host - without an unnecessary nginx reload on every
    # run when nothing actually changed.
    local changed=0
    local tmp

    # Shared by every nginx-fronted moonraker instance regardless of which
    # printer/host this is - not templated per-host, so written
    # unconditionally alongside the site file below.
    tmp="$(mktemp)"
    cat >"$tmp" <<'EOF'
# /etc/nginx/conf.d/common_vars.conf

map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
EOF
    if ! sudo cmp -s "$tmp" /etc/nginx/conf.d/common_vars.conf 2>/dev/null; then
        sudo cp "$tmp" /etc/nginx/conf.d/common_vars.conf
        changed=1
    fi
    rm -f "$tmp"

    tmp="$(mktemp)"
    cat >"$tmp" <<'EOF'
# /etc/nginx/conf.d/upstreams.conf
upstream apiserver {
    ip_hash;
    server 127.0.0.1:7125;
}

upstream mjpgstreamer1 {
    ip_hash;
    server 127.0.0.1:8080;
}

upstream mjpgstreamer2 {
    ip_hash;
    server 127.0.0.1:8081;
}

upstream mjpgstreamer3 {
    ip_hash;
    server 127.0.0.1:8082;
}

upstream mjpgstreamer4 {
    ip_hash;
    server 127.0.0.1:8083;
}
EOF
    if ! sudo cmp -s "$tmp" /etc/nginx/conf.d/upstreams.conf 2>/dev/null; then
        sudo cp "$tmp" /etc/nginx/conf.d/upstreams.conf
        changed=1
    fi
    rm -f "$tmp"

    tmp="$(mktemp)"
    cat >"$tmp" <<EOF
server {
    listen 80;
    # uncomment the next line to activate IPv6
    # listen [::]:80;

    access_log /var/log/nginx/mainsail-access.log;
    error_log /var/log/nginx/mainsail-error.log;

    # disable this section on smaller hardware like a pi zero
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_proxied expired no-cache no-store private auth;
    gzip_comp_level 4;
    gzip_buffers 16 8k;
    gzip_http_version 1.1;
    gzip_types text/plain text/css text/xml text/javascript application/javascript application/x-javascript application/json application/xml;

    # web_path from mainsail static files
    root $MAINSAIL_DIR/dist;

    index index.html;
    server_name _;

    # disable max upload size checks
    client_max_body_size 0;

    # disable proxy request buffering
    proxy_request_buffering off;

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location = /index.html {
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    # Root-level .js files (mainly the service worker, sw.js) have no
    # content hash in their names, unlike /assets/*.js. Without no-store, a
    # caching proxy such as Cloudflare can keep serving an old copy after
    # an update.
    location ~ ^/[^/]+\.js\$ {
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    location /websocket {
        proxy_pass http://apiserver/websocket;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }

    location ~ ^/(printer|api|access|machine|server)/ {
        proxy_pass http://apiserver\$request_uri;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme \$scheme;
    }

    location /webcam/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer1/;
    }

    location /webcam2/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer2/;
    }

    location /webcam3/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer3/;
    }

    location /webcam4/ {
        postpone_output 0;
        proxy_buffering off;
        proxy_ignore_headers X-Accel-Buffering;
        access_log off;
        error_log off;
        proxy_pass http://mjpgstreamer4/;
    }
}
EOF
    if ! sudo cmp -s "$tmp" /etc/nginx/sites-available/mainsail 2>/dev/null; then
        sudo cp "$tmp" /etc/nginx/sites-available/mainsail
        changed=1
    fi
    rm -f "$tmp"

    sudo ln -sf /etc/nginx/sites-available/mainsail /etc/nginx/sites-enabled/mainsail
    # Avoid a default-server conflict on port 80 with nginx's stock site.
    sudo rm -f /etc/nginx/sites-enabled/default

    if [[ "$fresh_install" -eq 0 && "$changed" -eq 0 ]]; then
        log "mainsail: nginx config unchanged, skipping reload"
        return
    fi

    sudo nginx -t
    if [[ "$fresh_install" -eq 1 ]]; then
        # `restart` rather than `reload`: nginx's own package postinst is
        # supposed to start it automatically, but that's not guaranteed on
        # every host (some environments block automatic service starts
        # during package installation), and `reload` only works on an
        # already-active unit - `enable --now`/`restart` works regardless
        # of nginx's current state, so this doesn't silently no-op on a
        # host where nginx never actually got started.
        sudo systemctl enable nginx
        sudo systemctl restart nginx
    else
        log "mainsail: nginx config changed, reloading nginx"
        sudo systemctl reload nginx
    fi
}

### mainsail-config ###########################################################

sync_mainsail_config() {
    local before=""
    [[ -d "$MAINSAIL_CONFIG_DIR/.git" ]] && before="$(git -C "$MAINSAIL_CONFIG_DIR" rev-parse HEAD)"
    git_clone_or_pull "$MAINSAIL_CONFIG_DIR" "$MAINSAIL_CONFIG_ORIGIN" "master"
    local after
    after="$(git -C "$MAINSAIL_CONFIG_DIR" rev-parse HEAD)"
    if [[ "$before" != "$after" ]]; then
        restart_service klipper
    fi
}

### crowsnest ##################################################################

sync_crowsnest() {
    log "crowsnest: running its own installer (unattended)"
    # crowsnest's installer resolves its own resource files (e.g.
    # tools/libs/core.sh's `service_file="${PWD}/resources/crowsnest.service"`)
    # relative to the CALLER's current directory, not its own script
    # location - it must be run with $CROWSNEST_DIR as the working
    # directory, or it silently builds a bogus path and fails partway
    # through. Subshell so this doesn't change biokalico-installer.sh's own CWD for
    # whatever runs after it.
    (cd "$CROWSNEST_DIR" && CROWSNEST_UNATTENDED=1 sudo -E ./tools/install.sh)

    # Don't trust the installer blindly. crowsnest's install_service_file
    # (upstream tools/libs/core.sh) removes the existing unit with `rm -f`
    # BEFORE copying its replacement, so a run that aborts mid-replace leaves
    # NO crowsnest.service behind -- only the stale multi-user.target.wants
    # symlink -- and the camera then silently never comes back (a real
    # incident: the unit vanished during an update and nothing reported it
    # until a manual restart). Verify the unit actually landed and fail loudly
    # if it didn't, rather than sailing on to restart_service (which would
    # just warn "not found" and skip) and leaving the operator with no camera
    # and no error.
    if ! service_unit_exists crowsnest; then
        warn "crowsnest: install.sh returned but /etc/systemd/system/crowsnest.service is missing"
        warn "crowsnest: its installer probably aborted mid-replace -- reinstall by hand with:"
        warn "  (cd \"$CROWSNEST_DIR\" && CROWSNEST_UNATTENDED=1 sudo -E ./tools/install.sh)"
        return 1
    fi
    restart_service crowsnest
}

### orchestration ##############################################################

sync_component() {
    case "$1" in
        klipper) sync_klipper ;;
        moonraker) sync_moonraker ;;
        mainsail) sync_mainsail ;;
        mainsail-config) sync_mainsail_config ;;
        crowsnest) sync_crowsnest ;;
        *) echo "Unknown component: $1 (expected one of: ${ALL_COMPONENTS[*]})" >&2; exit 2 ;;
    esac
}

service_unit_exists() {
    local svc="$1"
    [[ -f "/etc/systemd/system/${svc}.service" ]] || [[ -f "/lib/systemd/system/${svc}.service" ]]
}

ensure_services_running() {
    # Final safety net, run once after everything else in this install/update
    # has finished. Every individual component already tries to start its
    # own service via restart_service() as it's synced, but running many
    # components' worth of package installs and systemctl calls back to back
    # in one process can non-deterministically cause an individual
    # restart_service call to land wrong (found by actually running this
    # against a fresh install repeatedly, not just reading the code - see
    # restart_service()'s own comment). What a user actually cares about is
    # the END state - "is my printer stack up" - not which code path got
    # there, so guarantee that here regardless: anything that's actually
    # installed and isn't already running gets one more restart attempt,
    # in a single pass with everything else already settled (much less
    # contention than mid-install).
    local svc failed=()
    for svc in klipper moonraker crowsnest nginx; do
        service_unit_exists "$svc" || continue
        if ! sudo systemctl is-active --quiet "$svc"; then
            log "ensure_services_running: $svc.service isn't up yet, restarting"
            if ! sudo systemctl restart "$svc"; then
                failed+=("$svc")
            fi
        fi
    done

    if [[ ${#failed[@]} -gt 0 ]]; then
        warn "ensure_services_running: failed to restart: ${failed[*]} -- printer stack is not fully up"
        return 1
    fi
}

cmd_install() {
    # Pull this repo first so sync_submodules checks out the new revisions.
    sync_klipper
    sync_submodules
    for c in "${ALL_COMPONENTS[@]}"; do
        [[ "$c" == "klipper" ]] && continue
        sync_component "$c"
    done
    ensure_services_running
}

cmd_update() {
    local components=("$@")
    [[ ${#components[@]} -eq 0 ]] && components=("${ALL_COMPONENTS[@]}")

    local c
    for c in "${components[@]}"; do
        if [[ "$c" == "klipper" ]]; then
            sync_klipper
            break
        fi
    done
    # After sync_klipper, so the newly pulled submodule revisions are used.
    sync_submodules
    for c in "${components[@]}"; do
        [[ "$c" == "klipper" ]] && continue
        sync_component "$c"
    done
    ensure_services_running
}

cmd_status() {
    local c dir
    for c in klipper moonraker mainsail mainsail-config crowsnest; do
        case "$c" in
            klipper) dir="$KLIPPER_DIR" ;;
            moonraker) dir="$MOONRAKER_DIR" ;;
            mainsail) dir="$MAINSAIL_DIR" ;;
            mainsail-config) dir="$MAINSAIL_CONFIG_DIR" ;;
            crowsnest) dir="$CROWSNEST_DIR" ;;
        esac
        # Submodules have a .git file rather than a directory.
        if git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            printf "%-16s %s\n" "$c" "$(git -C "$dir" log -1 --format='%h %cd %s' --date=short)"
        else
            printf "%-16s not installed\n" "$c"
        fi
    done
}

main() {
    require_not_root
    local cmd="${1:-update}"
    [[ $# -gt 0 ]] && shift
    case "$cmd" in
        install) cmd_install ;;
        update) cmd_update "$@" ;;
        status) cmd_status ;;
        -h|--help)
            sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
            ;;
        *)
            echo "Unknown command: $cmd (expected install, update, or status)" >&2
            exit 2
            ;;
    esac
}

main "$@"
