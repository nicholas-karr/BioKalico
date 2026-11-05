import pathlib

import pytest
from klippy_testing import PrinterShim

import klippy.extras.stall_monitor as stall_monitor
from klippy import chelper

CONFIG_DIR = pathlib.Path(__file__).parent / "test_configs" / "stall_monitor"
STEP_DIST = 0.003125
ACCEL = 2000.0
NORMAL = 400
# int(0.75 * NORMAL / 2), the driver threshold for a motion reading NORMAL
ARMED_THRESHOLD = 150
HOMING_THRESHOLD = 100
HOMING_GATE = 469


class CommandError(Exception):
    pass


class ConfigError(Exception):
    pass


class FakeTimer:
    def __init__(self, callback, waketime):
        self.callback = callback
        self.waketime = waketime


class FakeReactor:
    """Time advances in 1 ms steps. Print time equals reactor time."""

    NOW = 0.0
    NEVER = 9999999999999999.0

    def __init__(self):
        self.now = 10.0
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, waketime=NEVER):
        timer = FakeTimer(callback, waketime)
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        self.timers.remove(timer)

    def update_timer(self, timer, waketime):
        timer.waketime = waketime

    def run_until(self, end):
        while self.now < end:
            self.now = round(self.now + 0.001, 6)
            for timer in list(self.timers):
                if timer.waketime <= self.now:
                    timer.waketime = timer.callback(self.now)


class FakeMcu:
    def estimated_print_time(self, eventtime):
        return eventtime


class FakeStepper:
    """One motor of a CoreXY printer."""

    def __init__(self, y_sign):
        self.y_sign = y_sign

    def get_step_dist(self):
        return STEP_DIST

    def calc_position_from_coord(self, coord):
        return coord[0] + self.y_sign * coord[1]


class FakeFields:
    def __init__(self):
        self.values = {
            "en_spreadcycle": 0,
            "tpwmthrs": 0,
            "mres": 3,
            "sgthrs": HOMING_THRESHOLD,
            "tcoolthrs": HOMING_GATE,
        }

    def get_field(self, name, value=None, reg_name=None):
        if value is not None:
            return value
        return self.values[name]


class FakeTmc:
    def __init__(self):
        self.fields = FakeFields()
        self.reading = NORMAL
        self.reads = 0
        self.fail = False
        self.writes = []

    def get_fields(self):
        return self.fields

    def get_register(self, name):
        assert name == "SG_RESULT"
        self.reads += 1
        if self.fail:
            raise CommandError("Unable to read tmc uart register")
        return self.reading

    def set_register(self, name, value, print_time=None):
        self.writes.append((name, value, print_time))

    def get_tmc_frequency(self):
        return 12000000.0

    def last_write(self, name):
        for written, value, print_time in reversed(self.writes):
            if written == name:
                return value
        return None


class FakeDriver:
    def __init__(self):
        self.mcu_tmc = FakeTmc()
        self.fields = self.mcu_tmc.fields


class FakeForceMove:
    def __init__(self, steppers):
        self.steppers = steppers

    def lookup_stepper(self, name):
        return self.steppers[name]


class FakePins:
    def __init__(self):
        self.shared = []

    def allow_multi_use_pin(self, pin_desc):
        self.shared.append(pin_desc)


class FakeButtons:
    def __init__(self):
        self.callbacks = {}

    def register_buttons(self, pins, callback):
        self.callbacks[pins[0]] = callback


class FakeToolhead:
    """Queues moves in a real trapq, a quarter of a second ahead."""

    def __init__(self, reactor):
        self.reactor = reactor
        ffi_main, self.ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(
            self.ffi_lib.trapq_alloc(), self.ffi_lib.trapq_free
        )
        self.step_generators = []
        self.position = [100.0, 100.0, 0.0]
        self.print_time = 0.0

    def get_trapq(self):
        return self.trapq

    def register_step_generator(self, handler):
        self.step_generators.append(handler)

    # Queues a move and returns the print times its cruise starts and ends.
    def move(self, dx, dy, speed, finalize=True):
        distance = (dx**2 + dy**2) ** 0.5
        accel_t = speed / ACCEL
        cruise_t = (distance - speed * accel_t) / speed
        start = max(self.print_time, self.reactor.monotonic() + 0.25)
        self.ffi_lib.trapq_append(
            self.trapq,
            start,
            accel_t,
            cruise_t,
            accel_t,
            self.position[0],
            self.position[1],
            self.position[2],
            dx / distance,
            dy / distance,
            0.0,
            0.0,
            speed,
            ACCEL,
        )
        self.position[0] += dx
        self.position[1] += dy
        self.print_time = start + 2.0 * accel_t + cruise_t
        if finalize:
            self.ffi_lib.trapq_finalize_moves(self.trapq, self.print_time, 0.0)
        self.generate_steps()
        return start + accel_t, start + accel_t + cruise_t

    def generate_steps(self):
        for handler in self.step_generators:
            handler(self.print_time)


class Printer(PrinterShim):
    command_error = CommandError
    config_error = ConfigError

    def __init__(self):
        super().__init__({"config_file": str(CONFIG_DIR / "printer.cfg")})
        self.reactor = FakeReactor()
        self.handlers = {}
        self.shutdown_messages = []
        self.infos = []
        self.lookup_object("gcode").respond_info = self.infos.append
        self.steppers = {
            "stepper_x": FakeStepper(1.0),
            "stepper_y": FakeStepper(-1.0),
        }
        self.drivers = {name: FakeDriver() for name in self.steppers}
        self.toolhead = FakeToolhead(self.reactor)
        self.add_object("force_move", FakeForceMove(self.steppers))
        self.add_object("pins", FakePins())
        self.add_object("buttons", FakeButtons())
        self.add_object("mcu", FakeMcu())
        self.add_object("toolhead", self.toolhead)
        for name, driver in self.drivers.items():
            self.add_object("tmc2209 " + name, driver)

    def get_reactor(self):
        return self.reactor

    def load_object(self, config, name):
        return self.lookup_object(name)

    def register_event_handler(self, event, callback):
        self.handlers[event] = callback

    def send_event(self, event, *args):
        self.handlers[event](*args)

    def invoke_shutdown(self, msg):
        self.shutdown_messages.append(msg)

    def tmc(self, name):
        return self.drivers[name].mcu_tmc

    def set_readings(self, x, y):
        self.tmc("stepper_x").reading = x
        self.tmc("stepper_y").reading = y

    def reads(self):
        return sum(d.mcu_tmc.reads for d in self.drivers.values())

    # Moves along X and back, both at the given speed, and lets the moves
    # run. Returns the print times the first cruise starts and ends.
    def stroke(self, speed=50.0, distance=100.0):
        cruise = self.toolhead.move(distance, 0.0, speed)
        self.toolhead.move(-distance, 0.0, speed)
        self.reactor.run_until(self.toolhead.print_time + 0.1)
        return cruise

    def pulse(self, name):
        pin = {"stepper_x": "^gpio4", "stepper_y": "^gpio16"}[name]
        callback = self.lookup_object("buttons").callbacks[pin]
        callback(self.reactor.monotonic(), 1)
        callback(self.reactor.monotonic(), 0)


def _load(printer, **options):
    config = printer.lookup_object("configfile").read_main_config()
    section = config.getsection("stall_monitor xy")
    for option, value in options.items():
        section.fileconfig.set("stall_monitor xy", option, str(value))
    monitor = stall_monitor.load_config_prefix(section)
    printer.add_object("stall_monitor xy", monitor)
    printer.send_event("klippy:connect")
    printer.send_event("klippy:ready")
    return monitor


@pytest.fixture
def printer():
    return Printer()


@pytest.fixture
def monitor(printer):
    return _load(printer)


@pytest.fixture
def polling(printer):
    return _load(printer, use_diag_pins=False)


def test_motion_key_separates_speed_and_direction():
    key = stall_monitor.get_motion_key
    assert key([50.0, 50.0]) == key([50.5, 49.8])
    assert key([50.0, 50.0]) != key([50.0, -50.0])
    assert key([50.0, 50.0]) != key([60.0, 50.0])
    assert key([50.0, 0.1]) == key([50.0, -0.2])


def test_zero_readings_are_not_learned():
    baseline = stall_monitor.MotionBaseline(3)
    key = (1, 1)
    for reading in (0, 400, 410, 420):
        baseline.add(key, reading)
    assert baseline.get_floor(key) == 400


def test_a_stretch_without_scheduled_reads_does_not_raise(printer, monitor):
    stretch = stall_monitor.Stretch(0.0, 1.0, (1, 1), [50.0, 50.0])
    assert monitor._read_due(stretch, 0.5) == float("inf")


def test_diag_pins_are_shared_with_homing(printer, monitor):
    assert printer.lookup_object("pins").shared == ["gpio4", "gpio16"]


def test_learned_motion_is_checked_by_the_driver(printer, monitor):
    printer.stroke()
    printer.stroke()
    tmc = printer.tmc("stepper_x")
    # The speed gate sits just below the 50 mm/s being checked.
    gate = stall_monitor.tmc.TMCtstepHelper(
        tmc, 45.0, pstepper=printer.steppers["stepper_x"]
    )
    assert ("TCOOLTHRS", gate) in [write[:2] for write in tmc.writes]
    reads = printer.reads()
    tmc.writes.clear()
    start, end = printer.stroke()
    armed, silenced = tmc.writes[:2]
    assert armed[:2] == ("SGTHRS", ARMED_THRESHOLD)
    assert armed[2] == pytest.approx(start + 0.1, abs=0.002)
    assert silenced[:2] == ("SGTHRS", 0)
    assert silenced[2] == pytest.approx(end - stall_monitor.GUARD_TIME)
    # The host only takes the occasional reading to keep the motion current.
    assert printer.reads() - reads < 100
    assert monitor.get_status(0.0)["checked_time"] > 5.0


def test_queued_move_is_checked_before_it_is_finished(printer, monitor):
    printer.stroke()
    # A long move stays in the queue until its steps are generated, which
    # is about its end.
    start, end = printer.toolhead.move(100.0, 0.0, 10.0, finalize=False)
    printer.toolhead.move(-100.0, 0.0, 50.0, finalize=False)
    start, end = printer.toolhead.move(100.0, 0.0, 50.0, finalize=False)
    printer.reactor.run_until(start + 0.5)
    assert printer.tmc("stepper_x").last_write("SGTHRS") == ARMED_THRESHOLD
    printer.pulse("stepper_x")
    printer.pulse("stepper_x")
    assert len(printer.shutdown_messages) == 1


def test_pin_pulses_shut_printer_down(printer, monitor):
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.pulse("stepper_x")
    assert printer.shutdown_messages == []
    printer.pulse("stepper_y")
    assert len(printer.shutdown_messages) == 1
    # Moonraker's auto_recovery leaves a "Shutdown due to" shutdown alone.
    assert printer.shutdown_messages[0].startswith(
        "Shutdown due to stall on stepper_y at 50.0 mm/s"
    )
    assert monitor.get_status(0.0)["stalls"] == 1


def test_pin_pulses_outside_a_checked_stretch_are_ignored(printer, monitor):
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.05)
    # Not learned yet
    printer.pulse("stepper_x")
    printer.pulse("stepper_x")
    printer.reactor.run_until(end + 0.2)
    # Learned, but the stretch is over
    printer.pulse("stepper_x")
    printer.pulse("stepper_x")
    assert printer.shutdown_messages == []


def test_homing_gets_its_driver_settings_back(printer, monitor):
    printer.stroke()
    tmc = printer.tmc("stepper_x")
    assert tmc.last_write("SGTHRS") == 0
    printer.send_event("homing:homing_move_begin", None)
    assert tmc.last_write("SGTHRS") == HOMING_THRESHOLD
    assert tmc.last_write("TCOOLTHRS") == HOMING_GATE
    # The homing move itself and its pulses are not checked.
    tmc.writes.clear()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.pulse("stepper_x")
    printer.pulse("stepper_x")
    printer.reactor.run_until(end + 0.1)
    printer.send_event("homing:homing_move_end", None)
    assert tmc.writes == []
    assert printer.shutdown_messages == []
    # Checking resumes with the next move.
    printer.toolhead.move(-100.0, 0.0, 50.0)
    printer.stroke()
    assert tmc.last_write("SGTHRS") == 0
    assert ("SGTHRS", ARMED_THRESHOLD) in [w[:2] for w in tmc.writes]


def test_rest_of_a_cut_short_homing_move_is_not_checked(printer, polling):
    printer.stroke()
    printer.toolhead.move(-100.0, 0.0, 50.0)
    printer.reactor.run_until(printer.toolhead.print_time + 0.1)
    # The endstop triggers a third of the way into a learned motion. The
    # steppers stop there, so their readings no longer mean anything.
    printer.send_event("homing:homing_move_begin", None)
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.6)
    printer.send_event("homing:homing_move_end", None)
    printer.toolhead.generate_steps()
    reads = printer.reads()
    printer.set_readings(NORMAL // 2, NORMAL // 2)
    printer.reactor.run_until(end + 0.1)
    assert printer.reads() == reads
    assert printer.shutdown_messages == []


def test_homing_during_a_checked_stretch(printer, monitor):
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.send_event("homing:homing_move_begin", None)
    assert printer.tmc("stepper_x").last_write("SGTHRS") == HOMING_THRESHOLD
    printer.reactor.run_until(end + 0.1)
    assert printer.tmc("stepper_x").last_write("SGTHRS") == HOMING_THRESHOLD


def test_short_stretch_is_skipped(printer, monitor):
    printer.stroke(distance=5.0)
    assert printer.reads() == 0


def test_slow_and_still_steppers_are_not_checked(printer, monitor):
    printer.stroke(speed=5.0, distance=10.0)
    assert printer.reads() == 0
    # A diagonal turns only one motor of a CoreXY printer.
    for _ in range(2):
        printer.toolhead.move(60.0, 60.0, 50.0)
        printer.toolhead.move(-60.0, -60.0, 50.0)
        printer.reactor.run_until(printer.toolhead.print_time + 0.1)
    assert printer.tmc("stepper_x").reads > 0
    assert printer.tmc("stepper_y").reads == 0
    assert printer.tmc("stepper_y").writes == []


def test_unlearned_direction_is_not_checked(printer, monitor):
    printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(printer.toolhead.print_time + 0.1)
    tmc = printer.tmc("stepper_x")
    tmc.writes.clear()
    # The same speed in the other direction has readings of its own.
    start, end = printer.toolhead.move(-100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.15)
    assert tmc.writes == []


def test_polling_checks_readings_on_the_host(printer, polling):
    assert printer.lookup_object("buttons").callbacks == {}
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.set_readings(NORMAL // 2, NORMAL // 2)
    printer.reactor.run_until(start + 0.52)
    assert printer.shutdown_messages[0].startswith("Shutdown due to stall on")
    assert printer.tmc("stepper_x").writes == []


def test_polling_ignores_a_single_low_reading(printer, polling):
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.set_readings(NORMAL // 2, NORMAL)
    printer.reactor.run_until(start + 0.509)
    printer.set_readings(NORMAL, NORMAL)
    printer.reactor.run_until(end + 0.1)
    assert printer.shutdown_messages == []
    assert polling.get_status(0.0)["lowest_ratio"]["stepper_x"] == 0.5


def test_polling_accepts_a_small_dip(printer, polling):
    printer.stroke()
    printer.set_readings(int(NORMAL * 0.8), int(NORMAL * 0.8))
    printer.stroke()
    assert printer.shutdown_messages == []


def test_sample_interval_is_the_time_between_readings(printer):
    _load(printer, use_diag_pins=False, sample_interval=0.004)
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.1)
    reads = printer.tmc("stepper_x").reads
    printer.reactor.run_until(start + 0.5)
    assert printer.tmc("stepper_x").reads - reads == pytest.approx(100, abs=2)


def test_learning_takes_a_second_and_a_half(printer, monitor):
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.1 + 1.4)
    assert printer.tmc("stepper_x").writes == []
    printer.reactor.run_until(start + 0.1 + 1.6)
    assert printer.tmc("stepper_x").last_write("SGTHRS") == ARMED_THRESHOLD


def test_host_readings_do_not_trip_a_driver_checked_stepper(printer, monitor):
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.set_readings(NORMAL // 2, NORMAL // 2)
    printer.reactor.run_until(end + 0.1)
    assert printer.shutdown_messages == []
    assert monitor.get_status(0.0)["lowest_ratio"]["stepper_x"] == 0.5
    # The low readings were learned, so the next threshold is lower.
    printer.toolhead.move(-100.0, 0.0, 50.0)
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    assert printer.tmc("stepper_x").last_write("SGTHRS") == ARMED_THRESHOLD // 2


def test_report_action_does_not_shut_down(printer):
    _load(printer, action="report")
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    printer.pulse("stepper_x")
    printer.pulse("stepper_x")
    assert printer.shutdown_messages == []
    assert printer.infos == [
        "stall_monitor xy: stall on stepper_x at 50.0 mm/s"
    ]


def test_read_error_is_skipped(printer, polling):
    printer.stroke()
    for driver in printer.drivers.values():
        driver.mcu_tmc.fail = True
    printer.stroke()
    assert printer.shutdown_messages == []


def test_set_stall_monitor(printer, monitor):
    printer.stroke()
    start, end = printer.toolhead.move(100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.5)
    tmc = printer.tmc("stepper_x")
    assert tmc.last_write("SGTHRS") == ARMED_THRESHOLD
    printer.call("SET_STALL_MONITOR MONITOR=xy ENABLE=0")
    assert tmc.last_write("SGTHRS") == 0
    printer.pulse("stepper_x")
    printer.pulse("stepper_x")
    reads = printer.reads()
    printer.reactor.run_until(end + 0.1)
    printer.stroke()
    assert printer.reads() == reads
    printer.call("SET_STALL_MONITOR MONITOR=xy ENABLE=1 ACTION=report CLEAR=1")
    assert monitor.get_status(0.0)["lowest_ratio"]["stepper_x"] is None
    assert monitor.get_status(0.0)["checked_time"] == 0.0
    # The learned readings are gone, so the motion is learned again.
    tmc.writes.clear()
    start, end = printer.toolhead.move(-100.0, 0.0, 50.0)
    printer.reactor.run_until(start + 0.15)
    assert tmc.writes == []
    assert printer.shutdown_messages == []
    with pytest.raises(Exception, match="ACTION must be one of"):
        printer.call("SET_STALL_MONITOR MONITOR=xy ACTION=pause")


def test_query_stall_monitor(printer, monitor):
    printer.call("QUERY_STALL_MONITOR MONITOR=xy")
    assert (
        "stepper_x (checked by the driver): no readings yet"
        in printer.infos[-1]
    )
    printer.stroke()
    printer.stroke()
    printer.call("QUERY_STALL_MONITOR MONITOR=xy")
    assert "lowest reading was 1.00 of the normal low" in printer.infos[-1]


def test_spreadcycle_driver_is_rejected(printer):
    printer.drivers["stepper_y"].fields.values["en_spreadcycle"] = 1
    with pytest.raises(ConfigError, match="must run in stealthChop"):
        _load(printer)


def test_missing_driver_is_rejected(printer):
    del printer.objects["tmc2209 stepper_x"]
    with pytest.raises(ConfigError, match=r"needs a \[tmc2209 stepper_x\]"):
        _load(printer)


def test_shutdown_stops_checking(printer, monitor):
    printer.send_event("klippy:shutdown")
    assert printer.reactor.timers == []
