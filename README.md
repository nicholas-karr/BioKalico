# BioKalico

BioKalico is a fork of [Kalico](https://github.com/KalicoCrew/kalico) (itself
a community-maintained fork of [Klipper](https://github.com/Klipper3d/klipper))
for hybrid FFF/SLA bioprinting: multi-material toolchanging, projector-driven
resin exposure, and syringe/paste extrusion, on top of the FFF printer
firmware every Klipper install starts with.

Klipper splits the printing job between a regular computer (usually a
Raspberry Pi, running "Klippy", the Python host software) and the printer's
own micro-controller. The host handles trajectory planning and math in
software; the micro-controller just fires precisely-timed step pulses, so
even a cheap MCU can drive motion accurately.

Kalico maintains features upstream Klipper is too conservative to merge:
features that could damage a printer or its surroundings if misconfigured.

## Quick Start

New to this? [QUICKSTART.md](QUICKSTART.md) walks through everything from
a blank Raspberry Pi (or laptop) to a running, remotely-accessible printer.
No prior Linux or SSH experience assumed.

## Developing

Editing source on the host? See [Applying Changes During
Development](biokalico_extras/README.md#applying-changes-during-development)
for which service to restart (or rebuild) for a given change - Mainsail
source, a Klipper extra, a Moonraker component, or just a `.cfg` file.

## Documentation

- [biokalico_extras/README.md](biokalico_extras/README.md): host setup with
  `scripts/biokalico-installer.sh`, and how the Moonraker/Mainsail features
  (projector power buttons, `$HOME` config root, firmware build panel,
  shared-password login) built into BioKalico's own forks are wired up via
  the `.conf` fragments in `biokalico_extras/`.
- [scripts/sla/README.md](scripts/sla/README.md): the TCP display server
  that drives the projector: setup, systemd service, and the G-code command
  reference (`SLA_SHOW_FRAME`, `SLA_LOAD_GCODE_VIDEOS`, etc.).
- [klippy/extras/VENDORED.md](klippy/extras/VENDORED.md): the
  `toolchanger`/`tool`/`tool_probe` extras vendored in from
  [klipper-toolchanger](https://github.com/viesturz/klipper-toolchanger).
- [bio_config/](bio_config/): `printer.cfg` templates for BioTrident and
  Printess.
- Firmware build & flash panel: see below.

## Vendored submodules

`deps/mainsail` and `deps/moonraker` are BioKalico's own forks
([nicholas-karr/mainsail](https://github.com/nicholas-karr/mainsail),
[nicholas-karr/moonraker](https://github.com/nicholas-karr/moonraker)) - see
[biokalico_extras/README.md](biokalico_extras/README.md) for what each
feature does. `deps/crowsnest` is unmodified upstream
([mainsail-crew/crowsnest](https://github.com/mainsail-crew/crowsnest),
branch `v5`).

`scripts/biokalico-installer.sh` builds Mainsail from source on-host and runs
Moonraker's/Crowsnest's own installers against their submodule checkouts -
see that script for the exact flow. Pulling in upstream Mainsail/Moonraker
fixes means rebasing those forks' branches, then bumping the submodule
pointer commit here, same as any other submodule update.

## Printer service management

`~/klipper/scripts/printer-services.sh` restarts every host-side service this
printer stack depends on (klipper, moonraker, crowsnest, nginx/mainsail, and
the SLA image-display server) plus the MCU firmware itself, and/or tails all
of their logs together in one interleaved stream:

```bash
~/klipper/scripts/printer-services.sh --restart --logs
```

Both flags default to false; run with no arguments (or `-h`/`--help`) to see
full usage.

## Firmware build & flash panel

A "Firmware" card in Mainsail's Settings page builds and flashes this
printer's micro-controllers over the bootloader, no button presses needed,
using per-board build presets stored in `printer.cfg`. See
[biokalico_extras/firmware_flash.md](biokalico_extras/firmware_flash.md).

---

## Inherited Klipper/Kalico features

BioKalico tracks Kalico's `main` branch, so it carries everything Kalico
adds on top of upstream Klipper. See the [Kalico Additions
document](https://docs.kalico.gg/Kalico_Additions.html) for Kalico's own
full list; here's what that means concretely for this fork:

- [core: no Python2 tests; no PRU boards](https://github.com/KalicoCrew/kalico/pull/39)

- [core: git-untracked folder, plugins for user-plugins](https://github.com/KalicoCrew/kalico/pull/82)

- [core: danger_options](https://github.com/KalicoCrew/kalico/pull/67)

- [core: rotate log file at every restart](https://github.com/KalicoCrew/kalico/pull/181)

- [core: options for API server socket file mode, user, and group](https://github.com/KalicoCrew/kalico/pull/612)

- [core: options to change mode and group of linux mcu psuedoterminal](https://github.com/KalicoCrew/kalico/pull/692)

- [fan: normalising Fan PWM power](https://github.com/KalicoCrew/kalico/pull/44) ([klipper#6307](https://github.com/Klipper3d/klipper/pull/6307))

- [fan: reverse FAN](https://github.com/KalicoCrew/kalico/pull/51) ([klipper#4983](https://github.com/Klipper3d/klipper/pull/4983))

- [heaters: modify PID without reload](https://github.com/KalicoCrew/kalico/pull/35)

- [heaters: MPC temperature control](https://github.com/KalicoCrew/kalico/pull/333)

- [heaters: velocity PID](https://github.com/KalicoCrew/kalico/pull/47) ([klipper#6272](https://github.com/Klipper3d/klipper/pull/6272))

- [heaters: PID-Profiles](https://github.com/KalicoCrew/kalico/pull/162)

- [heaters: expose heater thermistor out of min/max](https://github.com/KalicoCrew/kalico/pull/182)

- [heaters: dual loop pid control](https://github.com/KalicoCrew/kalico/pull/735)

- [heaters/fan: new heated_fan module](https://github.com/KalicoCrew/kalico/pull/259)

- [gcode: jinja2.ext.do extension](https://github.com/KalicoCrew/kalico/pull/26) ([klipper#5149](https://github.com/Klipper3d/klipper/pull/5149))

- [gcode: gcode_shell_command](https://github.com/KalicoCrew/kalico/pull/71) ([klipper#2173](https://github.com/Klipper3d/klipper/pull/2173) / [kiuah](https://github.com/dw-0/kiauh/blob/master/resources/gcode_shell_command.py) )

- [gcode: expose math functions to gcode macros](https://github.com/KalicoCrew/kalico/pull/173) ([klipper#4072](https://github.com/Klipper3d/klipper/pull/4072))

- [gcode: HEATER_INTERRUPT gcode command](https://github.com/KalicoCrew/kalico/pull/94)

- [gcode: RELOAD_GCODE_MACROS command](https://github.com/KalicoCrew/kalico/pull/305)

- [probe: dockable Probe](https://github.com/KalicoCrew/kalico/pull/43) ([klipper#4328](https://github.com/Klipper3d/klipper/pull/4328))

- [probe: drop the first result](https://github.com/KalicoCrew/kalico/pull/2) ([klipper#3397](https://github.com/Klipper3d/klipper/issues/3397))

- [probe: z_calibration](https://github.com/KalicoCrew/kalico/pull/31) ([klipper#4614](https://github.com/Klipper3d/klipper/pull/4614) / [protoloft/z_calibration](https://github.com/protoloft/klipper_z_calibration))

- [z_tilt: z-tilt calibration](https://github.com/KalicoCrew/kalico/pull/105) ([klipper3d#4083](https://github.com/Klipper3d/klipper/pull/4083) / [dk/ztilt_calibration](https://github.com/KalicoCrew/kalico/pull/54))

- [stepper: home_current](https://github.com/KalicoCrew/kalico/pull/65)

- [stepper: current_change_dwell_time](https://github.com/KalicoCrew/kalico/pull/90)

- [homing: post-home retract](https://github.com/KalicoCrew/kalico/pull/65)

- [homing: sensorless minimum home distance](https://github.com/KalicoCrew/kalico/pull/65)

- [homing: min_home_dist](https://github.com/KalicoCrew/kalico/pull/90)

- [virtual_sdcard: scanning of subdirectories](https://github.com/KalicoCrew/kalico/pull/68) ([klipper#6327](https://github.com/Klipper3d/klipper/pull/6327))

- [retraction: z_hop while retracting](https://github.com/KalicoCrew/kalico/pull/83) ([klipper#6311](https://github.com/Klipper3d/klipper/pull/6311))

- [danger_options: allow plugins to override conflicting extras](https://github.com/KalicoCrew/kalico/pull/82)

- [danger_options: expose the multi mcu homing timeout as a parameter](https://github.com/KalicoCrew/kalico/pull/93)

- [danger_options: option to configure the homing elapsed distance tolerance](https://github.com/KalicoCrew/kalico/pull/110)

- [danger_options: option to ignore ADC out of range](https://github.com/KalicoCrew/kalico/pull/129)

- [temperature_mcu: add reference_voltage](https://github.com/KalicoCrew/kalico/pull/99) ([klipper#5713](https://github.com/Klipper3d/klipper/pull/5713))

- [adxl345: improve ACCELEROMETER_QUERY command](https://github.com/KalicoCrew/kalico/pull/124)

- [extruder: add flag to use the PA constant from a trapq move vs a cached value](https://github.com/KalicoCrew/kalico/pull/132)

- [force_move: turn on by default](https://github.com/KalicoCrew/kalico/pull/135)

- [resonance_tester: warn about active fans during input shaper calibration](https://github.com/KalicoCrew/kalico/pull/627)

- [bed_mesh: add BED_MESH_CHECK command for mesh validation](https://github.com/KalicoCrew/kalico/pull/629)

- [respond: turn on by default](https://github.com/KalicoCrew/kalico/pull/296)

- [exclude_object: turn on by default](https://github.com/KalicoCrew/kalico/pull/306)

- [bed_mesh: add bed_mesh_default config option](https://github.com/KalicoCrew/kalico/pull/143)

- [config: CONFIG_SAVE updates included files](https://github.com/KalicoCrew/kalico/pull/153)

- [kinematics: independent X&Y accel/velocity for corexy and cartesian](https://github.com/KalicoCrew/kalico/pull/4)

- [kinematics: independent X&Y accel/velocity for corexz](https://github.com/KalicoCrew/kalico/pull/267)

- [idle_timeout: allow the idle timeout to be disabled](https://github.com/KalicoCrew/kalico/issues/165)

- [canbus: custom CAN bus uuid hash for deterministic uuids](https://github.com/KalicoCrew/kalico/pull/156)

- [filament_switch|motion_sensor:  runout distance, smart and runout gcode](https://github.com/KalicoCrew/kalico/pull/158)

- [z_tilt|qgl: custom threshold for probe_points_increasing check](https://github.com/KalicoCrew/kalico/pull/189)

- [save_config: save without restarting the firmware](https://github.com/KalicoCrew/kalico/pull/191)

- [configfile: recursive globs](https://github.com/KalicoCrew/kalico/pull/200) / ([klipper#6375](https://github.com/Klipper3d/klipper/pull/6375))

- [temperature_fan: curve control algorithm](https://github.com/KalicoCrew/kalico/pull/193)

- [shaper_calibrate: store and expose accel_per_hz](https://github.com/KalicoCrew/kalico/pull/224)

- [resonance_tester: accepts ACCEL_PER_HZ in TEST_RESONANCES](https://github.com/KalicoCrew/kalico/pull/312)

- [mcu: support for AT32F403](https://github.com/KalicoCrew/kalico/pull/284)

- [z_tilt, quad_gantry_level: adaptive horizontal move z](https://github.com/KalicoCrew/kalico/pull/336)

- [core: non-critical-mcus](https://github.com/KalicoCrew/kalico/pull/339)

- [gcode_macros: !python templates](https://github.com/KalicoCrew/kalico/pull/360)

- [gcode_macros: !!include macros/my_macro.py](https://github.com/KalicoCrew/kalico/pull/578)

- [core: action_log](https://github.com/KalicoCrew/kalico/pull/367)

- [danger_options: configurable homing constants](https://github.com/KalicoCrew/kalico/pull/378)

- [tmc2240: adjustable driver_CS and current_range](https://github.com/KalicoCrew/kalico/pull/556)

- [extruder: cold_extrude](https://github.com/KalicoCrew/kalico/pull/750)

This project is licensed under the [GPLv3 license](COPYING).
