#!/bin/bash
# Installs and updates the whole BioKalico printer-software stack on a host:
# Klipper (this repo), Moonraker, Mainsail, mainsail-config, and crowsnest.
# Replaces KIAUH. First-time install and every subsequent update go through
# this one script, invoked directly or via Moonraker's [update_manager
# klipper] install_script hook (a single "Update" button in Mainsail).
#
# Idempotent: every component's sync function is clone/download-if-missing +
# refresh + hook + restart, so `install` and `update` share the same code.
# Where an upstream project ships its own installer (moonraker, crowsnest),
# this script delegates to it rather than re-implementing it.
#
# Usage:
#   biokalico-installer.sh install              # first-time setup, all components
#   biokalico-installer.sh update [names...]    # sync components (default: all)
#   biokalico-installer.sh status               # show each component's current ref
#
# Bootstrap on a brand-new host (stock Debian, Raspbian, or MainsailOS):
#   git clone https://github.com/nicholas-karr/BioKalico.git ~/klipper
#   bash ~/klipper/scripts/biokalico-installer.sh install
#
# Called with no arguments (Moonraker's install_script convention), this
# defaults to `update` with no component filter, i.e. sync everything.

set -euo pipefail

BIOKALICO_ORIGIN="${BIOKALICO_ORIGIN:-https://github.com/nicholas-karr/BioKalico.git}"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
KLIPPY_ENV="${KLIPPY_ENV:-$HOME/klippy-env}"
PRINTER_DATA="${PRINTER_DATA:-$HOME/printer_data}"

MOONRAKER_DIR="${MOONRAKER_DIR:-$HOME/moonraker}"
MOONRAKER_ORIGIN="${MOONRAKER_ORIGIN:-https://github.com/Arksine/moonraker.git}"

MAINSAIL_DIR="${MAINSAIL_DIR:-$HOME/mainsail}"
MAINSAIL_ZIP_URL="${MAINSAIL_ZIP_URL:-https://github.com/mainsail-crew/mainsail/releases/latest/download/mainsail.zip}"

MAINSAIL_CONFIG_DIR="${MAINSAIL_CONFIG_DIR:-$HOME/mainsail-config}"
MAINSAIL_CONFIG_ORIGIN="${MAINSAIL_CONFIG_ORIGIN:-https://github.com/mainsail-crew/mainsail-config.git}"

CROWSNEST_DIR="${CROWSNEST_DIR:-$HOME/crowsnest}"
CROWSNEST_ORIGIN="${CROWSNEST_ORIGIN:-https://github.com/mainsail-crew/crowsnest.git}"

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

    # Also called from sync_moonraker() below: on a fresh install klipper
    # syncs before moonraker exists, so this copy is a no-op here (silently
    # skipped by patch-moonraker-component.sh, which is why it's `|| true`)
    # and sync_moonraker's copy is what actually lands it. On an
    # already-installed host (e.g. `update klipper` from
    # deploy-mainsail-kalico.sh) this copy is the one that matters, so
    # restart moonraker too - it only picks up new components at startup.
    bash "$KLIPPER_DIR/scripts/patch-moonraker-component.sh" "$KLIPPER_DIR/biokalico_extras/moonraker/home_root.py" || true
    bash "$KLIPPER_DIR/scripts/patch-moonraker-component.sh" "$KLIPPER_DIR/biokalico_extras/moonraker/firmware_build.py" || true

    restart_service klipper
    restart_service moonraker
}

install_klipper_packages() {
    command -v apt-get >/dev/null 2>&1 || { warn "klipper: apt-get not found, skipping package install"; return; }
    log "klipper: installing system packages"
    sudo apt-get update -qq
    # Same list as this repo's own scripts/install-debian.sh installer, plus
    # ccache (multi-MCU firmware build cache), dfu-util (STM32 DFU flashing),
    # and python3-serial (lib/canboot/flash_can.py needs pyserial for the
    # SYSTEM python3 make/scripts/flash_usb.py invokes it with - klippy-env's
    # own pyserial doesn't cover this, it's a separate interpreter). See
    # biokalico_extras/firmware_flash.md. Also curl/ca-certificates/unzip -
    # not needed by Klipper itself, but sync_mainsail() below needs curl to
    # download the Mainsail release zip and unzip to extract it, and every
    # HTTPS clone/download this script does needs ca-certificates. A
    # genuinely bare Debian host has none of the three - installing them
    # here up front means the rest of `install` can assume they exist
    # instead of failing partway through a later component.
    sudo apt-get install --yes \
        virtualenv python3-dev libffi-dev build-essential libncurses-dev libusb-dev \
        avrdude gcc-avr binutils-avr avr-libc \
        stm32flash libnewlib-arm-none-eabi gcc-arm-none-eabi binutils-arm-none-eabi \
        libusb-1.0 pkg-config \
        ccache dfu-util python3-serial \
        curl ca-certificates unzip
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

### moonraker #################################################################

sync_moonraker() {
    git_clone_or_pull "$MOONRAKER_DIR" "$MOONRAKER_ORIGIN"
    log "moonraker: running its own installer"
    # -z: skip install-moonraker.sh's own systemctl enable/daemon-reload -
    # we restart it ourselves below (once, after the component patches),
    # rather than have it started/reloaded twice. But that means WE own
    # both of those steps now: daemon-reload so systemd actually knows
    # about the freshly-written unit file (without this, restart_service's
    # own `systemctl list-unit-files | grep moonraker` guard can miss it
    # entirely, silently skipping the restart below), and enable so
    # moonraker survives a reboot instead of only running until the next one.
    bash "$MOONRAKER_DIR/scripts/install-moonraker.sh" -s -z
    sudo systemctl daemon-reload
    sudo systemctl enable moonraker

    # Deploy here too (not just from sync_klipper) so a fresh `install` -
    # where sync_klipper runs before ~/moonraker exists and its copy is a
    # no-op - still ends up with these components in place: this is the
    # first point in the install sequence where ~/moonraker/moonraker/components
    # is guaranteed to exist.
    bash "$KLIPPER_DIR/scripts/patch-moonraker-component.sh" "$KLIPPER_DIR/biokalico_extras/moonraker/home_root.py" || true
    bash "$KLIPPER_DIR/scripts/patch-moonraker-component.sh" "$KLIPPER_DIR/biokalico_extras/moonraker/firmware_build.py" || true

    restart_service moonraker
}

### mainsail (release zip, not git) ##########################################

sync_mainsail() {
    log "mainsail: downloading latest release"
    local tmpzip
    tmpzip="$(mktemp --suffix=.zip)"
    curl -fsSL "$MAINSAIL_ZIP_URL" -o "$tmpzip"

    local cfg_backup=""
    if [[ -f "$MAINSAIL_DIR/config.json" ]]; then
        cfg_backup="$(mktemp)"
        cp "$MAINSAIL_DIR/config.json" "$cfg_backup"
    fi

    # Extract into a staging dir and verify it BEFORE touching the existing
    # $MAINSAIL_DIR. `unzip` failing outright already aborts here under this
    # script's `set -e` -- the staging dir keeps that abort from happening
    # AFTER the old, working $MAINSAIL_DIR has been deleted. The index.html
    # check below also catches an unzip that "succeeds" but extracts
    # something unexpected (e.g. a truncated/corrupt zip unzip didn't error
    # on).
    local staging
    staging="$(mktemp -d)"
    unzip -qo "$tmpzip" -d "$staging"
    rm -f "$tmpzip"

    if [[ ! -f "$staging/index.html" ]]; then
        rm -rf "$staging"
        [[ -n "$cfg_backup" ]] && rm -f "$cfg_backup"
        warn "mainsail: extracted release is missing index.html, leaving existing $MAINSAIL_DIR untouched"
        return 1
    fi

    rm -rf "$MAINSAIL_DIR"
    mv "$staging" "$MAINSAIL_DIR"

    if [[ -n "$cfg_backup" ]]; then
        cp "$cfg_backup" "$MAINSAIL_DIR/config.json"
        rm -f "$cfg_backup"
    fi

    install_mainsail_nginx
    # `|| true`: patch-mainsail-panel.sh has its own `set -euo pipefail`, and
    # without this a single missing/unreadable panel source would abort the
    # rest of this function (and, since biokalico-installer.sh itself runs under
    # `set -e`, every component after mainsail in cmd_install) partway
    # through - after $MAINSAIL_DIR was already wiped and re-unzipped above.
    bash "$KLIPPER_DIR/scripts/patch-mainsail-panel.sh" "$KLIPPER_DIR/biokalico_extras/mainsail/projector-panel.js" || true
    bash "$KLIPPER_DIR/scripts/patch-mainsail-panel.sh" "$KLIPPER_DIR/biokalico_extras/mainsail/firmware-panel.js" || true
    bash "$KLIPPER_DIR/scripts/patch-mainsail-panel.sh" "$KLIPPER_DIR/biokalico_extras/mainsail/home-root-throttle.js" || true
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
    root $MAINSAIL_DIR;

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

    # Injected panel scripts (biokalico_extras/mainsail/*.js, e.g.
    # firmware-panel.js, projector-panel.js) live at a stable root-level
    # URL with no cache-busting hash in the filename - unlike Mainsail's own
    # /assets/*.js build output, which can be cached forever because a
    # content change always gets a new hashed filename. Without this,
    # updates get stuck behind any caching proxy in front of this host
    # (e.g. Cloudflare) for as long as its default static-asset cache TTL,
    # since nginx would otherwise send no explicit Cache-Control for these
    # at all. Matches any root-level .js file, not just the two above, so
    # future panel scripts don't need another edit here.
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
    git_clone_or_pull "$CROWSNEST_DIR" "$CROWSNEST_ORIGIN" "v5"
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
    for c in "${ALL_COMPONENTS[@]}"; do
        sync_component "$c"
    done
    ensure_services_running
}

cmd_update() {
    local components=("$@")
    [[ ${#components[@]} -eq 0 ]] && components=("${ALL_COMPONENTS[@]}")
    for c in "${components[@]}"; do
        sync_component "$c"
    done
    ensure_services_running
}

cmd_status() {
    local c dir
    for c in klipper moonraker mainsail-config crowsnest; do
        case "$c" in
            klipper) dir="$KLIPPER_DIR" ;;
            moonraker) dir="$MOONRAKER_DIR" ;;
            mainsail-config) dir="$MAINSAIL_CONFIG_DIR" ;;
            crowsnest) dir="$CROWSNEST_DIR" ;;
        esac
        if [[ -d "$dir/.git" ]]; then
            printf "%-16s %s\n" "$c" "$(git -C "$dir" log -1 --format='%h %cd %s' --date=short)"
        else
            printf "%-16s not installed\n" "$c"
        fi
    done
    if [[ -f "$MAINSAIL_DIR/index.html" ]]; then
        printf "%-16s installed (%s)\n" "mainsail" "$(date -r "$MAINSAIL_DIR/index.html" +%Y-%m-%d)"
    else
        printf "%-16s not installed\n" "mainsail"
    fi
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
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            ;;
        *)
            echo "Unknown command: $cmd (expected install, update, or status)" >&2
            exit 2
            ;;
    esac
}

main "$@"
