# Stall detection for TMC2209 driven steppers while they are moving.
#
# A stalled stepper barely changes its average StallGuard reading, so one
# fixed threshold cannot tell a stall from free motion across speeds. What
# does change is that the reading dips sharply each time the motor loads up
# and skips. This module learns the normal readings for each steady motion,
# and treats readings well below the lowest of them as a stall.
#
# Where the driver's DIAG pin is wired, the comparison is left to the driver.
# The planned moves are read from the motion queue ahead of time, and for
# each stretch of constant speed the driver gets a threshold for that motion
# and a speed gate just below it. The driver then checks every motor step
# and the micro-controller reports pulses on the pin. The host only reads
# the driver to learn a motion it has not seen. Steppers without a DIAG pin
# are read and compared by the host during those stretches.
#
# Copyright (C) 2026  Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections
import logging
import math

from klippy import chelper

from . import tmc

# Speeds within this ratio of each other share one learned reading.
BUCKET_RATIO = 1.05
# A stepper slower than this counts as standing still.
STILL_SPEED = 0.5
# Readings kept per motion. New readings are compared with the lowest.
BUCKET_HISTORY = 64
# Motions remembered per stepper. The least recently seen is dropped first.
MAX_MOTIONS = 2048
# A stepper holds its speed if it changes less than this over a stretch.
SPEED_TOLERANCE = 0.02
# The driver's speed gate, as a fraction of the speed being checked.
GATE_RATIO = 0.9
# Checking stops this long before a stretch of constant speed ends.
GUARD_TIME = 0.02
# Stretches that leave less time than this to check are skipped.
MIN_CHECK_TIME = 0.05
# Driver commands are sent this long before they are due.
LEAD_TIME = 0.01
# Time between readings while a motion is being learned. Spreading them
# out lets them cover the slow swings in load a motion can have.
LEARN_INTERVAL = 0.03
# Time between readings that keep a learned motion up to date while the
# driver does the checking.
REFRESH_INTERVAL = 0.1
# A pulse may be reported this long after the driver was told to stop.
PULSE_GRACE_TIME = 0.01
# Shortest time between two stall messages when the action is "report".
REPORT_INTERVAL = 1.0
ACTIONS = {"shutdown": "shutdown", "report": "report"}


# Identifies a steady motion by the direction and speed of every watched
# stepper. A stepper's reading at one speed also depends on its direction
# and on what the other steppers are doing.
def get_motion_key(velocities):
    key = []
    for velocity in velocities:
        speed = abs(velocity)
        if speed < STILL_SPEED:
            key.append(None)
            continue
        bucket = int(math.floor(math.log(speed) / math.log(BUCKET_RATIO)))
        key.append((velocity > 0.0, bucket))
    return tuple(key)


# The normal StallGuard readings of one stepper in each motion
class MotionBaseline:
    def __init__(self, learn_samples):
        self.learn_samples = learn_samples
        self.motions = collections.OrderedDict()

    def add(self, key, reading):
        if reading <= 0:
            # StallGuard 0 is the end of the scale, not a normal reading. A
            # floor of 0 would never trip and would look like "not learned".
            return
        readings = self.motions.get(key)
        if readings is None:
            readings = self.motions[key] = collections.deque(
                maxlen=BUCKET_HISTORY
            )
            if len(self.motions) > MAX_MOTIONS:
                self.motions.popitem(last=False)
        else:
            self.motions.move_to_end(key)
        readings.append(reading)

    def get_floor(self, key):
        readings = self.motions.get(key)
        if readings is None or len(readings) < self.learn_samples:
            return None
        return min(readings)

    def clear(self):
        self.motions.clear()


# A stretch of constant speed taken from the motion queue
class Stretch:
    def __init__(self, start, end, key, velocities):
        self.start = start
        self.end = end
        self.key = key
        self.velocities = velocities
        self.active = False
        self.checked_from = None
        # Print time of the next reading of each stepper that is read
        self.next_reads = {}


class WatchedStepper:
    def __init__(self, printer, name, learn_samples, diag_pin):
        self.name = name
        self.diag_pin = diag_pin
        force_move = printer.lookup_object("force_move")
        self.stepper = force_move.lookup_stepper(name)
        driver = printer.lookup_object("tmc2209 " + name, None)
        if driver is None:
            raise printer.config_error(
                "stall_monitor: stepper '%s' needs a [tmc2209 %s] section"
                % (name, name)
            )
        self.mcu_tmc = driver.mcu_tmc
        self.fields = driver.fields
        self.baseline = MotionBaseline(learn_samples)
        self.lowest_ratio = None
        self.lowest_ratio_speed = 0.0
        self.gate = None
        self.armed_from = None
        self.armed_until = None
        self.driver_changed = False

    # The driver only measures load in stealthChop mode. Returns the speed
    # at which it leaves that mode.
    def get_stealthchop_limit(self):
        if self.fields.get_field("en_spreadcycle"):
            return 0.0
        tpwmthrs = self.fields.get_field("tpwmthrs")
        if not tpwmthrs:
            return float("inf")
        mres = self.fields.get_field("mres")
        step_dist_256 = self.stepper.get_step_dist() / (1 << mres)
        return self.mcu_tmc.get_tmc_frequency() * step_dist_256 / tpwmthrs

    # Returns the stepper's velocity while the toolhead moves from the
    # given position at the given velocity.
    def get_velocity(self, position, velocity):
        calc_position = self.stepper.calc_position_from_coord
        ahead = [p + v for p, v in zip(position, velocity)]
        return calc_position(ahead) - calc_position(position)

    def read(self):
        value = self.mcu_tmc.get_register("SG_RESULT")
        return self.fields.get_field("sg_result", value, "SG_RESULT")

    # Has the driver raise its DIAG pin from print_time on while the
    # reading is below trip_reading and the stepper is near the given speed.
    def arm(self, trip_reading, speed, print_time):
        self.driver_changed = True
        gate = tmc.TMCtstepHelper(
            self.mcu_tmc, GATE_RATIO * speed, pstepper=self.stepper
        )
        if gate != self.gate:
            self.gate = None
            self.mcu_tmc.set_register("TCOOLTHRS", gate, print_time)
            self.gate = gate
        threshold = max(1, min(255, int(trip_reading / 2.0)))
        self.mcu_tmc.set_register("SGTHRS", threshold, print_time)
        self.armed_from = print_time
        self.armed_until = None

    # Silences the DIAG pin from print_time on, or at once if it is None.
    def disarm(self, print_time=None):
        if self.armed_from is None or self.armed_until is not None:
            return
        if print_time is None:
            self.armed_from = None
        else:
            self.armed_until = print_time
        self.mcu_tmc.set_register("SGTHRS", 0, print_time)

    def is_armed(self, print_time):
        if self.armed_from is None or print_time < self.armed_from:
            return False
        if self.armed_until is None:
            return True
        return print_time <= self.armed_until + PULSE_GRACE_TIME

    # Sensorless homing uses the same two driver settings. This puts back
    # the values the rest of Klipper expects.
    def restore_driver(self):
        if not self.driver_changed:
            return
        self.armed_from = self.armed_until = self.gate = None
        self.mcu_tmc.set_register(
            "TCOOLTHRS", self.fields.get_field("tcoolthrs")
        )
        self.mcu_tmc.set_register("SGTHRS", self.fields.get_field("sgthrs"))
        self.driver_changed = False

    def note_ratio(self, ratio, speed):
        if self.lowest_ratio is None or ratio < self.lowest_ratio:
            self.lowest_ratio = ratio
            self.lowest_ratio_speed = speed

    def clear(self):
        self.baseline.clear()
        self.lowest_ratio = None
        self.lowest_ratio_speed = 0.0


class StallMonitor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.stepper_names = config.getlist("steppers")
        self.sample_interval = config.getfloat(
            "sample_interval", 0.01, minval=0.001, maxval=1.0
        )
        self.settle_time = config.getfloat("settle_time", 0.1, above=0.0)
        self.min_velocity = config.getfloat("min_velocity", 12.0, above=0.0)
        self.trip_ratio = config.getfloat(
            "trip_ratio", 0.75, above=0.0, below=1.0
        )
        self.trip_count = config.getint("trip_count", 2, minval=1)
        self.trip_window = config.getfloat("trip_window", 0.25, above=0.0)
        self.learn_samples = config.getint(
            "learn_samples", 48, minval=1, maxval=BUCKET_HISTORY
        )
        self.action = config.getchoice("action", ACTIONS, "shutdown")
        self.enabled = True
        self.watched = []
        self.stretches = collections.deque()
        self.planned_time = 0.0
        self.ignore_before = 0.0
        self.next_index = 0
        self.homing_moves = 0
        self.low_times = collections.deque()
        self.checked_time = 0.0
        self.stalls = 0
        self.last_stall = ""
        self.last_report_time = 0.0
        self.mcu = self.trapq = self.run_timer = None
        self.printer.load_object(config, "force_move")
        self.diag_pins = {}
        if config.getboolean("use_diag_pins", True):
            self._setup_diag_pins(config)
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect
        )
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "klippy:shutdown", self._handle_shutdown
        )
        self.printer.register_event_handler(
            "homing:homing_move_begin", self._handle_homing_move_begin
        )
        self.printer.register_event_handler(
            "homing:homing_move_end", self._handle_homing_move_end
        )
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command(
            "SET_STALL_MONITOR",
            "MONITOR",
            self.name,
            self.cmd_SET_STALL_MONITOR,
            desc=self.cmd_SET_STALL_MONITOR_help,
        )
        gcode.register_mux_command(
            "QUERY_STALL_MONITOR",
            "MONITOR",
            self.name,
            self.cmd_QUERY_STALL_MONITOR,
            desc=self.cmd_QUERY_STALL_MONITOR_help,
        )

    # A stepper's DIAG pin is usually its sensorless homing endstop too,
    # so the pin has to be shared.
    def _setup_diag_pins(self, config):
        buttons = self.printer.load_object(config, "buttons")
        ppins = self.printer.lookup_object("pins")
        for name in self.stepper_names:
            section = "tmc2209 " + name
            if not config.has_section(section):
                continue
            diag_pin = config.getsection(section).get("diag_pin", None)
            if diag_pin is None:
                continue
            self.diag_pins[name] = diag_pin
            ppins.allow_multi_use_pin(diag_pin.strip().lstrip("^~!"))
            buttons.register_buttons(
                [diag_pin],
                lambda eventtime, state, name=name: self._handle_diag(
                    eventtime, state, name
                ),
            )

    def _handle_connect(self):
        for name in self.stepper_names:
            watched = WatchedStepper(
                self.printer,
                name,
                self.learn_samples,
                self.diag_pins.get(name),
            )
            if watched.get_stealthchop_limit() <= self.min_velocity:
                raise self.printer.config_error(
                    "stall_monitor: stepper '%s' must run in stealthChop."
                    " Set stealthchop_threshold in [tmc2209 %s] above the"
                    " printer's top speed" % (name, name)
                )
            self.watched.append(watched)
        self.mcu = self.printer.lookup_object("mcu")
        toolhead = self.printer.lookup_object("toolhead")
        self.trapq = toolhead.get_trapq()
        toolhead.register_step_generator(self._handle_step_generation)

    def _handle_ready(self):
        self.planned_time = self._get_print_time()
        self.run_timer = self.reactor.register_timer(self._run)

    def _handle_shutdown(self):
        if self.run_timer is not None:
            self.reactor.unregister_timer(self.run_timer)
            self.run_timer = None

    def _handle_step_generation(self, flush_time):
        if self.run_timer is not None and self.enabled:
            self.reactor.update_timer(self.run_timer, self.reactor.NOW)

    def _handle_homing_move_begin(self, hmove):
        self.homing_moves += 1
        self._stop_checking()
        for watched in self.watched:
            watched.restore_driver()

    def _handle_homing_move_end(self, hmove):
        self.homing_moves = max(0, self.homing_moves - 1)
        # A homing move is queued at its full length and cut short when the
        # endstop triggers, so what is left of it in the queue never runs.
        self.ignore_before = self._get_print_time()

    def _handle_diag(self, eventtime, state, name):
        if not state or not self.enabled or self.homing_moves:
            return
        print_time = self.mcu.estimated_print_time(eventtime)
        for index, watched in enumerate(self.watched):
            if watched.name != name or not watched.is_armed(print_time):
                continue
            speed = 0.0
            if self.stretches and self.stretches[0].active:
                speed = abs(self.stretches[0].velocities[index])
            logging.info(
                "stall_monitor %s: stall pin pulse on %s at %.1f mm/s",
                self.name,
                name,
                speed,
            )
            self._note_low_reading(watched, speed)

    def _get_print_time(self):
        return self.mcu.estimated_print_time(self.reactor.monotonic())

    # Reads the moves added to the motion queue since the last call and
    # notes each stretch of constant speed as something to check. Nothing
    # is read while homing, and moves that started before homing ended are
    # skipped.
    def _plan(self):
        if self.homing_moves:
            return
        ffi_main, ffi_lib = chelper.get_ffi()
        moves = []
        # Moves the step generators are done with are in the history, newest
        # first. Those still queued are listed oldest first.
        end_time = float("inf")
        while True:
            data = ffi_main.new("struct pull_move[128]")
            count = ffi_lib.trapq_extract_old(
                self.trapq, data, len(data), self.planned_time, end_time
            )
            moves.extend(data[i] for i in range(count))
            if count < len(data):
                break
            end_time = data[count - 1].print_time
        start_time = self.planned_time
        while True:
            data = ffi_main.new("struct pull_move[128]")
            count = ffi_lib.trapq_extract_pending(
                self.trapq, data, len(data), start_time, float("inf")
            )
            moves.extend(data[i] for i in range(count))
            if count < len(data):
                break
            start_time = data[count - 1].print_time + data[count - 1].move_t
        for move in sorted(moves, key=lambda m: m.print_time):
            start = max(move.print_time, self.planned_time)
            end = move.print_time + move.move_t
            if end <= start:
                continue
            self.planned_time = end
            if move.accel or not move.start_v:
                continue
            if move.print_time < self.ignore_before:
                continue
            self._note_cruise(move, start, end)

    def _note_cruise(self, move, start, end):
        position = (move.start_x, move.start_y, move.start_z)
        velocity = [
            move.start_v * axis for axis in (move.x_r, move.y_r, move.z_r)
        ]
        final = [p + v * move.move_t for p, v in zip(position, velocity)]
        velocities = []
        checkable = False
        for watched in self.watched:
            stepper_velocity = watched.get_velocity(position, velocity)
            change = stepper_velocity - watched.get_velocity(final, velocity)
            if abs(change) > SPEED_TOLERANCE * abs(stepper_velocity):
                return
            velocities.append(stepper_velocity)
            if self._is_checkable(watched, stepper_velocity):
                checkable = True
        if not checkable:
            return
        key = get_motion_key(velocities)
        if self.stretches:
            last = self.stretches[-1]
            if last.key == key and abs(last.end - start) < 1e-6:
                last.end = end
                return
        self.stretches.append(Stretch(start, end, key, velocities))

    def _is_checkable(self, watched, velocity):
        speed = abs(velocity)
        return self.min_velocity <= speed < watched.get_stealthchop_limit()

    def _run(self, eventtime):
        while True:
            self._plan()
            if not self.enabled or self.homing_moves:
                self._stop_checking()
                return self.reactor.NEVER
            if not self.stretches:
                return self.reactor.NEVER
            stretch = self.stretches[0]
            now = self._get_print_time()
            check_start = stretch.start + self.settle_time
            check_end = stretch.end - GUARD_TIME
            if stretch.active:
                if now >= check_end - LEAD_TIME:
                    self._end_stretch(stretch, check_end)
                    continue
                wake_time = min(
                    self._read_due(stretch, now), check_end - LEAD_TIME
                )
            elif check_end - max(now, check_start) < MIN_CHECK_TIME:
                self.stretches.popleft()
                continue
            elif now < check_start - LEAD_TIME:
                wake_time = check_start - LEAD_TIME
            else:
                self._begin_stretch(stretch, max(now, check_start))
                continue
            now = self._get_print_time()
            if wake_time > now:
                return self.reactor.monotonic() + wake_time - now

    def _begin_stretch(self, stretch, print_time):
        stretch.active = True
        for index, watched in enumerate(self.watched):
            if not self._is_checkable(watched, stretch.velocities[index]):
                continue
            stretch.next_reads[index] = print_time
            if watched.baseline.get_floor(stretch.key):
                stretch.checked_from = print_time
            self._arm_if_learned(stretch, index, print_time)

    def _arm_if_learned(self, stretch, index, print_time):
        watched = self.watched[index]
        floor = watched.baseline.get_floor(stretch.key)
        if watched.diag_pin is None or not floor or not stretch.active:
            return
        if watched.is_armed(print_time):
            return
        try:
            watched.arm(
                self.trip_ratio * floor,
                abs(stretch.velocities[index]),
                print_time,
            )
        except self.printer.command_error:
            logging.exception("stall_monitor %s: unable to arm", self.name)
        if self.homing_moves:
            # Homing started while the driver was being written to
            watched.restore_driver()
        elif not stretch.active:
            watched.disarm()

    def _end_stretch(self, stretch, print_time):
        stretch.active = False
        for watched in self.watched:
            try:
                watched.disarm(print_time)
            except self.printer.command_error:
                logging.exception(
                    "stall_monitor %s: unable to disarm", self.name
                )
        if print_time is None:
            print_time = self._get_print_time()
        if stretch.checked_from is not None:
            self.checked_time += max(0.0, print_time - stretch.checked_from)
        if self.stretches and self.stretches[0] is stretch:
            self.stretches.popleft()

    def _stop_checking(self):
        if self.stretches and self.stretches[0].active:
            self._end_stretch(self.stretches[0], None)
        self.stretches.clear()

    # Takes at most one reading that is due. Returns the print time of the
    # next one.
    def _read_due(self, stretch, now):
        count = len(self.watched)
        for offset in range(count):
            index = (self.next_index + offset) % count
            if stretch.next_reads.get(index, float("inf")) <= now:
                self.next_index = (index + 1) % count
                self._read(stretch, index)
                break
        if not stretch.next_reads:
            return float("inf")
        return min(stretch.next_reads.values())

    def _read(self, stretch, index):
        watched = self.watched[index]
        try:
            reading = watched.read()
        except self.printer.command_error:
            reading = None
        now = self._get_print_time()
        if reading is not None and self._is_checking(stretch, now):
            self._note_reading(stretch, index, reading, now)
        interval = LEARN_INTERVAL
        if watched.is_armed(now):
            interval = REFRESH_INTERVAL
        elif watched.baseline.get_floor(stretch.key):
            interval = self.sample_interval
        stretch.next_reads[index] = max(
            stretch.next_reads[index] + interval, now
        )

    def _is_checking(self, stretch, print_time):
        if not stretch.active or not self.enabled or self.homing_moves:
            return False
        return print_time <= stretch.end - GUARD_TIME

    def _note_reading(self, stretch, index, reading, print_time):
        watched = self.watched[index]
        speed = abs(stretch.velocities[index])
        floor = watched.baseline.get_floor(stretch.key)
        if floor:
            watched.note_ratio(float(reading) / floor, speed)
            # A stepper checked by its driver is only read to keep the
            # motion's readings current, so a low one is learned too.
            if watched.diag_pin is None and reading < self.trip_ratio * floor:
                logging.info(
                    "stall_monitor %s: low reading on %s at %.1f mm/s:"
                    " %d (floor %d)",
                    self.name,
                    watched.name,
                    speed,
                    reading,
                    floor,
                )
                self._note_low_reading(watched, speed)
                return
        watched.baseline.add(stretch.key, reading)
        if watched.baseline.get_floor(stretch.key):
            if stretch.checked_from is None:
                stretch.checked_from = print_time
            self._arm_if_learned(stretch, index, print_time)

    def _note_low_reading(self, watched, speed):
        eventtime = self.reactor.monotonic()
        self.low_times.append(eventtime)
        while self.low_times[0] < eventtime - self.trip_window:
            self.low_times.popleft()
        if len(self.low_times) < self.trip_count:
            return
        self.low_times.clear()
        self.stalls += 1
        self.last_stall = "stall on %s at %.1f mm/s" % (watched.name, speed)
        if self.action == "shutdown":
            # Worded like Klipper's other deliberate shutdowns, which
            # Moonraker's auto_recovery does not undo.
            self.printer.invoke_shutdown(
                "Shutdown due to %s. Something blocked the motor or it lost"
                " steps. Clear the obstruction, restart the firmware and"
                " home again." % (self.last_stall,)
            )
        elif eventtime >= self.last_report_time + REPORT_INTERVAL:
            self.last_report_time = eventtime
            gcode = self.printer.lookup_object("gcode")
            gcode.respond_info(
                "stall_monitor %s: %s" % (self.name, self.last_stall)
            )

    cmd_SET_STALL_MONITOR_help = "Configure stall detection during moves"

    def cmd_SET_STALL_MONITOR(self, gcmd):
        enable = gcmd.get_int("ENABLE", None, minval=0, maxval=1)
        action = gcmd.get("ACTION", None)
        if action is not None and action.lower() not in ACTIONS:
            raise gcmd.error(
                "ACTION must be one of: %s" % (", ".join(ACTIONS),)
            )
        self.trip_ratio = gcmd.get_float(
            "TRIP_RATIO", self.trip_ratio, above=0.0, below=1.0
        )
        if enable is not None:
            self.enabled = bool(enable)
            if self.enabled:
                self.planned_time = self._get_print_time()
            else:
                self._stop_checking()
        if action is not None:
            self.action = action.lower()
        if gcmd.get_int("CLEAR", 0, minval=0, maxval=1):
            for watched in self.watched:
                watched.clear()
            self.checked_time = 0.0
            self.stalls = 0
            self.last_stall = ""
        self.low_times.clear()

    cmd_QUERY_STALL_MONITOR_help = "Report stall detection state and margins"

    def cmd_QUERY_STALL_MONITOR(self, gcmd):
        lines = [
            "stall_monitor %s: %s, action %s, trip_ratio %.2f, stalls %d,"
            " checked for %.0f seconds"
            % (
                self.name,
                "enabled" if self.enabled else "disabled",
                self.action,
                self.trip_ratio,
                self.stalls,
                self.checked_time,
            )
        ]
        for watched in self.watched:
            how = "read by the host"
            if watched.diag_pin is not None:
                how = "checked by the driver"
            if watched.lowest_ratio is None:
                lines.append("%s (%s): no readings yet" % (watched.name, how))
                continue
            lines.append(
                "%s (%s): lowest reading was %.2f of the normal low,"
                " at %.1f mm/s"
                % (
                    watched.name,
                    how,
                    watched.lowest_ratio,
                    watched.lowest_ratio_speed,
                )
            )
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        return {
            "enabled": self.enabled,
            "action": self.action,
            "trip_ratio": self.trip_ratio,
            "checked_time": round(self.checked_time, 1),
            "stalls": self.stalls,
            "last_stall": self.last_stall,
            "lowest_ratio": {
                watched.name: watched.lowest_ratio for watched in self.watched
            },
        }


def load_config_prefix(config):
    return StallMonitor(config)
