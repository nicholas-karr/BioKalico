import logging

from klippy.printer import Printer


class _FakeReactor:
    def register_callback(self, callback):
        pass

    def monotonic(self):
        return 0.0

    def run(self):
        pass


def _make_printer(run_result):
    printer = Printer.__new__(Printer)
    printer.reactor = _FakeReactor()
    printer.event_handlers = {}
    printer.run_result = run_result
    return printer


def test_disconnect_runs_when_firmware_restart_handler_fails(caplog):
    # An arduino reset on an unplugged MCU raises from the firmware_restart
    # handler; webhooks must still get klippy:disconnect to close its sockets.
    printer = _make_printer("firmware_restart")
    calls = []

    def failing_restart():
        calls.append("restart_fail")
        raise OSError("could not open port")

    printer.register_event_handler("klippy:firmware_restart", failing_restart)
    printer.register_event_handler(
        "klippy:firmware_restart", lambda: calls.append("restart_ok")
    )
    printer.register_event_handler(
        "klippy:disconnect", lambda: calls.append("disconnect")
    )

    with caplog.at_level(logging.ERROR):
        assert printer.run() == "firmware_restart"
    assert calls == ["restart_fail", "restart_ok", "disconnect"]
    assert "Unhandled exception during post run" in caplog.text


def test_failing_disconnect_handler_does_not_skip_others():
    printer = _make_printer("restart")
    calls = []

    def failing_disconnect():
        raise RuntimeError("boom")

    printer.register_event_handler(
        "klippy:firmware_restart", lambda: calls.append("restart")
    )
    printer.register_event_handler("klippy:disconnect", failing_disconnect)
    printer.register_event_handler(
        "klippy:disconnect", lambda: calls.append("disconnect")
    )

    assert printer.run() == "restart"
    assert calls == ["disconnect"]
