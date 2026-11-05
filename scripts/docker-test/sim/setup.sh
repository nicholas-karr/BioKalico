#!/bin/bash
# Turns a container that biokalico-installer.sh has already installed into a
# simulated printer: real Klipper, Moonraker and Mainsail, with the MCUs
# replaced by Klipper's file-output mode (nothing is ever sent to hardware).
#
# Runs INSIDE the container, as the biokalico user. scripts/docker-test/run.sh
# --sim calls it, then runs e2e.py from the host.
#
# What it does:
#   1. Builds MCU data dictionaries from THIS tree, so they always match
#      klippy (a dictionary from another revision fails to encode commands).
#   2. Builds printer.cfg from bio_config/biotrident.cfg, the template that is
#      deployed to real printers, changing only the serial paths and layering
#      sim/overlay.cfg (docks, motion macros, a pluggable board) on top.
#   3. Points klipper.service at file-output mode and starts it.
#   4. Lets the host talk to Moonraker without the shared password (local
#      requests only; the container is a throwaway).

set -euo pipefail

KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
PRINTER_DATA="${PRINTER_DATA:-$HOME/printer_data}"
SIM_DIR="$HOME/sim"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$PRINTER_DATA/config"

log() { echo "==> sim: $*"; }

mkdir -p "$SIM_DIR/out" "$PRINTER_DATA/gcodes"
# The Makefile needs this (git ignores it, so a fresh clone lacks it).
mkdir -p "$KLIPPER_DIR/src/extras"

for mcu in stm32h743 rp2040; do
    build="$SIM_DIR/build-$mcu"
    log "building the $mcu data dictionary"
    mkdir -p "$build"
    cp "$KLIPPER_DIR/test/configs/$mcu.config" "$build/.config"
    make -s -C "$KLIPPER_DIR" KCONFIG_CONFIG="$build/.config" OUT="$build/" olddefconfig >/dev/null
    make -s -C "$KLIPPER_DIR" KCONFIG_CONFIG="$build/.config" OUT="$build/" -j"$(nproc)" >/dev/null
    cp "$build/klipper.dict" "$SIM_DIR/$mcu.dict"
done

log "writing printer.cfg from bio_config/biotrident.cfg plus the sim overlay"
mkdir -p "$CONFIG"
cp "$HERE/mainsail.cfg" "$CONFIG/mainsail.cfg"
# The template ends with Klipper's SAVE_CONFIG block, which has to stay last,
# so the overlay goes in front of it.
sed -E \
    -e 's#^serial: /dev/serial/by-id/usb-Klipper_stm32h743xx_.*#serial: /tmp/sim-mcu-main#' \
    -e 's#^serial: /dev/serial/by-id/usb-Klipper_rp2040_.*#serial: /tmp/sim-mcu-nhk#' \
    "$KLIPPER_DIR/bio_config/biotrident.cfg" \
    | awk -v overlay="$HERE/overlay.cfg" '
        /^#\*# <-+ SAVE_CONFIG -+>/ && !done {
            while ((getline line < overlay) > 0) print line
            print ""
            done = 1
        }
        { print }' > "$CONFIG/printer.cfg"

# Every run starts from the config's own initial states.
rm -f "$CONFIG/toolheads_state.cfg" "$CONFIG/materials_state.cfg"

log "switching klipper.service to file-output mode"
klippy="$HOME/klippy-env/bin/python $KLIPPER_DIR/klippy/klippy.py $CONFIG/printer.cfg"
klippy="$klippy -l $PRINTER_DATA/logs/klippy.log -a $PRINTER_DATA/comms/klippy.sock"
klippy="$klippy -o $SIM_DIR/out/out -d $SIM_DIR/stm32h743.dict"
klippy="$klippy -d nhk=$SIM_DIR/rp2040.dict -d usbhead=$SIM_DIR/rp2040.dict"
sudo mkdir -p /etc/systemd/system/klipper.service.d
printf '[Service]\nExecStart=\nExecStart=%s\n' "$klippy" \
    | sudo tee /etc/systemd/system/klipper.service.d/sim.conf >/dev/null
sudo systemctl daemon-reload

log "letting local requests reach Moonraker without the shared password"
sed -i 's/^local_bypass:.*/local_bypass: True/' "$CONFIG/moonraker.conf"

# What each slot holds is set the way Mainsail sets it, through Moonraker.
load_slots() {
    local group slot material
    while read -r group slot material; do
        curl -sf -o /dev/null -X POST -H 'Content-Type: application/json' \
            -d "{\"group\": \"$group\", \"slot\": $slot, \"material\": \"$material\"}" \
            http://127.0.0.1:7125/printer/materials/set_slot
    done <<'SLOTS'
toolhead1 1 basic_pla
syringe 1 pcl_bioink
solenoid 1 silicone
usb_head 1 usb_ink
sla 1 standard_resin
sla 2 gelma_10
sla 3 pegda_20
sla 4 alginate_gelma
SLOTS
}

# The board that can be plugged in and out starts unplugged.
python3 "$HERE/usb_board.py" unplug

sudo systemctl restart moonraker
sudo systemctl restart klipper

log "waiting for klippy to become ready"
for _ in $(seq 1 60); do
    state="$(curl -s http://127.0.0.1:7125/printer/info 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' 2>/dev/null || true)"
    if [[ "$state" == "ready" ]]; then
        log "ready, loading the material slots"
        load_slots
        exit 0
    fi
    sleep 2
done
log "klippy did not become ready (state: ${state:-unknown})"
tail -30 "$PRINTER_DATA/logs/klippy.log" >&2
exit 1
