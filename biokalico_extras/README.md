# BioSlicer Kalico Macros for SLA Video Materials

Use [printer/sla_video_macros.cfg](printer/sla_video_macros.cfg) to:

1. Load videos once at job start.
2. Reference videos by short name.
3. Show specific frames as the slicer requests SLA operations.

## Layout

Files are grouped by which host-side component they get deployed into:

- `moonraker/`: Moonraker `.conf` includes and the `home_root.py` /
  `firmware_build.py` components.
- `mainsail/`: `projector-panel.js` / `firmware-panel.js` /
  `home-root-throttle.js` / `auto-reconnect.js`, copied into the Mainsail
  static site.
- `printer/`: `.cfg` macro files glob-included by `printer.cfg`.
- `firmware_presets/`: Kconfig build presets for the firmware panel (see
  [firmware_flash.md](firmware_flash.md)).

## Notes

- Embedded videos are loaded from comments via `LOAD_VIDEOS_FROM_GCODE`.
- External references use `[sla_video_path]` and can be pre-loaded with `SLA_LOAD_LOCAL_VIDEO`.
- Add your printer-specific movement, projector timing, and exposure control.
- With systemd, install/start the display server first: `scripts/install-image-display-service.sh`.

## G-Code Command Reference

### SLA Image Display

- **`SLA_CMD CMD=<cmd>`**: Execute a raw command on the SLA image display server.
- **`SLA_LOAD_GCODE_VIDEOS`**: Load all videos embedded in the current G-Code file, named `1`, `2`, etc.
- **`SLA_LOAD_LOCAL_VIDEO NAME=<name> PATH=<path>`**: Load a video from a local file path.
- **`SLA_UNLOAD_ALL`**: Unload all currently loaded videos.
- **`SLA_SHOW_FRAME NAME=<name> FRAME=<frame>`**: Display frame `FRAME` from loaded video `NAME`.
- **`SLA_CLEAR`**: Display black on projector (clear image).
- **`SLA_SET_H_OFFSET OFFSET=<offset>`**: Set horizontal image offset from center, in pixels.
- **`BIOSLICER_MATERIAL_SWAP MATERIAL=<material>`**: Move toolhead(s) or reposition resin bowl to start printing with the given material.

### Projector Control

- **`PROJECTOR_ON`**: Turn on the projector.
- **`PROJECTOR_STANDBY`**: Switch to a black image and start a 5-minute idle timer; turns off the bulb if no further commands arrive. Avoids the full startup delay on next use.
- **`PROJECTOR_OFF`**: Turn off the projector bulb. Requires >30 s to restart. Does not enter a deep sleep state requiring manual wakeup.
- **`PROJECTOR_BLOCK`**: Close the projector shutter (if installed). Always switches to a black image first.
- **`PROJECTOR_UNBLOCK`**: Open the projector shutter.

`PROJECTOR_BLOCK` / `PROJECTOR_UNBLOCK` are stubs: only some machines have a
physical shutter installed. If yours does, override the `gcode:` in your own
printer.cfg (after the line that includes this file); machines without a
shutter can leave the stubs as-is.

### Process Parameters (BioSlicer)

Sliced values (exposure, delay, temperatures) are written into
`BIOSLICER_PROCESS_PARAMS` by the G-code file at print start. These commands
let you inspect them and override any value before or during a print.

- **`BIOSLICER_INFO`**: Show current sliced values, any active overrides, and the override syntax.
- **`BIOSLICER_OVERRIDE PARAM=<name> EXTRUDER=<n> VALUE=<v>`**: Set one override. `EXTRUDER` is required for `sla_exposure`, `sla_delay_before`, `sla_delay_after`, `fff_first_layer_temp`, and `fff_temp`; omit it for `bed_first_layer_temp` and `bed_temp`.
- **`BIOSLICER_CLEAR_OVERRIDE PARAM=<name> [EXTRUDER=<n>]`**: Clear one override. Omitting `EXTRUDER` on a per-extruder param clears it for every extruder.
- **`BIOSLICER_CLEAR_ALL_OVERRIDES`**: Reset every override. Run this after a print if you set any overrides during that session.

## First-Time Host Setup

New hosts should be provisioned with `scripts/biokalico-installer.sh install`
(see the top-level README and `QUICKSTART.md`). It replaces KIAUH, cloning
and wiring up Klipper, Moonraker, Mainsail, mainsail-config, and crowsnest in
one pass. It installs the systemd service as `klipper`. The firmware
build/flash panel (`biokalico_extras/firmware_flash.md`) hardcodes that unit
name when stopping and restarting the service around a flash, so a host must
not use any other service name.

1. **Run the installer.**
   ```
   bash ~/klipper/scripts/biokalico-installer.sh install
   ```
   This creates `~/klipper` (this repo), `~/klippy-env`,
   `~/printer_data/config/{printer.cfg,moonraker.conf}`, and the
   `klipper`/`moonraker` systemd services.

2. **Apply the BioKalico patches below.** The generated `printer.cfg` and
   `moonraker.conf` don't know about this repo's macros or projector UI
   until the includes are added and the Mainsail patch script is run.

## BioKalico Host Setup (Moonraker / Mainsail)

These pieces of the projector UI live outside the printer G-code macros and
must be wired up by hand on each host, since `printer_data/config/` and
`~/mainsail/` are not part of this repo. Note that `[include ...]` directives
in `printer.cfg` and `moonraker.conf` are the one place `~` can't be used
(Klipper and Moonraker resolve them by literal path-joining, with no `~`
expansion), so these two use a path relative to the including file instead:

1. **`printer.cfg` macro include.** `printer.cfg` must glob-include the
   `printer/` directory so `projector_panel.cfg` (and the other `.cfg` files
   there) load:
   ```
   [include ../../klipper/biokalico_extras/printer/*.cfg]
   ```
   (path is relative to `printer.cfg`'s directory, typically
   `~/printer_data/config/`).

2. **`moonraker.conf` include.** The `[power Projector...]` button sections
   live in
   [moonraker/moonraker_projector.conf](moonraker/moonraker_projector.conf),
   not in `moonraker.conf` itself, so they travel with this repo instead of
   the per-host config. Add to `moonraker.conf`:
   ```
   [include ../../klipper/biokalico_extras/moonraker/moonraker_projector.conf]
   ```
   (path is relative to `moonraker.conf`'s directory, typically
   `~/printer_data/config/`).

3. **`projector-panel.js` on the Mainsail host.** Mainsail is deployed as a
   release zip (`type: web` in `[update_manager mainsail]`), not a git
   checkout, so [mainsail/projector-panel.js](mainsail/projector-panel.js) is kept here as the
   source of truth and copied into place by
   `scripts/patch-mainsail-panel.sh mainsail/projector-panel.js`, which also
   re-injects its `<script>` tag into `index.html`. `persistent_files` in
   `[update_manager mainsail]` preserves `projector-panel.js` itself across
   Mainsail updates, but **not** `index.html`, so this script must be re-run
   after every Mainsail update (it's currently a manual step; there's no
   Moonraker post-update hook wired up for it). The same script (it's
   parameterized) also deploys
   [mainsail/firmware-panel.js](mainsail/firmware-panel.js). See
   [firmware_flash.md](firmware_flash.md).

4. **`$HOME` as a root in Mainsail's Config Files page.** Mainsail's Machine
   > Config Files panel has a "Root" selector, but its options come straight
   from Moonraker's `registered_directories` (see `server.info`). Moonraker
   only registers `config`/`logs`/`gcodes`/`config_examples`/`docs` out of
   the box, so `home` never shows up on its own.
   [moonraker/home_root.py](moonraker/home_root.py) is a Moonraker component that registers
   `$HOME` as an additional root named `home` (read-only by default; set
   `full_access: True` in [moonraker/moonraker_home_root.conf](moonraker/moonraker_home_root.conf)
   to allow edit/delete through it). It's dropped in as an *untracked* file
   under `moonraker/components/`, not a patch to Moonraker's own tracked
   source. Moonraker auto-updates itself via git and refuses to update a
   dirty (tracked-file-modified) repo, and Moonraker itself already treats
   untracked `.py` files under `components/` as a supported
   "unofficial component" mechanism, so this survives Moonraker updates
   for free. `scripts/patch-moonraker-component.sh` (parameterized, takes
   the component's path as an argument) copies it into place (idempotent),
   and `scripts/deploy-mainsail-kalico.sh` runs it, and restarts
   `moonraker`, on every deploy. If `moonraker` was set up some other way,
   run it once by hand and restart Moonraker:
   ```
   bash ~/klipper/scripts/patch-moonraker-component.sh ~/klipper/biokalico_extras/moonraker/home_root.py
   sudo systemctl restart moonraker
   ```
   Also add to `moonraker.conf`:
   ```
   [include ../../klipper/biokalico_extras/moonraker/moonraker_home_root.conf]
   ```
   (path is relative to `moonraker.conf`'s directory, typically
   `~/printer_data/config/`).

   **[mainsail/home-root-throttle.js](mainsail/home-root-throttle.js)** must
   also be deployed alongside this (same `patch-mainsail-panel.sh` as the
   other panels). Mainsail's Configure page recursively enumerates every
   registered root to build its file tree. This is fine for the small default
   roots, but `home` points at `$HOME`, which can be arbitrarily large and
   deep; without this, that recursive walk generates thousands of requests
   per page load, each one running Moonraker's directory listing
   synchronously on its event loop, long enough to trip Moonraker's own
   `EVENT LOOP BLOCKED` watchdog and drop the websocket connection
   entirely. This script intercepts the WebSocket transport client-side and
   suppresses only the automatic recursive follow-up requests (detected by
   their exact cause-and-effect timing relative to their parent directory's
   response), never anything a manual click sends. See the file's own
   comments for the full reasoning.

5. **Firmware build/flash panel.** A "Firmware" card in Mainsail's Settings
   page that builds and flashes this printer's micro-controllers over the
   bootloader, with per-board build presets stored in `printer.cfg`. Uses
   the same `moonraker/`/`mainsail/` deployment mechanisms as items 3-4
   above. See [firmware_flash.md](firmware_flash.md) for the full writeup
   (preset format, safety notes, REST endpoints, host setup).

6. **Auto-reconnect for Mainsail's "Connection Lost" dialog.**
   [mainsail/auto-reconnect.js](mainsail/auto-reconnect.js), deployed the
   same way as the other panel scripts (`patch-mainsail-panel.sh
   mainsail/auto-reconnect.js`). Mainsail's own websocket client gives up
   after only 2 failed reconnect attempts and then just sits there showing
   a "Try Again" button until someone clicks it. A Klippy restart or a
   brief network blip is enough to trigger that. This script watches for
   that dialog (matched on its fixed `the-connection-dialog` card-class,
   not its locale-specific button text) and clicks "Try Again" for
   you every couple of seconds until the connection actually comes back.
   See the file's own comments for the full reasoning.

## Non-Default Install Paths

MainsailOS defaults: `send-image.py` at `~/klipper/scripts/sla/send-image.py`,
host/port `127.0.0.1:5555`, service name `bioslicer-image-display.service`. If
your install path or port differs, edit the `gcode_shell_command SLA_CMD_SHELL`
line.

## Debugging

Test images are generated by `scripts/sla/gen-test-images.py` and sent directly to the display. No video needs to be loaded first. All commands share an `ASPECT` parameter: `screen` (native resolution), `16:9`, `4:3`, or `1:1`. Default is `16:9`.

- **`DISPLAY_TEST_BLUE ASPECT=<aspect>`**: Display a solid blue field. Confirms projector is on, image server is reachable, and the full frame is illuminated.

- **`DISPLAY_TEST_CHECKERBOARD ASPECT=<aspect> REGIONS=<n>`**: Display an alternating black/white checkerboard. `REGIONS` sets the number of grid divisions per axis (default 8). Use to check display uniformity and pixel response.

- **`DISPLAY_TEST_NESTED_SQUARES ASPECT=<aspect> OFFSET_X=<px> OFFSET_Y=<px> NESTED_COUNT=<n> LINE_WIDTH=<px>`**: Display concentric square outlines centered on the build plate. `OFFSET_X`/`OFFSET_Y` shift the center in pixels (default 0). Use with `SLA_SET_H_OFFSET` to align the image to the resin vat.

- **`DISPLAY_TEST_DENSE_TEXT ASPECT=<aspect> FONT_SIZE=<pt>`**: Fill the screen with dense text at the given point size (default 0 = auto). Use to check focus, resolution, and pixel sharpness across the frame.
