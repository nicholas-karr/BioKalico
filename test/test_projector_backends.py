import termios
import time

import pytest

from scripts.sla import projector_backends
from scripts.sla.projector_backends import (
    NullProjectorBackend,
    OptomaProjectorBackend,
    build_projector_backend,
)

#: A tcgetattr() result holding typical terminal defaults, which
#: _configure_port must overwrite.
_DEFAULT_ATTRS = [
    termios.ICRNL | termios.IXON,
    termios.OPOST | termios.ONLCR,
    termios.CS8 | termios.CREAD | termios.CRTSCTS,
    termios.ECHO | termios.ICANON | termios.ISIG,
    termios.B9600,
    termios.B9600,
    [0] * 32,
]


class _FakeSerial:
    """Replaces the os, select and termios calls in projector_backends so
    OptomaProjectorBackend can run without a serial device.

    reply is bytes, or a list of bytes returned one read() at a time.
    """

    FD = 99

    def __init__(self, monkeypatch, reply: bytes = b"", readable: bool = True):
        self.reply = reply
        self.readable = readable
        self.written = []
        self.flushed = []
        self.closed = []
        self.attrs_set = []
        monkeypatch.setattr(
            projector_backends.os, "open", lambda *a, **k: self.FD
        )
        monkeypatch.setattr(projector_backends.os, "write", self._write)
        monkeypatch.setattr(projector_backends.os, "read", self._read)
        monkeypatch.setattr(
            projector_backends.termios,
            "tcgetattr",
            lambda fd: list(_DEFAULT_ATTRS),
        )
        monkeypatch.setattr(
            projector_backends.termios,
            "tcsetattr",
            lambda fd, when, attrs: self.attrs_set.append(attrs),
        )
        monkeypatch.setattr(
            projector_backends.os, "close", lambda fd: self.closed.append(fd)
        )
        monkeypatch.setattr(
            projector_backends.select,
            "select",
            lambda *a, **k: (
                ([self.FD], [], []) if self.readable else ([], [], [])
            ),
        )
        monkeypatch.setattr(
            projector_backends.termios,
            "tcflush",
            lambda fd, which: self.flushed.append((fd, which)),
        )

    def _write(self, fd, data):
        self.written.append(data)

    def _read(self, fd, n):
        if isinstance(self.reply, list):
            return self.reply.pop(0) if self.reply else b""
        return self.reply


def _backend(**config_overrides):
    config = {"projector_device": "/dev/ttyFake"}
    config.update(config_overrides)
    return OptomaProjectorBackend(config)


def test_build_projector_backend_disabled_is_null():
    backend = build_projector_backend({"projector_control": False})
    assert isinstance(backend, NullProjectorBackend)
    assert backend.enabled is False
    assert backend.query_power() is None
    backend.turn_on()
    backend.turn_off()


def test_build_projector_backend_defaults_to_optoma():
    backend = build_projector_backend({"projector_device": "/dev/ttyFake"})
    assert isinstance(backend, OptomaProjectorBackend)


def test_build_projector_backend_unknown_protocol_raises():
    with pytest.raises(ValueError, match="unknown projector_protocol"):
        build_projector_backend(
            {"projector_protocol": "bogus", "projector_device": "/dev/ttyFake"}
        )


def test_optoma_backend_requires_device_path():
    with pytest.raises(ValueError, match="projector_device"):
        OptomaProjectorBackend({})


def test_query_power_parses_ok_digit(monkeypatch):
    fake = _FakeSerial(monkeypatch, reply=b"Ok1\r")
    backend = _backend()
    assert backend.query_power() is True
    assert backend.last_error is None

    fake.reply = b"Ok0\r"
    assert backend.query_power() is False


def test_query_power_unrecognized_reply_sets_last_error(monkeypatch):
    _FakeSerial(monkeypatch, reply=b"P\rINFO2\r")
    backend = _backend()
    assert backend.query_power() is None
    assert "unrecognized power-status reply" in backend.last_error


def test_query_power_fail_ack_is_not_a_link_error(monkeypatch):
    """'F' means the projector declined (normal in standby), not a link error."""
    _FakeSerial(monkeypatch, reply=b"F\r")
    backend = _backend()
    assert backend.query_power() is None
    assert backend.last_error is None
    assert "declined the power-status query" in backend.last_declined


def test_query_power_ok_reply_clears_a_stale_decline(monkeypatch):
    fake = _FakeSerial(monkeypatch, reply=b"F\r")
    backend = _backend()
    backend.query_power()
    assert backend.last_declined is not None

    fake.reply = b"Ok1\r"
    assert backend.query_power() is True
    assert backend.last_declined is None


def test_configure_port_disables_echo_and_cr_translation(monkeypatch):
    """ECHO causes an endless 'F' loop and ICRNL breaks reply framing."""
    fake = _FakeSerial(monkeypatch, reply=b"Ok1\r")
    backend = _backend()
    backend.query_power()

    assert len(fake.attrs_set) == 1
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = fake.attrs_set[0]
    assert not lflag & termios.ECHO
    assert not lflag & termios.ICANON
    assert not iflag & termios.ICRNL
    assert not oflag & termios.OPOST
    assert not cflag & termios.CRTSCTS
    assert cflag & termios.CS8
    assert ispeed == ospeed == termios.B9600
    assert cc[termios.VMIN] == 0
    assert cc[termios.VTIME] == 0


def test_reply_split_across_reads_is_reassembled(monkeypatch):
    """A short first read must not truncate 'Ok1' to 'Ok'."""
    _FakeSerial(monkeypatch, reply=[b"Ok", b"1\r"])
    backend = _backend()
    assert backend.query_power() is True
    assert backend.last_error is None


def test_turn_off_sends_value_2_not_0(monkeypatch):
    fake = _FakeSerial(monkeypatch, reply=b"P\r")
    backend = _backend()
    backend.turn_off()
    assert fake.written == [b"~0000 2\r"]
    assert backend.last_error is None


def test_turn_on_sends_value_1(monkeypatch):
    fake = _FakeSerial(monkeypatch, reply=b"P\r")
    backend = _backend()
    backend.turn_on()
    assert fake.written == [b"~0000 1\r"]


def test_check_send_ack_fail_ack_is_not_a_link_error(monkeypatch):
    """A declined command (e.g. OFF landing on an already-off projector)
    proves the link works, so it must not read as a link fault."""
    _FakeSerial(monkeypatch, reply=b"F\r")
    backend = _backend()
    backend.turn_off()
    assert backend.last_error is None
    assert "declined the power-off command" in backend.last_declined


def test_check_send_ack_pass_clears_a_stale_decline(monkeypatch):
    fake = _FakeSerial(monkeypatch, reply=b"F\r")
    backend = _backend()
    backend.turn_off()
    assert backend.last_declined is not None

    fake.reply = b"P\r"
    backend.turn_off()
    assert backend.last_declined is None


def test_transact_flushes_input_before_writing(monkeypatch):
    fake = _FakeSerial(monkeypatch, reply=b"Ok1\r")
    backend = _backend()
    backend.query_power()
    assert fake.flushed == [
        (_FakeSerial.FD, projector_backends.termios.TCIFLUSH)
    ]


def test_transact_open_failure_sets_last_error(monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(projector_backends.os, "open", raise_oserror)
    backend = _backend()
    assert backend.query_power() is None
    assert "cannot open" in backend.last_error


def test_transact_no_reply_times_out(monkeypatch):
    _FakeSerial(monkeypatch, reply=b"", readable=False)
    backend = _backend()
    assert backend.query_power() is None
    assert "no response from" in backend.last_error


def test_last_error_clears_once_the_link_works_again(monkeypatch):
    """A recovered link must stop reporting the old failure."""

    def raise_oserror(*a, **k):
        raise OSError("no such device")

    monkeypatch.setattr(projector_backends.os, "open", raise_oserror)
    backend = _backend()
    assert backend.query_power() is None
    assert backend.last_error is not None

    # Adapter comes back.
    _FakeSerial(monkeypatch, reply=b"Ok1\r")
    assert backend.query_power() is True
    assert backend.last_error is None


def test_transactions_are_serialized(monkeypatch):
    """Overlapping transactions would read each other's replies."""
    import threading

    fake = _FakeSerial(monkeypatch, reply=b"Ok1\r")
    backend = _backend()
    overlaps = []
    active = []

    def slow_read(fd, n):
        active.append(1)
        if len(active) > 1:
            overlaps.append(1)
        time.sleep(0.02)
        active.pop()
        return fake.reply

    monkeypatch.setattr(projector_backends.os, "read", slow_read)
    threads = [threading.Thread(target=backend.query_power) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == []


def test_termios_failure_is_reported_as_a_link_error(monkeypatch):
    """An adapter yanked mid-transaction must not kill the calling thread."""
    _FakeSerial(monkeypatch, reply=b"Ok1\r")

    def raise_termios(fd):
        raise termios.error(9, "Bad file descriptor")

    monkeypatch.setattr(projector_backends.termios, "tcgetattr", raise_termios)
    backend = _backend()
    assert backend.query_power() is None
    assert "communication with" in backend.last_error
