# Vendored files

`bed_thermal_adjust.py`, `manual_rail.py`, `multi_fan.py`, `rounded_path.py`,
`tool.py`, `toolchanger.py`, `tool_probe.py`, `tool_probe_endstop.py`, and
`tools_calibrate.py` are copied in from
[viesturz/klipper-toolchanger](https://github.com/viesturz/klipper-toolchanger)
(GPLv3) rather than symlinked from a separate checkout. They're permanent,
plain tracked files here. Pulling in upstream fixes means manually
re-copying the relevant file(s) from that repo, not an automatic update.

`toolchanger.py` carries one addition that upstream does not have,
`Toolchanger.set_active_tool()`, which `toolheads.py` calls when a toolhead is
mounted or removed by hand. Keep it when re-copying.

These files are formatted with `ruff format` and pass `ruff check` (see
`pyproject.toml`), so this repo's lint CI accepts them, which means they do
not diff cleanly against upstream. `tool.py` also registers its T-command with
a named function instead of an assigned lambda (`register_t_gcode`) for the
same reason. After re-copying a file from upstream, run `ruff format` and
`ruff check --fix` on it.

`tools_calibrate.py` replaces the version of that file that Kalico itself
ships. It adds the `TOOL_CALIBRATE_PROBE_OFFSET` command, the `probe` config
option, and the `last_probe_offset`, `last_x_result`, `last_y_result` and
`last_z_result` status fields. It no longer reports `sensor_location`. The
G-code, config and status references in `docs/` describe this version.
