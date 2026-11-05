# Klipper Macros for BioSlicer G-code

The macros a printer must provide to run the G-code BioSlicer emits for SLA
materials, and what each one has to do. The G-code itself (file layout,
header comments, the per-layer command sequence and the toolchange
placeholders) is documented with the slicer, in
[Hybrid_FDM_SLA_Video_Materials.md](https://github.com/nicholas-karr/BioSlicer/blob/master/doc/Hybrid_FDM_SLA_Video_Materials.md).

## `SLA_LOAD_GCODE_VIDEOS`

Called once after start G-code. Must:

1. Locate and open the currently-printing G-code file
2. Scan sequentially for `; bioslicer_sla_video begin` lines
3. For each block found, collect all `; bioslicer_sla_video <base64>` lines
   until `; bioslicer_sla_video end`
4. Strip the `; bioslicer_sla_video ` prefix from each data line, concatenate,
   and base64-decode to recover the MKV binary
5. Register the decoded MKV in memory as video `1`, `2`, ... (in file order)
6. Optionally verify decoded byte count against `bytes=<N>` and SHA-256 against
   `sha256=<hex>` from the `begin` line

The `name=` field of the header comments is not used for this: only the
order of the blocks in the file is. The macro must complete before any
`SLA_SHOW_FRAME` is called.

## `SLA_SHOW_FRAME NAME=<n> FRAME=<f>`

Called once per SLA channel per layer it has geometry on, directly by the
slicer or through `BIOSLICER_SLA_EXPOSE`. Must:

1. Retrieve video `<n>` from the loaded set
2. Seek to frame `<f>` in that video
3. Display the frame on the projector (white = expose, black = block)

## `BIOSLICER_SLA_EXPOSE EXTRUDER=<i> NAME=<n> FRAME=<f>`

Emitted instead of `SLA_SHOW_FRAME` with the Klipper G-code flavor. Applies
the delay before exposure, shows the frame, then applies the exposure time
and the delay after it for extruder `<i>` (0-based), from the sliced values
or their `BIOSLICER_OVERRIDE` overrides.

## `BIOSLICER_DROPLET EXTRUDER=<i> X=<mm> Y=<mm> Z=<mm> SAFE_Z=<mm> VOLUME_NL=<nL> COUNT=<n>`

Emitted once per droplet for a droplet channel (`channel_type` `droplet`),
after the SLA exposures of the layer the droplet lands on. Defined in
`printer/droplet_macros.cfg`: it rises to `SAFE_Z`, moves over the point,
lowers to `Z`, waits the settle time, and fires `COUNT` pulses of
`VOLUME_NL / droplet_nl_per_us` microseconds at `droplet_frequency` through
the `[pulse_pin]` named in the macro's `pin` variable. The three parameters
come from `BIOSLICER_PROCESS_PARAMS` (keyed by extruder like the SLA times)
or their `BIOSLICER_OVERRIDE` overrides. The slicer already rejects pulses
shorter than 100 us and more than 10% duty, the limits of `pulse_pin`; the
macro checks them again. The material of the droplet channel is selected
beforehand by `SELECT_MATERIAL`, so a droplet material needs an entry in
`materials.cfg` with `params_channel_type: droplet`.

## `SLA_CLEAR`

Must black out the projector immediately. Called as the first command of
every SLA channel's sequence on every layer, and once more after the last
exposure of the layer. Should be near-instantaneous.

## `SELECT_MATERIAL MATERIAL=<material>`

Called by the BioTrident profile's `toolchange_gcode`, for an extruder's
material at a tool change and for an SLA material before its exposure when
the SLA material changes. `<material>` is the material name described under
[Toolchange Placeholders](https://github.com/nicholas-karr/BioSlicer/blob/master/doc/Hybrid_FDM_SLA_Video_Materials.md#toolchange-placeholders). For an SLA
material the `[material_selector sla]` of the machine config runs
`BIOSLICER_MATERIAL_SWAP CHANNEL=<slot - 1>` when the slot holding it is not
the active one.

## `BIOSLICER_MATERIAL_SWAP CHANNEL=<index>`

Brings the vat of channel `<index>` (0-based slot of the `sla` material
group) into place. Must:

1. Move any FFF toolheads clear of the optical path if applicable
2. Reposition the resin bowl / select the correct resin reservoir
3. Return when the hardware is ready for exposure

It is normally reached through `SELECT_MATERIAL`. A slicer profile that
leaves `toolchange_omit_tool_command` off calls it directly, as
`BIOSLICER_MATERIAL_SWAP MATERIAL=<video name>` (see
[Material selection](https://github.com/nicholas-karr/BioSlicer/blob/master/doc/Hybrid_FDM_SLA_Video_Materials.md#material-selection)); the machine configs'
macros read only `CHANNEL` and would treat that as channel 0.

## `PRINT_START MATERIALS=<comma-separated-list>`

Called at print start with all materials used in the job. It runs
`TOOLHEADS_PREFLIGHT` to stop before anything moves if one of them cannot be
had.

## `SLA_LOAD_LOCAL_VIDEO NAME=<name> PATH=<path>`

Loads an MKV from `<path>` on the printer's local filesystem and registers it
as `<name>`. Not emitted by the slicer: referenced videos (`embedded=0`) are
loaded by `SLA_LOAD_GCODE_VIDEOS`, which also gives them the position number
the exposure commands use. Available for manual use.

## `SLA_CMD CMD=<cmd>` (optional)

Raw command passthrough to the SLA image display server. Not emitted by the
slicer automatically; available for use in custom G-code fields.

## `SLA_SET_H_OFFSET OFFSET=<pixels>` (optional)

Horizontal image offset. Not emitted automatically by the slicer.
