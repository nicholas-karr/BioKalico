# Support for a manual controlled stepper
#
# Copyright (C) 2019-2025  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from klippy import chelper, stepper

from . import force_move


# Like ManualStepper, but is a multi stepper rail with min/max and homing.
class ManualRail:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        if config.get("endstop_pin", None) is not None:
            self.can_home = True
            self.rail = stepper.LookupMultiRail(config)
            self.steppers = self.rail.get_steppers()
        else:
            self.can_home = False
            self.rail = stepper.PrinterStepper(config)
            self.steppers = [self.rail]
        self.velocity = config.getfloat("velocity", 5.0, above=0.0)
        self.accel = self.homing_accel = config.getfloat(
            "accel", 0.0, minval=0.0
        )
        self.next_cmd_time = 0.0
        self.commanded_pos = 0.0
        self.pos_min, self.pos_max = self._resolve_position_range(config)
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.rail.setup_itersolve("cartesian_stepper_alloc", b"x")
        self.rail.set_trapq(self.trapq)
        # Registered with toolhead as an axtra axis
        self.axis_gcode_id = None
        self.instant_corner_v = 0.0
        self.gaxis_limit_velocity = self.gaxis_limit_accel = 0.0
        # Register commands
        rail_name = self.name.split()[1]
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "MANUAL_RAIL",
            "RAIL",
            rail_name,
            self.cmd_MANUAL_RAIL,
            desc=self.cmd_MANUAL_RAIL_help,
        )

    def _resolve_position_range(self, config):
        # When homing is available, self.rail is a real PrinterRail that
        # already parsed position_min/max from this same config section,
        # defaulting position_min to 0.0 when omitted (position_max has no
        # default and is required). Re-parsing those options here
        # independently, with a mismatched "None" default, used to leave
        # pos_min as None for a config that validly omits position_min -
        # and do_homing_move's arithmetic on hi.position_endstop - self.pos_min
        # would then crash with a TypeError instead of homing.
        if self.can_home:
            return self.rail.get_range()
        # A bare stepper (no endstop_pin) has no rail-level min/max concept
        # at all - position_min/max are an optional soft move-clamp here,
        # so None (no limit) is intentional when they're left unset.
        return (
            config.getfloat("position_min", None),
            config.getfloat("position_max", None),
        )

    def get_name(self):
        return self.name

    def sync_print_time(self):
        toolhead = self.printer.lookup_object("toolhead")
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time

    def do_enable(self, enable):
        self.sync_print_time()
        stepper_enable = self.printer.lookup_object("stepper_enable")
        for stepper_obj in self.steppers:
            enable_line = stepper_enable.lookup_enable(stepper_obj.get_name())
            if enable:
                enable_line.motor_enable(self.next_cmd_time)
            else:
                enable_line.motor_disable(self.next_cmd_time)
        self.sync_print_time()

    def do_set_position(self, setpos):
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.flush_step_generation()
        self.commanded_pos = setpos
        self.rail.set_position([self.commanded_pos, 0.0, 0.0])

    def _submit_move(self, movetime, movepos, speed, accel):
        cp = self.commanded_pos
        dist = movepos - cp
        axis_r, accel_t, cruise_t, cruise_v = force_move.calc_move_time(
            dist, speed, accel
        )
        self.trapq_append(
            self.trapq,
            movetime,
            accel_t,
            cruise_t,
            accel_t,
            cp,
            0.0,
            0.0,
            axis_r,
            0.0,
            0.0,
            0.0,
            cruise_v,
            accel,
        )
        self.commanded_pos = movepos
        return movetime + accel_t + cruise_t + accel_t

    def do_move(self, movepos, speed, accel, sync=True):
        self.sync_print_time()
        self.next_cmd_time = self._submit_move(
            self.next_cmd_time, movepos, speed, accel
        )
        self.rail.generate_steps(self.next_cmd_time)
        self.trapq_finalize_moves(
            self.trapq,
            self.next_cmd_time + 99999.9,
            self.next_cmd_time + 99999.9,
        )
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.note_mcu_movequeue_activity(self.next_cmd_time)
        if sync:
            self.sync_print_time()

    def do_homing_move(self, accel):
        if not self.can_home:
            raise self.printer.command_error(
                "No endstop for this manual stepper"
            )
        self.homing_accel = accel
        hi = self.rail.get_homing_info()
        pos = [hi.position_endstop, 0.0, 0.0, 0.0]
        forcepos = hi.position_endstop
        if hi.positive_dir:
            forcepos -= 1.5 * (hi.position_endstop - self.pos_min)
        else:
            forcepos += 1.5 * (self.pos_max - hi.position_endstop)
        self.do_set_position(forcepos)
        endstops = self.rail.get_endstops()
        phoming = self.printer.lookup_object("homing")
        phoming.manual_home(
            self, endstops, pos, hi.speed, triggered=True, check_triggered=True
        )
        # Perform second home
        if hi.retract_dist:
            retract_dist = (
                -hi.retract_dist if hi.positive_dir else hi.retract_dist
            )
            self.do_move(hi.position_endstop + retract_dist, hi.speed, accel)
            self.do_set_position(hi.position_endstop + retract_dist * 1.5)
            phoming.manual_home(
                self,
                endstops,
                pos,
                hi.second_homing_speed,
                triggered=True,
                check_triggered=True,
            )

    cmd_MANUAL_RAIL_help = "Command a manually configured rail"

    def cmd_MANUAL_RAIL(self, gcmd):
        if gcmd.get("GCODE_AXIS", None) is not None:
            return self.command_with_gcode_axis(gcmd)
        if self.axis_gcode_id is not None:
            raise gcmd.error("Must unregister from gcode axis first")
        enable = gcmd.get_int("ENABLE", None)
        if enable is not None:
            self.do_enable(enable)
        setpos = gcmd.get_float("SET_POSITION", None)
        if setpos is not None:
            self.do_set_position(setpos)
        speed = gcmd.get_float("SPEED", self.velocity, above=0.0)
        accel = gcmd.get_float("ACCEL", self.accel, minval=0.0)
        home = gcmd.get_int("HOME", 0)
        if home:
            self.do_enable(1)
            self.do_homing_move(accel=accel)
        elif gcmd.get_float("MOVE", None) is not None:
            movepos = gcmd.get_float("MOVE")
            sync = gcmd.get_int("SYNC", 1)
            if self.pos_min is not None and movepos < self.pos_min:
                raise gcmd.error(
                    "Stepper %s move to %s below min %s"
                    % (self.rail.get_name(), movepos, self.pos_min)
                )
            if self.pos_max is not None and movepos > self.pos_max:
                raise gcmd.error(
                    "Stepper %s move to %s above max %s"
                    % (self.rail.get_name(), movepos, self.pos_max)
                )
            self.do_move(movepos, speed, accel, sync)
        elif gcmd.get_int("SYNC", 0):
            self.sync_print_time()

    def get_status(self, eventtime):
        stepper_enable = self.printer.lookup_object("stepper_enable")
        enable = stepper_enable.lookup_enable(self.steppers[0].get_name())
        return {
            "position": self.rail.get_commanded_position(),
            "enabled": enable.is_motor_enabled(),
        }

    # Register as a gcode axis
    def command_with_gcode_axis(self, gcmd):
        toolhead = self.printer.lookup_object("toolhead")
        gcode_axis = gcmd.get("GCODE_AXIS").upper()
        instant_corner_v = gcmd.get_float(
            "INSTANTANEOUS_CORNER_VELOCITY", 1.0, minval=0.0
        )
        limit_velocity = gcmd.get_float("LIMIT_VELOCITY", 999999.9, above=0.0)
        limit_accel = gcmd.get_float("LIMIT_ACCEL", 999999.9, above=0.0)
        if self.axis_gcode_id is not None:
            if gcode_axis:
                raise gcmd.error("Must unregister axis first")
            # Unregister
            toolhead.remove_extra_axis(self)
            self.axis_gcode_id = None
            return
        if not all(
            hasattr(toolhead, method)
            for method in (
                "add_extra_axis",
                "remove_extra_axis",
                "get_extra_axes",
            )
        ):
            raise gcmd.error(
                "GCODE_AXIS requires toolhead extra-axis support, which is "
                "not available in this Klipper build"
            )
        if (
            len(gcode_axis) != 1
            or not gcode_axis.isupper()
            or gcode_axis in "XYZEFN"
        ):
            if not gcode_axis:
                # Request to unregister already unregistered axis
                return
            raise gcmd.error("Not a valid GCODE_AXIS")
        for ea in toolhead.get_extra_axes():
            if ea is not None and ea.get_axis_gcode_id() == gcode_axis:
                raise gcmd.error("Axis '%s' already registered" % (gcode_axis,))
        self.axis_gcode_id = gcode_axis
        self.instant_corner_v = instant_corner_v
        self.gaxis_limit_velocity = limit_velocity
        self.gaxis_limit_accel = limit_accel
        toolhead.add_extra_axis(self, self.commanded_pos)

    def process_move(self, print_time, move, ea_index):
        axis_r = move.axes_r[ea_index]
        start_pos = move.start_pos[ea_index]
        accel = move.accel * axis_r
        start_v = move.start_v * axis_r
        cruise_v = move.cruise_v * axis_r
        self.trapq_append(
            self.trapq,
            print_time,
            move.accel_t,
            move.cruise_t,
            move.decel_t,
            start_pos,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            start_v,
            cruise_v,
            accel,
        )
        self.commanded_pos = move.end_pos[ea_index]

    def check_move(self, move, ea_index):
        # Check move is in bounds
        movepos = move.end_pos[ea_index]
        if (self.pos_min is not None and movepos < self.pos_min) or (
            self.pos_max is not None and movepos > self.pos_max
        ):
            raise move.move_error()
        # Check if need to limit maximum velocity and acceleration
        axis_ratio = move.move_d / abs(move.axes_d[ea_index])
        limit_velocity = self.gaxis_limit_velocity * axis_ratio
        limit_accel = self.gaxis_limit_accel * axis_ratio
        if not move.is_kinematic_move and self.accel:
            limit_accel = min(limit_accel, self.accel * axis_ratio)
        move.limit_speed(limit_velocity, limit_accel)

    def calc_junction(self, prev_move, move, ea_index):
        diff_r = move.axes_r[ea_index] - prev_move.axes_r[ea_index]
        if diff_r:
            return (self.instant_corner_v / abs(diff_r)) ** 2
        return move.max_cruise_v2

    def get_axis_gcode_id(self):
        return self.axis_gcode_id

    def get_trapq(self):
        return self.trapq

    # Toolhead wrappers to support homing
    def flush_step_generation(self):
        self.sync_print_time()

    def get_position(self):
        return [self.commanded_pos, 0.0, 0.0, 0.0]

    def set_position(self, newpos, homing_axes=""):
        self.do_set_position(newpos[0])

    def get_last_move_time(self):
        self.sync_print_time()
        return self.next_cmd_time

    def dwell(self, delay):
        self.next_cmd_time += max(0.0, delay)

    def drip_move(self, newpos, speed, drip_completion):
        self.do_move(newpos[0], speed, self.homing_accel)

    def get_kinematics(self):
        return self

    def get_steppers(self):
        return self.steppers

    def calc_position(self, stepper_positions):
        return [stepper_positions[self.rail.get_name()], 0.0, 0.0]


# Dummy object for multi stepper setup
class DummyStepper:
    def get_status(self, eventtime):
        return {}


def load_config_prefix(config):
    name = config.get_name()
    # Return a dummy if this is a secondary motor in a multi-motor setup.
    for i in range(1, 99):
        if name.endswith(str(i)) and config.has_section(name[: -len(str(i))]):
            return DummyStepper()
    return ManualRail(config)
