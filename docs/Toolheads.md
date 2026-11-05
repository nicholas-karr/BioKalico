# Toolheads

This document describes how to set up a printer that has more than one
toolhead, or that switches material in some other way, such as an SLA vat
table. The `[toolheads]` module keeps track of where each toolhead is and
which materials it holds, and gets the right material into place when a
print asks for it.

To enable this feature add a
[toolheads config section](Config_Reference.md#toolheads). Also see the
[command reference](G-Codes.md#toolheads), the
[status reference](Status_Reference.md#toolheads) and the
[API endpoints](API_Server.md#toolheadsset_state).

## Toolhead states

Every toolhead is in one of four states:

- `disconnected`: unplugged from the printer.
- `unmounted`: plugged in, but set aside. It is neither on the carriage
  nor in a dock.
- `docked`: parked in one of the printer's docks.
- `mounted`: on the carriage, where it is used for printing.

At most one toolhead is mounted, and each dock holds at most one
toolhead. The states are saved to a file, so they survive a restart.

The printer has no way to see where a toolhead is. It relies on what it
has been told, so when a toolhead is moved by hand the change has to be
recorded. Do this on the Toolheads card of the Mainsail dashboard, or
with the [TOOLHEAD_SET_STATE](G-Codes.md#toolhead_set_state) command.
Changes are refused while a print is running.

## Basic configuration

Each toolhead needs a
[toolhead_unit](Config_Reference.md#toolhead_unit) section. To have it
hold materials, also give it a
[material_slots](Config_Reference.md#material_slots) group and name that
group in `material_group`:

```
[materials]

[material basic_pla]
display_name: Basic PLA

[toolheads]

[material_slots extruder_slots]
display_name: Extruder
count: 1

[toolhead_unit extruder]
display_name: Extruder
material_group: extruder_slots
initial_state: mounted

[material_slots syringe_slots]
display_name: Syringe
count: 1

[toolhead_unit syringe]
display_name: Syringe
material_group: syringe_slots
```

`initial_state` is only used the first time the printer starts with the
toolhead in its config. After that, the saved state is used.

Every slot starts empty. Set what a slot holds on the Toolheads card of
the Mainsail dashboard. It is saved to a file, so it survives a restart.

With this configuration every toolhead is changed by hand, as described
in the next section.

## Changing toolheads by hand

When a print (or a [SELECT_MATERIAL](G-Codes.md#select_material) or
[SELECT_TOOLHEAD](G-Codes.md#select_toolhead) command) needs a toolhead
that is not on the carriage, the printer stops and waits. It does not
move while it waits.

A dialog appears on every open Mainsail dashboard. It names the toolhead
to mount and, when another one is on the carriage, the toolhead to take
off first. Swap the toolheads, say where the removed one went, and
confirm. The print then continues.

The printer does not check the result. It trusts that the right toolhead
is on the carriage once the dialog is confirmed.

Cancelling the dialog fails the command, which ends a running print with
an error. While the dialog is open, the Cancel Print button in Mainsail
has no effect until the dialog is answered, so use the Cancel button of
the dialog instead.

## Changing toolheads automatically

A printer with docks can exchange toolheads on its own. Three things are
needed:

1. Set `dock_count` in the `[toolheads]` section to the number of docks.
2. Add a `[tool]` section for the toolhead, with a `pickup_gcode` and a
   `dropoff_gcode` that move the carriage to the dock and take the
   toolhead from it or leave it there.
3. Name that section in the `tool` option of the toolhead.

```
[toolheads]
dock_count: 2

[toolchanger]

[tool syringe_tool]
pickup_gcode:
    MY_DOCK_PICKUP DOCK={tool.dock}
dropoff_gcode:
    MY_DOCK_DROPOFF DOCK={tool.dock}

[toolhead_unit syringe]
tool: syringe_tool
material_group: syringe_slots
initial_state: docked
initial_dock: 1
```

`MY_DOCK_PICKUP` and `MY_DOCK_DROPOFF` stand for macros of your own. The
moves they make depend on how the docks of the printer are built. Both
templates can read `tool.dock`, the number of the dock involved (1 to
`dock_count`, or 0 when it is not known), so one pair of macros can serve
every dock.

The `[tool]` and `[toolchanger]` sections come from the
klipper-toolchanger project. Its
[documentation](https://github.com/viesturz/klipper-toolchanger/blob/main/toolchanger.md)
lists their other options, such as tool offsets and tool detection pins.

An exchange is automatic when all of the following are true. In any
other case the toolhead is changed by hand.

- The wanted toolhead is docked and has a `tool`.
- The mounted toolhead, if there is one, also has a `tool`.

During an exchange the `dropoff_gcode` of the mounted toolhead runs
first, then the `pickup_gcode` of the wanted one. The mounted toolhead
goes back to its own dock (the one it was picked up from, or its
`initial_dock`). If it has none, it is put in the first free dock, and
the exchange fails if every dock is taken.

Home the printer before an exchange. By default an exchange is refused
while an axis is not homed.

If an exchange fails, check where each toolhead ended up and record it.
If the printer then reports that the toolchanger is in an error state,
fix the cause and run `INITIALIZE_TOOLCHANGER` before the next exchange.

The Toolheads card shows a Mount button on a docked toolhead that has a
`tool`.

## A toolhead that cannot be homed

A toolhead that is too fragile to touch an endstop sets
`homeable: False`. While it is mounted, `G28` never moves anything.
Instead:

1. The axes are treated as homed in the middle of their travel, so the jog buttons
   in Mainsail work. Jog the toolhead to where the origin should be.
2. Run `TOOLHEAD_SET_ORIGIN`. That position becomes X0 Y0 Z0, or the
   values given with `X=`, `Y=` and `Z=`.

Until the origin is set, the Move to fields in Mainsail are disabled,
`G28` fails and a print does not start (`TOOLHEADS_PREFLIGHT` refuses).
After that, `G28` does nothing and says so, so a print start macro that
homes still works. Mounting or removing such a toolhead, turning the
motors off or restarting forget the origin, so set it again each time.

## Following a toolhead board

A toolhead with its own control board can be marked disconnected and
connected as the board is unplugged and plugged in. Set
`is_non_critical: True` in the [mcu](Config_Reference.md#mcu) section of
the board, and name that section in the `mcu` option of the toolhead:

```
[mcu syringe_board]
serial: /dev/serial/by-id/usb-Klipper_my_board-if00
is_non_critical: True

[toolhead_unit syringe]
mcu: syringe_board
```

Unplugging the board marks the toolhead disconnected. Plugging it back in
restores the state it had before. Such a toolhead cannot be marked
disconnected by hand, and cannot be placed while its board is unplugged.

A board with `is_non_critical` set no longer shuts the printer down when
it is unplugged. Do not set it for a board that runs a heater.

## Material selectors

Some materials are switched without changing toolheads. An SLA printer
with several vats on a turntable, for example, rotates the wanted vat
into place. Describe such a group with a
[material_selector](Config_Reference.md#material_selector) section. Its
`select_gcode` runs whenever a different slot of the group is needed:

```
[material_slots vats]
display_name: Vats
count: 4

[material_selector turntable]
material_group: vats
select_gcode:
    MY_VAT_SWAP CHANNEL={index}
```

`MY_VAT_SWAP` stands for a macro of your own. A slot group belongs to
either one toolhead or one selector, never both.

## Use during a print

Two commands connect a print to the toolheads.

[TOOLHEADS_PREFLIGHT](G-Codes.md#toolheads_preflight) checks that every
material of a print can be had, without moving anything. Run it at the
top of the start macro, so that a print with a missing material stops
before anything moves:

```
[gcode_macro PRINT_START]
gcode:
    TOOLHEADS_PREFLIGHT MATERIALS={params.MATERIALS|default('')}
    G28
```

[SELECT_MATERIAL](G-Codes.md#select_material) gets one material into
place. Have the slicer run it on every tool change. The setup for
BioSlicer is under
[Toolchange Placeholders](https://github.com/nicholas-karr/BioSlicer/blob/master/doc/Hybrid_FDM_SLA_Video_Materials.md#toolchange-placeholders)
in the BioSlicer docs.

Both commands accept either the material id or the name the slicer uses
for the material (`params_slicer_token` in the
[material](Config_Reference.md#material) section).

When more than one place holds the material, the one that is easiest to
reach is used, in this order:

1. The mounted toolhead, or the active slot of a selector. Nothing moves.
2. Another slot of a selector.
3. A docked toolhead that can be exchanged automatically.
4. A docked toolhead that has to be changed by hand.
5. An unmounted toolhead.
6. A disconnected toolhead.
