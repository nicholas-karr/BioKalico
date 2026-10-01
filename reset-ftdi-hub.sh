#!/bin/bash
# Cuts power to the USB hub that carries the SLA projector's serial adapter,
# by writing the xHCI PORTSC register of the computer port the hub is plugged
# into. Everything else on that hub loses power too, including any Klipper
# MCU. Use it when software resets don't bring the adapter back. It refuses
# to run while a print is printing or paused.
#
# Off until configured. The hub is the one named by projector_usb_hub_path
# under [auto_recovery] in moonraker.conf (deps/moonraker/docs/configuration.md
# says how to find it), and it must be plugged straight into the computer.
#
# Needs kernel lockdown "none", which means Secure Boot must be off. Check:
#   cat /sys/kernel/security/lockdown
#
# Usage:
#   reset-ftdi-hub.sh           # power-cycle the hub
#   reset-ftdi-hub.sh --check   # show which port would be power-cycled

set -euo pipefail

MOONRAKER_URL="${MOONRAKER_URL:-http://127.0.0.1:7125}"

log() { echo "==> $*"; }
warn() { echo "==> WARNING: $*" >&2; }
die() { echo "==> ERROR: $*" >&2; exit 1; }

check_only=0
case "${1:-}" in
    "") ;;
    --check) check_only=1 ;;
    *) die "unknown argument: $1 (expected --check or nothing)" ;;
esac

moonraker_api() { python3 "$(dirname "${BASH_SOURCE[0]}")/scripts/moonraker_api.py" "$@"; }

# Prints one [auto_recovery] option from Moonraker's loaded config, or
# nothing when it is not set.
auto_recovery_option() {
    moonraker_api "${MOONRAKER_URL}/server/config" 2>/dev/null \
        | python3 -c '
import json, sys
section = json.load(sys.stdin)["result"]["config"].get("auto_recovery", {})
print(section.get(sys.argv[1]) or "")' "$1" 2>/dev/null
}

hub_path="$(auto_recovery_option projector_usb_hub_path)" \
    || die "couldn't read moonraker's config (${MOONRAKER_URL}) - refusing to cut USB power on a guess"
if [[ -z "$hub_path" ]]; then
    die "not configured: set projector_usb_hub_path under [auto_recovery] in moonraker.conf to the hub to power-cycle, see this script's header comment"
fi

hub_dev="$(readlink -f "$hub_path")"
if [[ ! -d "$hub_dev" ]]; then
    die "projector_usb_hub_path (${hub_path}) is not a USB device on this machine"
fi
# A device plugged straight into the computer is named <bus>-<port>; one
# behind another hub has more port numbers (<bus>-<port>.<port>).
if [[ ! "$(basename "$hub_dev")" =~ ^[0-9]+-([0-9]+)$ ]]; then
    die "the hub at ${hub_path} is behind another hub - power can only be cut at one of the computer's own ports"
fi
root_port="${BASH_REMATCH[1]}"
root_hub="$(dirname "$hub_dev")"
xhci_debug="/sys/kernel/debug/usb/xhci/$(basename "$(dirname "$root_hub")")"

lockdown="$(cat /sys/kernel/security/lockdown 2>/dev/null || echo "unknown")"
if [[ "$lockdown" != *"[none]"* ]]; then
    die "kernel lockdown is not 'none' (currently: ${lockdown}) - this needs Secure Boot disabled in firmware first, see this script's header comment"
fi
if ! sudo test -d "$xhci_debug"; then
    die "no xHCI debugfs directory at ${xhci_debug} - the hub's controller is not an xHCI one"
fi

# The controller numbers its USB2 and USB3 ports in one list. Each
# reg-ext-protocol file describes one run of that list: the USB major version
# is the top byte of EXTCAP_REVISION, and EXTCAP_PORTINFO holds the first port
# (low byte) and the port count (next byte). The root hub's own ports count
# through the runs of its USB version in order.
usb_major=2
if (( $(cut -d. -f1 "$root_hub/speed") >= 5000 )); then
    usb_major=3
fi
port_index=""
remaining="$root_port"
for cap in $(sudo ls "$xhci_debug" | grep '^reg-ext-protocol:' | sort); do
    regs="$(sudo cat "$xhci_debug/$cap")"
    revision="$(sed -n 's/^EXTCAP_REVISION = //p' <<<"$regs")"
    portinfo="$(sed -n 's/^EXTCAP_PORTINFO = //p' <<<"$regs")"
    (( (revision >> 24) == usb_major )) || continue
    count=$(( (portinfo >> 8) & 0xff ))
    if (( remaining <= count )); then
        port_index=$(( (portinfo & 0xff) + remaining - 1 ))
        break
    fi
    remaining=$(( remaining - count ))
done
if [[ -z "$port_index" ]]; then
    die "couldn't find port ${root_port} of $(basename "$root_hub") among the controller's USB${usb_major} ports in ${xhci_debug}"
fi
PORTSC="${xhci_debug}/ports/$(printf 'port%02d' "$port_index")/portsc"

if ! sudo test -e "$PORTSC"; then
    die "no such debugfs port file: ${PORTSC}"
fi
port_state="$(sudo cat "$PORTSC")"
# The hub is plugged in, so its port must say so. Anything else means the
# port was worked out wrong.
if [[ "$port_state" != *" Connected "* ]]; then
    die "${PORTSC} reports no device (${port_state}), but the hub at ${hub_path} is plugged in - refusing to cut power to the wrong port"
fi

log "hub ${hub_path} is on ${PORTSC}"
log "current port state: ${port_state}"
if (( check_only )); then
    log "check only, nothing was power-cycled"
    exit 0
fi

print_state="$(moonraker_api "${MOONRAKER_URL}/printer/objects/query?print_stats" 2>/dev/null \
    | grep -oE '"state"[[:space:]]*:[[:space:]]*"[a-z]+"' | tail -1 | sed -E 's/.*"([a-z]+)"$/\1/' || true)"
if [[ -z "$print_state" ]]; then
    die "couldn't confirm print state from moonraker (${MOONRAKER_URL}) - refusing to cut USB power on a guess"
fi
if [[ "$print_state" == "printing" || "$print_state" == "paused" ]]; then
    die "print_stats state is '${print_state}' - refusing to cut power to the Klipper MCUs' hub while a print is active"
fi

log "print state is '${print_state}' - safe to proceed"

log "powering off (clearing Port Power bit)"
sudo sh -c "echo 0x0 > '${PORTSC}'"
sleep 5

log "powering back on (setting Port Power bit)"
sudo sh -c "echo 0x200 > '${PORTSC}'"
sleep 5

log "port state after power-cycle: $(sudo cat "$PORTSC")"

if [[ ! -d "$hub_dev" ]]; then
    warn "the hub at ${hub_path} is still not present after the power-cycle - this needs physical attention (cable/hub)"
    exit 1
fi
adapter_port="$(auto_recovery_option projector_usb_port_path || true)"
if [[ -z "$adapter_port" ]]; then
    log "hub is back"
elif [[ -e "${adapter_port}/device" ]]; then
    log "hub and projector adapter are back"
else
    warn "hub is back, but nothing is plugged into ${adapter_port} - the projector adapter needs physical attention (cable/adapter)"
fi
