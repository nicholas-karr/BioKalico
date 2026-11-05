# Code for handling the kinematics of printers with only a Z axis
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from klippy import stepper


class ZOnlyKinematics:
    def __init__(self, toolhead, config):
        self.printer = config.get_printer()
        rail = stepper.LookupMultiRail(config.getsection("stepper_z"))
        rail.setup_itersolve("cartesian_stepper_alloc", b"z")
        self.rails = [rail]
        self.supports_dual_carriage = False
        for s in rail.get_steppers():
            s.set_trapq(toolhead.get_trapq())
            toolhead.register_step_generator(s.generate_steps)
        self.printer.register_event_handler(
            "stepper_enable:motor_off", self._motor_off
        )
        max_velocity, max_accel = toolhead.get_max_velocity()
        self.max_z_velocity = config.getfloat(
            "max_z_velocity", max_velocity, above=0.0, maxval=max_velocity
        )
        self.max_z_accel = config.getfloat(
            "max_z_accel", max_accel, above=0.0, maxval=max_accel
        )
        z_min, z_max = rail.get_range()
        self.axes_min = toolhead.Coord(0.0, 0.0, z_min, e=0.0)
        self.axes_max = toolhead.Coord(0.0, 0.0, z_max, e=0.0)
        self.limits = (1.0, -1.0)

    def get_steppers(self):
        return self.rails[0].get_steppers()

    def calc_position(self, stepper_positions):
        return [0.0, 0.0, stepper_positions[self.rails[0].get_name()]]

    def set_position(self, newpos, homing_axes):
        self.rails[0].set_position(newpos)
        if 2 in homing_axes:
            self.limits = self.rails[0].get_range()

    def note_z_not_homed(self):
        self.clear_homing_state([2])

    def clear_homing_state(self, axes):
        if 2 in axes:
            self.limits = (1.0, -1.0)

    def home(self, homing_state):
        # There is no X or Y axis to home, so only a Z request moves
        if 2 not in homing_state.get_axes():
            return
        rail = self.rails[0]
        position_min, position_max = rail.get_range()
        hi = rail.get_homing_info()
        homepos = [None, None, hi.position_endstop, None]
        forcepos = list(homepos)
        if hi.positive_dir:
            forcepos[2] -= 1.5 * (hi.position_endstop - position_min)
        else:
            forcepos[2] += 1.5 * (position_max - hi.position_endstop)
        homing_state.home_rails([rail], forcepos, homepos)

    def _motor_off(self, print_time):
        self.clear_homing_state((2,))

    def check_move(self, move):
        if move.axes_d[0] or move.axes_d[1]:
            raise move.move_error("This printer has no X or Y axis")
        if not move.axes_d[2]:
            return
        zpos = move.end_pos[2]
        if zpos < self.limits[0] or zpos > self.limits[1]:
            if self.limits[0] > self.limits[1]:
                raise move.move_error("Must home axis first")
            raise move.move_error()
        move.limit_speed(self.max_z_velocity, self.max_z_accel)

    def get_status(self, eventtime):
        l, h = self.limits
        return {
            "homed_axes": "z" if l <= h else "",
            "axis_minimum": self.axes_min,
            "axis_maximum": self.axes_max,
        }


def load_kinematics(toolhead, config):
    return ZOnlyKinematics(toolhead, config)
