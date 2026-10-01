#!/bin/bash
# Restart every host-side service the printer depends on, plus the MCU
# firmware itself, and/or tail all their logs together in one stream.
#
# "Services" = the systemd units in SERVICES below (klipper, moonraker,
# crowsnest, nginx, and the SLA image-display server). "Firmware" = the
# actual MCU firmware ([mcu] and [mcu nhk] in printer.cfg), reset via
# Moonraker's /printer/firmware_restart endpoint once the services above are
# back up. A plain `systemctl restart klipper` reconnects to the MCUs but
# does not power-cycle their firmware the way FIRMWARE_RESTART does.
#
# Usage:
#   printer-services.sh [--restart] [--logs]
#
# Both flags default to false; running with neither set shows this help,
# since otherwise the script would do nothing.

set -euo pipefail

PRINTER_DATA="${PRINTER_DATA:-$HOME/printer_data}"
LOG_DIR="$PRINTER_DATA/logs"
MOONRAKER_HOST="${MOONRAKER_HOST:-127.0.0.1}"
MOONRAKER_PORT="${MOONRAKER_PORT:-7125}"
MOONRAKER_URL="http://${MOONRAKER_HOST}:${MOONRAKER_PORT}"
# Authenticates with the API key, which works whether or not local requests
# need a login (see moonraker_api.py).
moonraker_api() { python3 "$(dirname "${BASH_SOURCE[0]}")/moonraker_api.py" "$@"; }

# Order matters: klipper before moonraker (moonraker talks to klipper over
# its unix socket), everything else can follow in any order.
SERVICES=(klipper moonraker crowsnest nginx bioslicer-image-display)

RESTART=0
LOGS=0

usage() {
    cat <<EOF
Usage: printer-services.sh [--restart] [--logs]

Restarts every systemd service the printer stack depends on
(${SERVICES[*]}), then resets the physical MCU firmware
([mcu] / [mcu nhk] in printer.cfg) via Moonraker's firmware_restart
endpoint -- and/or tails all of their logs together as they advance.

Options:
  --restart    Restart all services above, then firmware-restart the MCUs.
                 (default: false)
  --logs       Tail klippy.log, moonraker.log, crowsnest.log, nginx's
               access/error logs, and the image-display service's journal,
               all interleaved in one stream. Ctrl-C to stop.
                 (default: false)
  -h, --help   Show this help

With both flags set, restarts happen first, then log tailing starts so you
can watch everything come back up. Running with neither flag set (or no
arguments) just shows this help.
EOF
}

log() { echo "==> $*"; }
warn() { echo "==> WARNING: $*" >&2; }

restart_service() {
    local svc="$1"
    # Check the unit file directly on disk rather than asking systemd
    # (`systemctl list-unit-files`) whether the unit exists -- as a regular
    # user that can silently come back empty even with a working sudo (see
    # biokalico-installer.sh's restart_service() for the full story). Checking both
    # /etc and /lib matches service_unit_exists() in biokalico-installer.sh, since a
    # unit installed by an upstream package (e.g. nginx) lands in /lib, not
    # /etc.
    if [[ -f "/etc/systemd/system/${svc}.service" ]] || [[ -f "/lib/systemd/system/${svc}.service" ]]; then
        log "restarting ${svc}.service"
        sudo systemctl restart "$svc"
    else
        warn "${svc}.service not found, skipping"
    fi
}

wait_for_klippy() {
    log "waiting for moonraker + klippy to come back up"
    local i
    for ((i = 0; i < 60; i++)); do
        if moonraker_api "${MOONRAKER_URL}/server/info" 2>/dev/null | grep -q '"klippy_connected":true'; then
            return 0
        fi
        sleep 1
    done
    warn "moonraker/klippy did not come back within 60s, trying firmware_restart anyway"
}

firmware_restart() {
    log "firmware-restarting MCUs via ${MOONRAKER_URL}/printer/firmware_restart"
    if ! moonraker_api --post "${MOONRAKER_URL}/printer/firmware_restart" >/dev/null; then
        warn "firmware_restart request failed -- is moonraker reachable at ${MOONRAKER_URL}?"
    fi
}

klippy_state() {
    # `|| true`: under pipefail, a transient curl failure or an unmatched
    # grep (both expected right after firmware_restart, while moonraker is
    # mid-reconnect) would otherwise make this pipeline's exit status
    # non-zero and trip `set -e` at the call site, killing the whole script.
    # Falling back to an empty string here just makes the caller's retry
    # loop treat it as "not ready yet", which is what we want.
    moonraker_api "${MOONRAKER_URL}/printer/info" 2>/dev/null \
        | grep -oE '"state"[[:space:]]*:[[:space:]]*"[a-z]+"' \
        | head -1 \
        | sed -E 's/.*"([a-z]+)"$/\1/' || true
}

FIRMWARE_RESTART_ATTEMPTS="${FIRMWARE_RESTART_ATTEMPTS:-3}"

wait_for_ready_after_firmware_restart() {
    # firmware_restart can lose a race with a slow-to-reboot MCU (rp2040 USB
    # re-enumeration on "nhk" in particular): klippy reconnects before the
    # MCU has actually cleared its old config, sees a stale CRC mismatch, and
    # lands in klippy_state "error" with "Failed automated reset of MCU ...".
    # Klipper's own fix for that is just running FIRMWARE_RESTART again once
    # the MCU has actually finished rebooting -- do that here automatically
    # instead of requiring it by hand.
    local attempt state i
    for ((attempt = 1; attempt <= FIRMWARE_RESTART_ATTEMPTS; attempt++)); do
        state=""
        for ((i = 0; i < 30; i++)); do
            state="$(klippy_state)"
            [[ "$state" == "ready" || "$state" == "error" ]] && break
            sleep 1
        done
        if [[ "$state" == "ready" ]]; then
            return 0
        fi
        if [[ "$attempt" -lt "$FIRMWARE_RESTART_ATTEMPTS" ]]; then
            warn "klippy state is '${state:-unknown}' after firmware_restart -- an MCU is probably still rebooting, retrying (${attempt}/${FIRMWARE_RESTART_ATTEMPTS})"
            sleep 2
            firmware_restart
        fi
    done
    warn "klippy did not reach 'ready' after ${FIRMWARE_RESTART_ATTEMPTS} firmware_restart attempts -- check moonraker/klippy manually"
}

do_restart() {
    for svc in "${SERVICES[@]}"; do
        restart_service "$svc"
    done
    wait_for_klippy
    firmware_restart
    wait_for_ready_after_firmware_restart
}

do_logs() {
    log "tailing logs from: ${SERVICES[*]} (Ctrl-C to stop)"
    local pids=()
    trap 'kill "${pids[@]}" 2>/dev/null' EXIT INT TERM

    # klipper, moonraker, and crowsnest each write their own log file.
    local files=()
    for f in klippy.log moonraker.log crowsnest.log; do
        [[ -e "$LOG_DIR/$f" ]] && files+=("$LOG_DIR/$f")
    done
    if [[ ${#files[@]} -gt 0 ]]; then
        tail -n 20 -f "${files[@]}" &
        pids+=($!)
    fi

    # nginx's logs are symlinked into printer_data/logs as mainsail-*.log,
    # but owned by www-data:adm -- read them via sudo like the restarts do.
    local nginx_files=()
    for f in mainsail-access.log mainsail-error.log; do
        [[ -e "$LOG_DIR/$f" ]] && nginx_files+=("$LOG_DIR/$f")
    done
    if [[ ${#nginx_files[@]} -gt 0 ]]; then
        sudo tail -n 20 -f "${nginx_files[@]}" &
        pids+=($!)
    fi

    # bioslicer-image-display has no dedicated log file -- only journald.
    # Same direct-file check as restart_service() above, not
    # `systemctl list-unit-files` (unreliable as a regular user).
    if [[ -f "/etc/systemd/system/bioslicer-image-display.service" ]] || [[ -f "/lib/systemd/system/bioslicer-image-display.service" ]]; then
        journalctl -f -u bioslicer-image-display -n 20 &
        pids+=($!)
    fi

    wait
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restart)
            RESTART=1
            shift
            ;;
        --logs)
            LOGS=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ "$RESTART" -eq 0 && "$LOGS" -eq 0 ]]; then
    usage
    exit 0
fi

if [[ "$RESTART" -eq 1 ]]; then
    do_restart
fi
if [[ "$LOGS" -eq 1 ]]; then
    do_logs
fi
