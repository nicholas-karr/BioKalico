# Firmware Build & Flash Panel

A "Firmware" card in Mainsail's Settings page that builds and flashes this
printer's micro-controllers from the browser, using per-board build presets
stored in `printer.cfg`. Flashing goes over the Katapult bootloader - no 
button presses.

## Layout

- `klippy/extras/firmware_build.py` - declares `[firmware_build <name>]`
  printer.cfg sections. Declarative only, no runtime behavior.
- `biokalico_extras/firmware_presets/*.config` - per-board Kconfig presets,
  referenced by name from `printer.cfg`.
- `scripts/firmware/build_and_flash.py` - the build/flash driver, invoked by
  the Moonraker component.
- `scripts/firmware/minimize_config.py` - reduces a full `.config` to a
  minimal preset.
- `biokalico_extras/moonraker/firmware_build.py` +
  `moonraker_firmware_build.conf` - Moonraker component exposing REST
  endpoints, deployed the same "untracked drop-in" way as `home_root.py`
  (see `biokalico_extras/README.md`).
- `biokalico_extras/mainsail/firmware-panel.js` - the injected Settings card.

## `printer.cfg` syntax

```ini
[firmware_build main]
preset: stm32h743_mainboard
mcu: mcu
device: /dev/serial/by-id/usb-Klipper_stm32h743xx_2A0052000151323236333534-if00

[firmware_build nhk]
preset: rp2040_nitehawk_sb
mcu: nhk
device: /dev/serial/by-id/usb-Klipper_rp2040_3232323236195ADE-if00
overrides:
    CONFIG_WANT_LDC1612=n
```

- `preset` (required): name of a `.config` file under
  `biokalico_extras/firmware_presets/`.
- `device` (required): a **stable** device path (`/dev/serial/by-id/...`),
  not a bare `/dev/ttyACM0`-style path.
- `mcu` (optional, defaults to the section name suffix): the `[mcu ...]`
  status object to read `mcu_version` from, for mismatch detection.
- `overrides` (optional, multi-line): extra `CONFIG_X=y`/`CONFIG_X=n` lines
  applied on top of `preset`, same multi-line syntax as a gcode macro's
  `gcode:` block. Appended after the preset before `olddefconfig` runs;
  Kconfig takes the last value seen for a symbol set more than once.
  Included in the mismatch-detection fingerprint.

  To skip a shared preset entirely, point `preset` at a preset file of your
  own instead - presets are plain files with no registry, so this needs no
  code changes.

`klippy/extras/firmware_build.py` exists only so Klipper's config parser
accepts these sections; the actual tooling
(`scripts/firmware/build_and_flash.py`) parses `printer.cfg` directly and
independently, so it works even when Klipper won't start.

## Presets

Presets are minimal Kconfig diffs (`make savedefconfig` format) - only
symbols that differ from Kconfig's own defaults, stored as
`biokalico_extras/firmware_presets/<name>.config`.

### Step-by-step: create or update a preset

1. Open a terminal on the printer host and go to the klipper checkout:
   ```bash
   cd ~/klipper
   ```
2. Launch the config menu, telling it to save to a scratch file (so it
   doesn't touch the repo's own root `.config`):
   ```bash
   KCONFIG_CONFIG=/tmp/full.config make menuconfig
   ```
   This opens a curses TUI. Pick the board/chip first ("Micro-controller
   Architecture") - that determines every other option that becomes
   visible. Keys:

   | Key | Action |
   |---|---|
   | Up/Down, or J/K | Move selection |
   | Space or Enter | Toggle a value / enter a submenu |
   | `?` | Show help text for the highlighted option |
   | `/` | Search by name or description, jump to a result |
   | Esc | Back out one menu level |
   | Q | Quit - asks "save your new configuration?", answer yes |

3. (Optional) Check what you just saved:
   ```bash
   cat /tmp/full.config
   ```
   This is the full, expanded config - hundreds of lines, most of them
   Kconfig defaults you didn't touch. That's expected; the next step strips
   those out.
4. Reduce it to a preset. `minimize_config.py` takes exactly two arguments -
   the full config you just made, and where to write the resulting preset:
   ```bash
   python3 scripts/firmware/minimize_config.py /tmp/full.config \
       biokalico_extras/firmware_presets/my_board_name.config
   ```
   Pick `my_board_name` to describe the board (e.g. `stm32h743_mainboard`) -
   this is the exact name you'll reference later as `preset:` in
   `printer.cfg`.
5. Check the result:
   ```bash
   cat biokalico_extras/firmware_presets/my_board_name.config
   ```
   This should be short (a handful of lines) - just the options you
   deliberately chose.

`make olddefconfig` (run automatically by the build driver before every
build) expands a minimal preset back to a full `.config` at build time - you
never need to do that expansion by hand.

A one-off tweak to an existing preset, without creating a new file, doesn't
need this whole flow - just copy the `CONFIG_X=y`/`CONFIG_X=n` line(s) you
want from `/tmp/full.config` into an `overrides:` block in `printer.cfg`
(see above).

### Current presets

| Preset | Board | Bootloader | Notes |
|---|---|---|---|
| `stm32h743_mainboard` | LDO Leviathan V1.3 (this printer's `[mcu]`) | Katapult, 128KiB offset | `printer.cfg`'s header comment says "V1.1"; that's stale, the chip is H743 (V1.3). |
| `rp2040_nitehawk_sb` | LDO Nitehawk SB (`[mcu nhk]`) | Katapult, 16KiB offset | Wrong offset erases Katapult - confirmed against LDO's docs. |
| `stm32f405_toolhead` | Example toolchanger toolhead

Getting a bootloader offset wrong either overwrites the bootloader or
prevents the app from booting. To verify a board's bootloader before
enabling a target for it: stop `klipper` (so it's not holding the port),
request bootloader entry
(`python3 -c 'import sys; sys.path.insert(0, "scripts"); import flash_usb as u; u.enter_bootloader("<device>")'`),
then check `lsusb` - `1d50:6177` is Katapult, `0483:df11` is raw STM32 DFU,
`2e8a:0003` is raw RP2040 bootloader. Restart `klipper` after.

## Build folders, parallel builds, ccache

Each target gets `firmware_builds/<name>/{.config,out/}` (gitignored), using
the `Makefile`'s existing `OUT=`/`KCONFIG_CONFIG=` overrides. All targets
share one ccache directory via `CC="ccache arm-none-eabi-gcc"`.

Builds run in parallel across targets. Flashing is always strictly
sequential, one target at a time - bootloader re-enumeration needs exclusive
USB access.

The repo-root `.config`/`out/` are untouched, and remain available for ad
hoc `make`/`menuconfig` use.

## Safety

- Flashing only happens through `build_and_flash` (build, then flash) -
  there's no standalone "flash the last build" action, so a stale binary can
  never get flashed without also being rebuilt.
- The Moonraker component refuses to start a `build_and_flash` job while
  Klipper reports an active print (`print_stats.state` in
  `printing`/`paused`).
- The driver wraps stop/flash/start in `try`/`finally` with `SIGINT`/
  `SIGTERM` handlers, so a Ctrl-C, crash, or Cancel click mid-flash always
  restarts the `klipper` service - worst case is a failed flash to retry,
  never a service left down.

## Build/flash mismatch detection

`GET /server/firmware/targets` compares what's on the MCU against what the
current checkout would build:

1. **MCU connected**: compares the firmware's embedded `mcu_version` (from
   `scripts/buildcommands.py`) as a prefix against the checkout's current
   `git describe --always --tags --long --dirty` (prefix, not exact match,
   since dirty builds embed a timestamp/hostname suffix). An unparseable
   `?`/`?-...` version always counts as a mismatch.
2. **MCU disconnected, flashed before by this panel**: falls back to
   `firmware_builds/<name>/last_flashed.json` (git commit + config hash +
   timestamp) vs. current HEAD.
3. **MCU disconnected, never flashed by this panel**: no signal.

## Moonraker endpoints

All under `/server/firmware/`:

| Endpoint | Method | Body | Notes |
|---|---|---|---|
| `targets` | GET | - | Configured targets + preset/mcu/device + mismatch status |
| `build` | POST | `{"targets": [...] \| "all"}` | Build only, targets run in parallel |
| `build_and_flash` | POST | `{"targets": [...] \| "all"}` | Build (parallel) then flash (sequential) - the only action that writes firmware |
| `status` | GET | - | Current job state + buffered per-target log tail |
| `cancel` | POST | - | SIGTERM the running driver process |

A single job lock spans an entire request (build-only or build-and-flash);
a second concurrent request is rejected.
