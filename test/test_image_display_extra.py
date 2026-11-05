import queue

import pytest

from klippy.extras.image_display import _STANDBY_DELAY, ImageDisplay


def _bare_image_display(auth_token):
    obj = ImageDisplay.__new__(ImageDisplay)
    obj.auth_token = auth_token
    obj._cmd_queue = queue.Queue()
    obj._standby_timer = None
    obj._standby_waketime = 0.0
    obj._idled_off = False
    return obj


def test_send_fire_and_forget_attaches_auth_token():
    # Commands queued without waiting for a reply (PROJECTOR_ON's retry and
    # the standby auto-off) must still carry auth_token.
    obj = _bare_image_display("secret-token")
    obj._send_fire_and_forget({"type": "PROJECTOR_ON"})

    cmd, result_q = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_ON", "auth_token": "secret-token"}
    assert isinstance(result_q, queue.Queue)


def test_send_fire_and_forget_omits_token_when_unset():
    obj = _bare_image_display("")
    obj._send_fire_and_forget({"type": "PROJECTOR_OFF"})

    cmd, _ = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_OFF"}


class _FakeReactor:
    NEVER = float("inf")
    NOW = 0.0

    def __init__(self):
        self.callbacks = []
        self.timers = {}
        self.now = 0.0

    def register_timer(self, cb, waketime=NEVER):
        handle = object()
        self.timers[handle] = (cb, waketime)
        return handle

    def update_timer(self, handle, waketime):
        self.timers[handle] = (self.timers[handle][0], waketime)

    def unregister_timer(self, handle):
        del self.timers[handle]

    def register_callback(self, cb):
        self.callbacks.append(cb)

    def monotonic(self):
        return self.now

    def pause(self, t):
        self.now = t
        return t


class _FakeGcode:
    def __init__(self):
        self.infos = []

    def respond_info(self, msg):
        self.infos.append(msg)


class _FakePrintStats:
    def __init__(self):
        self.state = "standby"

    def get_status(self, eventtime):
        return {"state": self.state}


class _FakePrinter:
    def __init__(self):
        self.reactor = _FakeReactor()
        self.gcode = _FakeGcode()
        self.print_stats = _FakePrintStats()

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == "print_stats":
            return self.print_stats
        return self.gcode


def _abortable_display():
    obj = _bare_image_display("")
    obj.printer = _FakePrinter()
    obj.timeout = 1.0
    return obj


def test_aborted_print_queues_clear_before_returning():
    # The CLEAR must already be queued when the event handler returns so it
    # stays ahead of any command from a following print.
    obj = _abortable_display()
    obj._on_print_aborted()

    cmd, _ = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "CLEAR"}
    assert len(obj.printer.reactor.callbacks) == 1


def test_aborted_print_drops_the_pause_reason():
    # Stop Print after a projector timeout paused the print must not leave
    # the explanation showing in Mainsail.
    obj = _abortable_display()
    obj._pause_reason = "PROJECTOR_ON: timed out"
    obj._on_print_aborted()
    assert obj._pause_reason is None


def test_aborted_print_clear_retries_then_succeeds():
    obj = _abortable_display()
    obj._on_print_aborted()
    _, first_q = obj._cmd_queue.get_nowait()
    first_q.put(("err", OSError("boom")))

    # Answer the retry as soon as the confirm loop queues it.
    class _Reply(queue.Queue):
        def put(self, item, *a, **k):
            super().put(item, *a, **k)
            cmd, result_q = self.get_nowait()
            assert cmd == {"type": "CLEAR"}
            result_q.put(("ok", {"status": "success"}))

    obj._cmd_queue = _Reply()
    obj.printer.reactor.callbacks[0](0.0)
    assert obj.printer.gcode.infos == []


def test_aborted_print_clear_warns_when_all_attempts_fail():
    obj = _abortable_display()
    obj._on_print_aborted()
    _, first_q = obj._cmd_queue.get_nowait()
    first_q.put(("ok", {"status": "error", "message": "no window"}))

    class _Fail(queue.Queue):
        def put(self, item, *a, **k):
            _, result_q = item
            result_q.put(("err", OSError("down")))

    obj._cmd_queue = _Fail()
    obj.printer.reactor.callbacks[0](0.0)
    assert len(obj.printer.gcode.infos) == 1
    assert "CLEAR_DISPLAY" in obj.printer.gcode.infos[0]


def _armed_wake(obj):
    ((cb, waketime),) = obj.printer.reactor.timers.values()
    return cb, waketime


def test_aborted_print_arms_standby():
    obj = _abortable_display()
    obj._on_print_aborted()

    _, waketime = _armed_wake(obj)
    assert waketime == _STANDBY_DELAY


def test_command_restarts_idle_countdown():
    obj = _abortable_display()
    obj._arm_standby()
    obj.printer.reactor.now = 100.0
    obj._arm_standby()

    _, waketime = _armed_wake(obj)
    assert waketime == 100.0 + _STANDBY_DELAY


def test_idle_expiry_sends_projector_off_once():
    obj = _abortable_display()
    obj._arm_standby()
    cb, waketime = _armed_wake(obj)

    assert cb(waketime) == obj.printer.reactor.NEVER
    cmd, _ = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_OFF"}
    assert obj._standby_timer is None


def test_idle_expiry_waits_while_print_is_printing():
    obj = _abortable_display()
    obj.printer.print_stats.state = "printing"
    obj._arm_standby()
    cb, waketime = _armed_wake(obj)

    assert cb(waketime) == waketime + _STANDBY_DELAY
    assert obj._cmd_queue.empty()
    assert not obj._idled_off


def test_idle_expiry_turns_off_while_print_is_paused():
    obj = _abortable_display()
    obj.printer.print_stats.state = "paused"
    obj._arm_standby()
    cb, waketime = _armed_wake(obj)

    assert cb(waketime) == obj.printer.reactor.NEVER
    cmd, _ = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_OFF"}
    assert obj._idled_off


class _Gcmd:
    def __init__(self):
        self.infos = []

    def respond_info(self, msg):
        self.infos.append(msg)


def test_image_command_wakes_projector_after_idle_off():
    obj = _abortable_display()
    obj._idled_off = True
    powered = []
    obj._power_on = lambda gcmd: (
        powered.append(gcmd),
        setattr(obj, "_idled_off", False),
    )
    gcmd = _Gcmd()

    assert obj._wake_if_idled_off(gcmd)
    assert powered == [gcmd]
    assert obj._wake_if_idled_off(gcmd)
    assert powered == [gcmd]


def test_image_command_skipped_when_wake_leaves_projector_off():
    # PROJECTOR_ON paused the print instead of finishing: nothing may be
    # shown on a projector that is not ready.
    obj = _abortable_display()
    obj._idled_off = True
    obj._power_on = lambda gcmd: None

    assert not obj._wake_if_idled_off(_Gcmd())


def test_projector_off_command_does_not_rearm_standby():
    obj = _abortable_display()
    obj._arm_standby()
    # _send posts to the worker; none runs here, so it times out quickly.
    obj._send({"type": "PROJECTOR_OFF"})

    assert obj.printer.reactor.timers == {}


class _Gcmd:
    error = RuntimeError

    def __init__(self, **params):
        self.params = params

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))


def test_plate_rect_rides_on_every_displayed_image():
    obj = _bare_image_display("")
    obj.h_offset = 0.0
    obj.plate_rect = None
    sent = []
    obj._send = lambda cmd, timeout=None: sent.append(cmd) or {
        "status": "success"
    }

    obj.cmd_DISPLAY_IMAGE(_Gcmd(PATH="/a.png"))
    assert sent[-1]["plate_rect"] is None

    obj.set_plate_rect((10, 20, 300, 200))
    obj.cmd_DISPLAY_IMAGE(_Gcmd(PATH="/a.png"))
    assert sent[-1]["plate_rect"] == [10, 20, 300, 200]

    obj.set_plate_rect(None)
    obj.cmd_DISPLAY_IMAGE(_Gcmd(PATH="/a.png"))
    assert sent[-1]["plate_rect"] is None


def test_calibration_pattern_command_shape():
    obj = _bare_image_display("")
    sent = []
    obj._send = lambda cmd, timeout=None: sent.append(cmd) or {
        "status": "success"
    }

    obj.show_calibration_pattern(_Gcmd(), "focus")
    obj.show_calibration_pattern(_Gcmd(), "rect", (1, 2, 3, 4))
    assert sent == [
        {"type": "CALIBRATION_PATTERN", "pattern": "focus"},
        {
            "type": "CALIBRATION_PATTERN",
            "pattern": "rect",
            "rect": [1, 2, 3, 4],
        },
    ]


def _warming_display(ready=False):
    obj = _abortable_display()
    obj._warmup_timer = None
    obj._warmup_active = False
    obj._warmup_deadline = 0.0
    obj._warmup_last_kick = -1e9
    obj._nudge_thread = None
    obj._startup_timeout = 100.0
    obj._status_cache = {"display_ready": ready}
    obj._pause_reason = None
    obj.screen_detect_path = None
    obj._nudge_screen_in_background = lambda: None
    obj._arm_standby = lambda *a, **k: None
    return obj


class _WaitGcmd:
    error = RuntimeError

    def __init__(self, wait):
        self.wait = wait
        self.infos = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        assert name == "WAIT"
        return self.wait

    def respond_info(self, msg):
        self.infos.append(msg)


def test_background_start_returns_at_once_and_keeps_kicking_until_ready():
    obj = _warming_display()
    obj._power_on = lambda gcmd: pytest.fail("must not wait")
    reactor = obj.printer.reactor
    obj.cmd_PROJECTOR_ON(_WaitGcmd(0))
    assert obj._warmup_active
    (cb, wake) = next(iter(reactor.timers.values()))
    assert wake == reactor.NOW

    reactor.now = 1.0
    assert cb(1.0) == 1.0 + 5.0
    cmd, _ = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_ON"}

    # Not due again yet: no second kick.
    reactor.now = 2.0
    cb(2.0)
    assert obj._cmd_queue.empty()

    obj._idled_off = True
    obj._status_cache["display_ready"] = True
    assert cb(3.0) == reactor.NEVER
    assert not obj._warmup_active
    assert not obj._idled_off


def test_background_start_gives_up_at_the_startup_timeout_and_can_restart():
    obj = _warming_display()
    reactor = obj.printer.reactor
    obj.cmd_PROJECTOR_ON(_WaitGcmd(0))
    cb, _ = next(iter(reactor.timers.values()))
    reactor.now = 101.0
    assert cb(101.0) == reactor.NEVER
    assert not obj._warmup_active

    obj.cmd_PROJECTOR_ON(_WaitGcmd(0))
    assert obj._warmup_active
    assert len(reactor.timers) == 1


def test_background_start_does_nothing_when_already_ready_or_running():
    ready = _warming_display(ready=True)
    gcmd = _WaitGcmd(0)
    ready.cmd_PROJECTOR_ON(gcmd)
    assert not ready._warmup_active
    assert "ready" in gcmd.infos[0]

    obj = _warming_display()
    obj.cmd_PROJECTOR_ON(_WaitGcmd(0))
    obj.cmd_PROJECTOR_ON(_WaitGcmd(0))
    assert len(obj.printer.reactor.timers) == 1


def test_waiting_start_is_still_the_default():
    obj = _warming_display()
    called = []
    obj._power_on = lambda gcmd: called.append(gcmd)
    obj.cmd_PROJECTOR_ON(_WaitGcmd(1))
    assert len(called) == 1
    assert not obj._warmup_active
