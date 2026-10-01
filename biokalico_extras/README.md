# BioSlicer Kalico Macros for SLA Video Materials

Use [printer/sla_video_macros.cfg](printer/sla_video_macros.cfg) to:

1. Load videos once at job start.
2. Reference videos by short name.
3. Show specific frames as the slicer requests SLA operations.

## Layout

- `moonraker/`: Moonraker `.conf` fragments, `[include ...]`d into
  `moonraker.conf` (see items 1-2 below) - except `[simple_password_auth]`,
  which lives directly in `moonraker.conf` itself rather than a fragment
  here, since it ends up holding a real secret and this directory is
  inside the git repo (see item 8). The actual Moonraker-side code for the
  projector power buttons, the `home` config root, the firmware
  build/flash panel, the "Restart All" button, the shared-password login,
  the per-print timelapse backend, and automatic fault recovery all lives as
  first-party, tracked source in `deps/moonraker` (BioKalico's own Moonraker
  fork,
  `moonraker/components/{home_root,firmware_build,printer_services,presence_prune,simple_password_auth,timelapse,auto_recovery}.py`)
  - not here.
- `mainsail/`: (empty of panel scripts as of this repo's submodule
  conversion) - the projector panel, firmware build/flash panel, "Restart
  All" button, auto-reconnect, presence warning, and shared-password login
  all live as real Vue/Vuex source in `deps/mainsail`
  (BioKalico's own Mainsail fork), built by Mainsail's own `npm run build`,
  not injected into a downloaded release.
- `printer/`: `.cfg` macro files glob-included by `printer.cfg`.
- `firmware_presets/`: Kconfig build presets for the firmware panel (see
  [firmware_flash.md](firmware_flash.md)).

## Notes

- Embedded videos are loaded from comments via `LOAD_VIDEOS_FROM_GCODE`.
- External references use `[sla_video_path]` and can be pre-loaded with `SLA_LOAD_LOCAL_VIDEO`.
- Add your printer-specific movement, projector timing, and exposure control.
- With systemd, install/start the display server first:
  `sudo scripts/sla/install-image-display-service.sh`.

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

`PROJECTOR_ON`, `PROJECTOR_OFF`, `PROJECTOR_STANDBY` and the other commands
the SLA macros above are built on come from the `[image_display]` module;
see [image_display](../docs/G-Codes.md#image_display) in the G-code
reference.

- **`PROJECTOR_BLOCK`**: Close the projector shutter (if installed). As shipped it only shows black (`SLA_CLEAR`).
- **`PROJECTOR_UNBLOCK`**: Open the projector shutter.

`PROJECTOR_BLOCK` / `PROJECTOR_UNBLOCK` are stubs: only some machines have a
physical shutter installed. If yours does, override the `gcode:` in your own
printer.cfg (after the line that includes this file), and start the
`PROJECTOR_BLOCK` override with `SLA_CLEAR` so it still shows black;
machines without a shutter can leave the stubs as-is.

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

2. **Apply the includes below.** The generated `printer.cfg` and
   `moonraker.conf` don't know about this repo's macros or projector UI
   until the `[include ...]` lines are added - the projector/firmware/login/
   home-root Mainsail and Moonraker features themselves need no separate
   deploy step, since they're built directly into the `deps/mainsail` and
   `deps/moonraker` submodules by `biokalico-installer.sh`.

## BioKalico Host Setup (Moonraker / Mainsail)

The `[include ...]` fragments below live outside the printer G-code macros
and must be wired up by hand on each host, since `printer_data/config/` is
not part of this repo. Note that `[include ...]` directives in `printer.cfg`
and `moonraker.conf` are the one place `~` can't be used (Klipper and
Moonraker resolve them by literal path-joining, with no `~` expansion), so
these two use a path relative to the including file instead:

1. **`printer.cfg` macro include.** `printer.cfg` must glob-include the
   `printer/` directory so the `.cfg` files there load:
   ```
   [include ../../klipper/biokalico_extras/printer/*.cfg]
   ```
   (path is relative to `printer.cfg`'s directory, typically
   `~/printer_data/config/`).

2. **`moonraker.conf` includes.** The Moonraker sections for the items
   below live in `moonraker/*.conf`, not in `moonraker.conf` itself, so they
   travel with this repo instead of the per-host config. Each item gives
   its include line; `bio_config/moonraker.conf.example`, which the
   installer copies on first install, already has all of them.

3. **Projector panel on the Mainsail dashboard.** A real Vue component in
   `deps/mainsail` (BioKalico's own Mainsail fork, vendored as a git
   submodule and built on-host by `biokalico-installer.sh`'s
   `npm run build`), polling the `image_display` printer object and shown
   as a dashboard card. The card only appears on a printer with an
   `[image_display]` section, and has Turn On/Off, Black Screen and Standby
   buttons that are grayed out while the display server isn't answering or
   the projector's serial link is down. See
   [firmware_flash.md](firmware_flash.md) for the related firmware panel. Because this is now first-party source built by
   Mainsail's own Vite pipeline, none of the old release-zip-patching
   machinery applies: no raw `<script>` injection into a downloaded
   `index.html`, and no service-worker cache-busting hack - a normal
   `npm run build` regenerates the service worker's precache manifest
   itself, the same as any other Mainsail change.

4. **`$HOME` as a root in Mainsail's Config Files page.** Mainsail's Machine
   > Config Files panel has a "Root" selector, but its options come straight
   from Moonraker's `registered_directories` (see `server.info`). Moonraker
   only registers `config`/`logs`/`gcodes`/`config_examples`/`docs` out of
   the box, so `home` never shows up on its own.
   `deps/moonraker/moonraker/components/home_root.py` (BioKalico's own
   Moonraker fork) registers `$HOME` as an additional root named `home`
   (read-only by default; see `[home_root]` in
   `deps/moonraker/docs/configuration.md`). It's a normal, tracked, first-party
   component in that fork - not a Moonraker-update-surviving untracked
   drop-in, since forking Moonraker removed the need for that workaround
   entirely (a fork can just carry its own tracked source; there's no
   upstream `git pull` to go dirty against). Add to `moonraker.conf`:
   ```
   [include ../../klipper/biokalico_extras/moonraker/moonraker_home_root.conf]
   ```
   (path is relative to `moonraker.conf`'s directory, typically
   `~/printer_data/config/`).

   Mainsail's Configure page used to recursively enumerate every registered
   root to build its file tree on load - fine for the small default roots,
   but ruinous for `home` (`$HOME` can be arbitrarily large and deep;
   observed directly: 11,000+ requests in 11 seconds, tripping Moonraker's
   `EVENT LOOP BLOCKED` watchdog and dropping the websocket). This is now
   fixed at the source in the Mainsail fork (lazy per-node expansion instead
   of eager recursion) rather than worked around client-side.

5. **Firmware build/flash panel.** A "Firmware" card that builds and flashes
   this printer's micro-controllers over the bootloader, with per-board
   build presets stored in `printer.cfg`. Backed by
   `deps/moonraker/moonraker/components/firmware_build.py` (REST endpoints)
   and a real panel component in `deps/mainsail`. See
   [firmware_flash.md](firmware_flash.md) for the full writeup (presets,
   safety notes, host setup). Add to `moonraker.conf`:
   ```
   [include ../../klipper/biokalico_extras/moonraker/moonraker_firmware_build.conf]
   ```
   (path is relative to `moonraker.conf`'s directory, typically
   `~/printer_data/config/`).

6. **Auto-reconnect for Mainsail's "Connection Lost" dialog.** Mainsail's
   own websocket client used to give up after only 2 failed reconnect
   attempts, leaving a dialog up with a "Try Again" button that needed a
   manual click - a Klippy restart or a brief network blip was enough to
   trigger that. Fixed directly in the Mainsail fork's connection-dialog/
   socket handling (auto-retry with backoff) rather than a script watching
   the dialog from outside.

7. **Multi-user conflict warning.** Mainsail lets multiple browsers connect
   to the same printer at once with no indication that anyone else is
   there, so two people can issue conflicting commands without realizing
   it. A real Vuex module + toolbar icon in the Mainsail fork gives each
   browser a UUID in `localStorage` (shared across tabs of the same
   browser, so a second tab never triggers this for itself) and, while that
   browser has recent mouse/keyboard input and its tab is visible and
   focused, posts a heartbeat timestamp to Moonraker's stock
   `/server/database/item` API under the `biokalico_presence` namespace.
   Every browser also polls that namespace and lights up a warning icon in
   the toolbar whenever another browser's last heartbeat is less than 5
   minutes old. Since a browser's heartbeat key is a UUID that persists in
   its `localStorage` forever, and never gets deleted client-side, a small
   `presence_prune` Moonraker component
   (`deps/moonraker/moonraker/components/presence_prune.py`) wakes up
   hourly and drops any heartbeat older than 3 days so the namespace
   doesn't grow without bound. Add to `moonraker.conf`:
   ```
   [include ../../klipper/biokalico_extras/moonraker/moonraker_presence_prune.conf]
   ```
   (path is relative to `moonraker.conf`'s directory, typically
   `~/printer_data/config/`).

8. **Password-only login for Mainsail.** Mainsail asks for one shared
   password, and that is the only way to log in: Moonraker's per-user
   accounts, account management endpoints and LDAP support are removed from
   the fork. `deps/moonraker/moonraker/components/simple_password_auth.py`
   registers the one user Mainsail logs in as, and the login screen is in
   the Mainsail fork (upstream Mainsail has no login screen; see
   mainsail-crew/mainsail#267 and #320). Its options (`password`,
   `local_bypass` and `hint`) are described under `[simple_password_auth]`
   in `deps/moonraker/docs/configuration.md`.

   `[simple_password_auth]` lives directly in `moonraker.conf`, not in an
   included snippet under `biokalico_extras/moonraker/` like this repo's
   other extras, because it holds a real secret and `biokalico_extras/` is
   inside this git repo. See `bio_config/moonraker.conf.example` for the
   section as shipped. If the password is blank, or the section is missing,
   a random password is generated on first start and written into
   `moonraker.conf`. See it with:
   ```
   grep password: ~/printer_data/config/moonraker.conf
   ```
   run on the printer host (or over SSH).

   `local_bypass` is on by default, so computers on the local network
   (`trusted_clients`) get in without the password. The Cloudflare check
   matters because QUICKSTART.md has people run
   `cloudflared tunnel --url http://localhost:80` on the printer host: a
   tunneled visitor reaches Moonraker over loopback, which by IP alone looks
   like someone on the local network. Requests with Cloudflare's headers
   always need the password.

   One-click login links are supported using a URL fragment. Append the
   password after `#password=`:
   ```
   https://printer.example.com/#password=the-shared-password
   ```
   URL fragments are not included in HTTP requests or referrer headers.
   Mainsail removes the fragment from the address bar before auto-submitting
   it, which also keeps it out of later reloads and copied URLs. An older
   `?password=` link still works, but prefer the fragment form: the query
   string is part of the request, so it can be kept in browser history and in
   proxy or CDN logs.

   Sessions use Moonraker's normal JWT lifetimes: an access token lasts an
   hour, and Mainsail silently refreshes it with a refresh token that lasts
   `[authorization] login_timeout` days (default 90), so a browser only
   asks for the password again after that long or after an explicit logout.
   Restarting Moonraker doesn't log anyone out, but changing `password` and
   restarting does. API key clients (crowsnest, other API clients) are not
   affected by the password. This includes this repo's own host-side scripts
   (`printer-services.sh`, `monitoring.sh`, `reset-ftdi-hub.sh` and the
   firmware flasher), which read the key from Moonraker's database with
   `scripts/moonraker_api.py`.

9. **"Restart All" button.** A menu item in Mainsail's top-right power menu,
   above the "Klipper Control" section, that restarts every host-side
   service the printer depends on (klipper, moonraker, crowsnest, nginx, the
   SLA image-display service) plus the MCU firmware, via
   `scripts/printer-services.sh --restart`, and streams that script's output
   into a log viewer dialog. Backed by
   `deps/moonraker/moonraker/components/printer_services.py`, which launches
   the script as its own transient systemd unit (`sudo systemd-run`) rather
   than as a normal Moonraker child process - moonraker restarting itself is
   one of the steps, and a child process would be killed along with it
   before it reached the later services. Progress is read back from
   `printer_data/logs/restart_all.log` (a normal Moonraker "logs" file
   download), not tracked in Moonraker's memory, so the log viewer keeps
   working across the moonraker outage in the middle of the job. Add to
   `moonraker.conf`:
   ```
   [include ../../klipper/biokalico_extras/moonraker/moonraker_printer_services.conf]
   ```
   (path is relative to `moonraker.conf`'s directory, typically
   `~/printer_data/config/`). Requires the same passwordless `sudo` access
   `printer-services.sh` already assumes for restarting services directly.

10. **Per-print timelapse.** One frame captured per layer (via the
    `TIMELAPSE_TAKE_FRAME` macro, called from a slicer's per-layer custom
    G-code) and rendered to an H.264 video after each print, backed by
    `deps/moonraker/moonraker/components/timelapse.py` and
    [printer/timelapse_macros.cfg](printer/timelapse_macros.cfg) (already
    picked up by the glob-include in item 1, no separate `printer.cfg`
    change needed). Off by default - see
    [moonraker/moonraker_timelapse.conf](moonraker/moonraker_timelapse.conf)
    - since per-layer capture plus a render on every print is a cost not
    every host should pay automatically. Once enabled (either there or from
    Mainsail's Timelapse Settings tab), Mainsail's stock Timelapse page
    talks to this component directly; nothing on the Mainsail side needed
    writing, since that page and its `machine.timelapse.*` API contract are
    upstream Mainsail features this component implements the backend of.

    Per-layer frames land in their own per-print directory under
    `~/printer_data/timelapse/tmp/` and are the actual troubleshooting
    artifact, not just encoder input - they're kept, individually openable,
    for the whole print, and are only deleted once a render of that exact
    print's frames has succeeded. If Klipper, Moonraker, or the host
    crashes or loses power mid-print, those frames aren't lost: the next
    time Moonraker starts, it reconciles `tmp/` against whatever print is
    (or isn't) actually still running and renders any leftover directory
    that isn't the live print as `<job>-recovered.mp4`, with no manual step
    required. Add to `moonraker.conf`:
    ```
    [include ../../klipper/biokalico_extras/moonraker/moonraker_timelapse.conf]
    ```
    (path is relative to `moonraker.conf`'s directory, typically
    `~/printer_data/config/`).

11. **Automatic recovery.** When Klipper shuts down on its own while the
    printer is idle, Moonraker runs the same restart sequence as the "Restart
    All" button (item 9), so an MCU communication fault no longer waits for
    someone to run `FIRMWARE_RESTART`. It tries at most
    `mcu_recovery_max_attempts` times per incident (default 2), then stops
    and says a person is needed. It never acts during a print, including a
    paused one, and never undoes a deliberate stop (`M112` or the emergency
    stop button). Every action is announced through the
    `auto_recovery_action` event: add it to a `[notifier ...]`'s `events:`
    line to get it as an alert, otherwise it only appears in
    `moonraker.log`. Backed by
    `deps/moonraker/moonraker/components/auto_recovery.py` and the include
    already present in `bio_config/moonraker.conf.example`:
    ```
    [include ../../klipper/biokalico_extras/moonraker/moonraker_auto_recovery.conf]
    ```
    A second mode, off until you set it up, watches the SLA projector's
    serial link (`projector_link_error` on the `image_display` object). When
    the link drops it switches the adapter's USB port off and on through
    sysfs, once, and can then power-cycle the whole USB hub if you give it
    `projector_usb_hub_path`. The hub power-cycle resets everything on that
    hub, including any Klipper MCU, so use it only for a hub that carries
    nothing else you need. The USB paths differ on every machine, so set
    them in a `[auto_recovery]` section of your own `moonraker.conf` after
    the include. Every option, and how to find the paths, is described
    under `[auto_recovery]` in `deps/moonraker/docs/configuration.md`.

Items 3-11 above are now real, tracked, committed source in BioKalico's own
Mainsail/Moonraker forks (`deps/mainsail`, `deps/moonraker`, vendored as git
submodules - see the top-level README for the submodule/build/CI setup). All
are built and tested like any other first-party
feature of those projects, rather than files copied or `sed`-injected into
someone else's build output at deploy time. Pulling in upstream
Mainsail/Moonraker fixes means rebasing those forks' branches, then bumping
the submodule pointer commit here - same workflow as any other submodule
update.

## Applying Changes During Development

After editing source in place on the host, apply it with the smallest step
that matches what you changed:

- **Mainsail (`deps/mainsail`) source:** rebuild the bundle (nginx serves
  `dist/` directly, no hot reload):
  ```
  cd ~/klipper/deps/mainsail && npm run build
  ```
  No hard refresh needed. Open dashboards poll for a new build every ~60s and
  self-update: they reload silently if the printer is currently disconnected
  (the reload rides along with the reconnect), otherwise a plain, dismissible
  `confirm()` asks to reload now - Cancel just defers it to the next
  disconnect or a manual reload, it never nags again (see
  `src/components/TheServiceWorker.vue`). For fast UI iteration, `npm run
  serve` runs a hot-reload dev server on port 8080 (separate from the
  nginx-served production site, same Moonraker).
- **A Klipper extra (`klippy/extras/*.py`):** a full service restart, so the
  edited Python is re-imported:
  ```
  sudo systemctl restart klipper
  ```
  `RESTART` / `FIRMWARE_RESTART` (and Moonraker's `/printer/restart`) rebuild
  the printer object **in the same process and do NOT re-import edited
  Python** - the old module stays in memory. A tell-tale symptom of using the
  wrong one after adding a config option alongside code is a startup error
  like `Option 'x' is not valid in section '...'` (the new config is re-read,
  but the old code never reads the new option).
- **A Moonraker component (`deps/moonraker/moonraker/components/*.py`) or
  `moonraker.conf`:** restart the Moonraker service (Klipper's restart does
  not touch it):
  ```
  sudo systemctl restart moonraker
  ```
- **Only a `.cfg` change** (macros, `printer.cfg`, `biokalico_extras/printer/*.cfg`),
  no `.py`: a config reload is enough and faster:
  ```
  curl -X POST http://127.0.0.1:7125/printer/restart
  ```
- **Everything at once** (what `biokalico-installer.sh update` does under the
  hood, incl. an `npm ci && npm run build`): rebuild Mainsail and restart both
  services:
  ```
  cd ~/klipper/deps/mainsail && npm run build && sudo systemctl restart klipper moonraker
  ```

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
