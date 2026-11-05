# Build plate calibration for a projector + nozzle bioprinter.
#
# Remembers three things until the user changes them:
#
#  - the build plate rectangle: where the build plate sits in the projector
#    image, in projector pixels. The projector extra stretches every layer
#    onto it;
#  - the Z the projector is in focus at, which is the SLA tool's Z reference;
#  - for each nozzle, the machine position of the front left corner of the
#    build plate, which is that nozzle's relative (0, 0, 0).
#
# The wizard in Mainsail drives the CAL_* commands. Nothing here moves the
# printer: the user jogs the axes, and a save command records where they are.
#
# A saved position becomes the g-code origin while a print uses its tool:
# when the print selects a material, the position saved for the toolhead or
# material selector that holds it is set as the g-code offset, and it is
# taken off again when the print ends. Outside a print, positions stay in
# machine coordinates.
#
# Values are runtime state, kept in a state file next to the printer config
# and never written to printer.cfg, so saving needs no restart. When a
# [tool <name>] section from the toolchanger exists, the saved offset is also
# given to it as gcode_x/y/z_offset.
#
# Copyright (C) 2026  Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import configparser
import logging
import os
import time

from . import materials as materials_mod

AXES = "xyz"


class PlateCalibration:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.state_file = os.path.expanduser(
            config.get(
                "state_file",
                "~/printer_data/config/plate_calibration_state.cfg",
            )
        )
        self.sla_tool = config.get("sla_tool", "sla")
        self.rect = None
        self.screen = None
        self.calibrated_at = None
        self.offsets = {}  # tool name -> {"x": .., "y": .., "z": ..}
        self.preview = None  # "focus", "rect" or None
        self.image_display = None
        self.gcode_move = None
        self.print_stats = None
        self.origin_tool = None  # whose saved position the print is using
        self._load_state()
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.printer.register_event_handler(
            "toolheads:selected", self._handle_selected
        )
        self.printer.register_event_handler(
            "homing:home_rails_begin", self._handle_home_rails_begin
        )
        for state in ("complete", "error", "cancelled"):
            self.printer.register_event_handler(
                "print_stats:%s_printing" % (state,), self._release_origin
            )
        for name, func, desc in (
            (
                "CAL_PROJECTOR_FOCUS_START",
                self.cmd_FOCUS_START,
                "Show the focus pattern on the projector",
            ),
            (
                "CAL_PROJECTOR_FOCUS_SAVE",
                self.cmd_FOCUS_SAVE,
                "Save the current Z as the projector focus height",
            ),
            (
                "CAL_PLATE_RECT",
                self.cmd_PLATE_RECT,
                "Preview the build plate rectangle (X= Y= W= H= in projector "
                "pixels)",
            ),
            (
                "CAL_PLATE_RECT_SAVE",
                self.cmd_PLATE_RECT_SAVE,
                "Save the build plate rectangle (X= Y= W= H=)",
            ),
            (
                "CAL_PLATE_RECT_CLEAR",
                self.cmd_PLATE_RECT_CLEAR,
                "Forget the build plate rectangle",
            ),
            (
                "CAL_TOOL_SAVE",
                self.cmd_TOOL_SAVE,
                "Save the current position as the front left corner of the "
                "build plate for a nozzle (TOOL=<name>)",
            ),
            (
                "CAL_TOOL_CLEAR",
                self.cmd_TOOL_CLEAR,
                "Forget the saved offset of a nozzle (TOOL=<name>)",
            ),
            (
                "CAL_CANCEL",
                self.cmd_CANCEL,
                "Leave the calibration patterns and clear the projector",
            ),
        ):
            self.gcode.register_command(name, func, desc=desc)

    def _handle_connect(self):
        self.image_display = self.printer.lookup_object("image_display", None)
        self.gcode_move = self.printer.lookup_object("gcode_move")
        self.print_stats = self.printer.lookup_object("print_stats", None)
        self._apply_rect()
        for name in self.offsets:
            self._apply_offset(name)

    def _apply_rect(self):
        if self.image_display is not None:
            self.image_display.set_plate_rect(self.rect)

    def _apply_offset(self, name):
        tool = self.printer.lookup_object("tool " + name, None)
        if tool is None:
            return
        values = self.offsets.get(name)
        for axis in AXES:
            tool.set_parameter(
                "gcode_%s_offset" % axis,
                values[axis] if values and axis in values else 0.0,
            )

    # G-code origin

    def _set_gcode_offset(self, values):
        params = {a.upper(): "%.6f" % (values.get(a, 0.0),) for a in AXES}
        self.gcode_move.cmd_SET_GCODE_OFFSET(
            self.gcode.create_gcode_command(
                "SET_GCODE_OFFSET", "SET_GCODE_OFFSET", params
            )
        )

    def _handle_selected(self, name):
        if self.print_stats is None or self.print_stats.state != "printing":
            return
        values = self.offsets.get(name)
        if not values:
            self._release_origin()
            return
        self._set_gcode_offset(values)
        self.origin_tool = name
        self.gcode.respond_info(
            "%s: this print's %s is the saved position %s"
            % (
                name,
                " ".join("%s0" % (a.upper(),) for a in AXES if a in values),
                " ".join(
                    "%s%.3f" % (a.upper(), values[a])
                    for a in AXES
                    if a in values
                ),
            )
        )

    def _handle_home_rails_begin(self, homing_state, rails):
        # The moves around homing are in machine coordinates. origin_tool is
        # kept, since a paused print gets its offset back from RESUME and it
        # still has to come off when that print ends.
        if self.origin_tool is not None:
            self._set_gcode_offset({})

    def _release_origin(self):
        if self.origin_tool is None:
            return
        self._set_gcode_offset({})
        self.origin_tool = None

    # State

    def _load_state(self):
        parser = materials_mod.read_state_file(
            self.state_file, "plate_calibration"
        )
        if parser is None:
            return
        for section in parser.sections():
            try:
                if section == "plate":
                    self.rect = tuple(
                        parser.getint(section, k) for k in ("x", "y", "w", "h")
                    )
                    if self.rect[2] <= 0 or self.rect[3] <= 0:
                        raise ValueError("empty rectangle")
                    self.screen = (
                        parser.getint(section, "screen_w", fallback=0),
                        parser.getint(section, "screen_h", fallback=0),
                    )
                    self.calibrated_at = parser.get(
                        section, "calibrated_at", fallback=None
                    )
                elif section.startswith("tool "):
                    name = section.split(None, 1)[1]
                    self.offsets[name] = {
                        axis: parser.getfloat(section, axis)
                        for axis in AXES
                        if parser.has_option(section, axis)
                    }
            except (ValueError, configparser.Error):
                logging.exception(
                    "plate_calibration: ignoring unreadable section [%s]",
                    section,
                )
                if section == "plate":
                    self.rect = None

    def _save_state(self):
        parser = configparser.ConfigParser(interpolation=None)
        if self.rect is not None:
            parser.add_section("plate")
            for key, value in zip("xywh", self.rect):
                parser.set("plate", key, str(value))
            parser.set("plate", "screen_w", str(self.screen[0]))
            parser.set("plate", "screen_h", str(self.screen[1]))
            if self.calibrated_at:
                parser.set("plate", "calibrated_at", self.calibrated_at)
        for name, values in self.offsets.items():
            section = "tool " + name
            parser.add_section(section)
            for axis, value in values.items():
                parser.set(section, axis, "%.4f" % (value,))
        try:
            materials_mod.write_state_file(
                self.state_file, parser, "plate_calibration"
            )
        except OSError as e:
            logging.exception("plate_calibration: unable to save state file")
            raise self.printer.command_error(
                "Unable to save plate calibration: %s" % (e,)
            )

    # Helpers

    def _projector(self, gcmd):
        if self.image_display is None:
            raise gcmd.error("plate_calibration: there is no [image_display]")
        return self.image_display

    def _screen_size(self):
        if self.image_display is None:
            return (0, 0)
        status = self.image_display.get_status(
            self.printer.get_reactor().monotonic()
        )
        return (
            int(status.get("window_width") or 0),
            int(status.get("window_height") or 0),
        )

    def _position(self, gcmd, axes):
        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(self.printer.get_reactor().monotonic())[
            "homed_axes"
        ]
        missing = [a.upper() for a in axes if a not in homed]
        if missing:
            raise gcmd.error(
                "Home %s first, so the saved position means something"
                % ("".join(missing),)
            )
        return toolhead.get_position()

    @staticmethod
    def _get_rect(gcmd):
        x = gcmd.get_int("X")
        y = gcmd.get_int("Y")
        w = gcmd.get_int("W", minval=1)
        h = gcmd.get_int("H", minval=1)
        return (x, y, w, h)

    def _tool_name(self, gcmd):
        name = gcmd.get("TOOL").strip()
        if not name:
            raise gcmd.error("TOOL must not be empty")
        return name

    # Commands

    def cmd_FOCUS_START(self, gcmd):
        self._projector(gcmd).show_calibration_pattern(gcmd, "focus")
        self.preview = "focus"

    def cmd_FOCUS_SAVE(self, gcmd):
        z = self._position(gcmd, "z")[2]
        self.offsets.setdefault(self.sla_tool, {})["z"] = z
        self._save_state()
        self._apply_offset(self.sla_tool)
        gcmd.respond_info(
            "Projector focus saved: Z %.3f is the %s tool's Z reference"
            % (z, self.sla_tool)
        )

    def cmd_PLATE_RECT(self, gcmd):
        rect = self._get_rect(gcmd)
        self._projector(gcmd).show_calibration_pattern(gcmd, "rect", rect)
        self.preview = "rect"

    def cmd_PLATE_RECT_SAVE(self, gcmd):
        rect = self._get_rect(gcmd)
        self.rect = rect
        self.screen = self._screen_size()
        self.calibrated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()
        self._apply_rect()
        gcmd.respond_info("Build plate rectangle saved: %d,%d %dx%d px" % rect)

    def cmd_PLATE_RECT_CLEAR(self, gcmd):
        self.rect = None
        self.screen = None
        self.calibrated_at = None
        self._save_state()
        self._apply_rect()
        gcmd.respond_info("Build plate rectangle cleared")

    def cmd_TOOL_SAVE(self, gcmd):
        name = self._tool_name(gcmd)
        pos = self._position(gcmd, AXES)
        self.offsets[name] = dict(zip(AXES, pos[:3]))
        self._save_state()
        self._apply_offset(name)
        gcmd.respond_info(
            "Saved the front left corner for %s: X%.3f Y%.3f Z%.3f"
            % (name, pos[0], pos[1], pos[2])
        )

    def cmd_TOOL_CLEAR(self, gcmd):
        name = self._tool_name(gcmd)
        if self.offsets.pop(name, None) is None:
            gcmd.respond_info("%s has no saved offset" % (name,))
            return
        self._save_state()
        self._apply_offset(name)
        gcmd.respond_info("Cleared the saved offset of %s" % (name,))

    def cmd_CANCEL(self, gcmd):
        if self.preview is not None and self.image_display is not None:
            self.image_display.clear_pattern(gcmd)
        self.preview = None

    def get_status(self, eventtime=None):
        screen_now = self._screen_size()
        mismatch = bool(
            self.rect
            and self.screen
            and all(self.screen)
            and all(screen_now)
            and self.screen != screen_now
        )
        return {
            "calibrated": self.rect is not None and not mismatch,
            "rect": list(self.rect) if self.rect else None,
            "screen": list(self.screen) if self.screen else None,
            "screen_mismatch": mismatch,
            "calibrated_at": self.calibrated_at,
            "preview": self.preview,
            "sla_tool": self.sla_tool,
            "origin_tool": self.origin_tool,
            "offsets": {n: dict(v) for n, v in self.offsets.items()},
        }


def load_config(config):
    return PlateCalibration(config)
