#!/usr/bin/env python3
"""Projector power-control backends for the SLA image display server.

To support another projector, add a ProjectorBackend subclass and register
it in _BACKENDS.
"""

from __future__ import annotations

import logging
import os
import select
import termios
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ProjectorBackend:
    """Interface every projector/screen power-control backend implements."""

    #: Name used in log and error messages, e.g. "serial adapter /dev/ttyUSB0".
    description: str = "projector"

    #: False for a backend with no power link (NullProjectorBackend).
    enabled: bool = True

    #: Description of the latest link failure (port won't open, no reply,
    #: garbled reply), or None if the link works. Monitoring watches this.
    last_error: Optional[str] = None

    #: Description of the latest command the projector refused (e.g. power
    #: off while already off), or None. Not a fault: the link worked.
    last_declined: Optional[str] = None

    def turn_on(self) -> None:
        raise NotImplementedError

    def turn_off(self) -> None:
        raise NotImplementedError

    def query_power(self) -> Optional[bool]:
        """Return True/False for a known power state, or None if unknown."""
        raise NotImplementedError


class NullProjectorBackend(ProjectorBackend):
    """No-op backend for projector_control: false (a screen with no power
    link). PROJECTOR_ON then only waits for the video output to appear.
    """

    enabled = False
    description = "no projector power control configured"

    def turn_on(self) -> None:
        pass

    def turn_off(self) -> None:
        pass

    def query_power(self) -> Optional[bool]:
        return None


class OptomaProjectorBackend(ProjectorBackend):
    """Serial power control for Optoma projectors (tested on a UHD38x).

    Commands are '~<id><function> <value>\\r' (id 00 = all). Power is
    function 00 (1 = on, 2 = off), acked 'P' or 'F'. Power state is read
    with function 124, which replies 'Ok<digit>' or 'F'.
    """

    _POWER_QUERY = b"~00124 1\r"
    _READ_TIMEOUT = 2.0  # seconds to wait for a reply to a query

    def __init__(self, config: dict):
        device_path = config.get("projector_device")
        if not device_path:
            raise ValueError(
                "Config error: 'projector_device' is required for the "
                "'optoma' projector_protocol. Set it to the serial port for "
                "the adapter connected to the projector (e.g. /dev/ttyUSB0), "
                "or set projector_control: false to disable projector power "
                "control entirely."
            )
        self.device_path = device_path
        self.description = "serial adapter %s" % device_path
        self.last_error: Optional[str] = None
        self.last_declined: Optional[str] = None
        # Several threads use the port; overlapping transactions would read
        # each other's replies.
        self._lock = threading.Lock()

    def _transact(
        self, cmd: bytes, expect_reply: bool = True
    ) -> Optional[bytes]:
        with self._lock:
            return self._transact_locked(cmd, expect_reply)

    def _transact_locked(
        self, cmd: bytes, expect_reply: bool = True
    ) -> Optional[bytes]:
        """Send cmd and return its reply without the trailing CR.

        Input is flushed first, because the receive buffer can still hold an
        unread earlier reply or an unsolicited status line (e.g. 'INFO2').
        Returns None, and logs an error, if the port can't be opened or no
        reply arrives in time.
        """
        try:
            fd = os.open(self.device_path, os.O_RDWR | os.O_NOCTTY)
        except OSError as e:
            self.last_error = (
                "cannot open %s (%s), check that the adapter is plugged in "
                "and that 'projector_device' in the config points to the "
                "right port" % (self.device_path, e)
            )
            logger.error("PROJECTOR SERIAL LINK ERROR: %s", self.last_error)
            return None
        try:
            self._configure_port(fd)
            termios.tcflush(fd, termios.TCIFLUSH)
            os.write(fd, cmd)
            # Debug level because the link probe runs this every minute.
            logger.debug(
                "Projector command sent to %s: %r", self.device_path, cmd
            )
            if not expect_reply:
                self.last_error = None
                return None
            raw = self._read_reply(fd)
            if raw is None:
                self.last_error = (
                    "no response from %s within %.1fs, the projector may be "
                    "unplugged, powered off at the wall, or the adapter is "
                    "misconfigured" % (self.device_path, self._READ_TIMEOUT)
                )
                logger.error("PROJECTOR SERIAL LINK ERROR: %s", self.last_error)
                return None
            self.last_error = None
            reply, _, extra = raw.partition(b"\r")
            if extra.strip():
                logger.info(
                    "Projector sent additional data after its reply "
                    "(likely an unsolicited status push): %r",
                    extra,
                )
            return reply
        except (OSError, termios.error) as e:
            # termios.error (not an OSError) is raised if the adapter is
            # unplugged mid-transaction.
            self.last_error = "communication with %s failed: %s" % (
                self.device_path,
                e,
            )
            logger.error("PROJECTOR SERIAL LINK ERROR: %s", self.last_error)
            return None
        finally:
            os.close(fd)

    def _configure_port(self, fd: int) -> None:
        """Put the serial port into raw 9600 8N1 with echo off.

        The default tty settings break this protocol: ECHO sends replies back
        to the projector, which answers 'F' to each in an endless loop, and
        ICRNL turns the CR that ends each reply into NL.
        """
        attrs = termios.tcgetattr(fd)
        cc = list(attrs[6])
        cc[termios.VMIN] = 0  # read() returns what has arrived, never blocks
        cc[termios.VTIME] = 0  # the select() timeout below does the waiting
        termios.tcsetattr(
            fd,
            termios.TCSANOW,
            [
                0,  # iflag: no ICRNL/IXON/... - deliver bytes as sent
                0,  # oflag: no post-processing on what we write
                termios.CS8
                | termios.CREAD
                | termios.CLOCAL,  # 8N1, no flow control
                0,  # lflag: no ECHO, no canonical mode, no signals
                termios.B9600,  # input speed  (Optoma serial control is 9600 8N1)
                termios.B9600,  # output speed
                cc,
            ],
        )

    def _read_reply(self, fd: int) -> Optional[bytes]:
        """Read until a CR arrives or _READ_TIMEOUT passes.

        A reply can arrive in several pieces. Returns the bytes including
        the CR, or None if nothing arrived.
        """
        deadline = time.monotonic() + self._READ_TIMEOUT
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, 256)
            if not chunk:
                break
            buf += chunk
            if b"\r" in buf:
                break
        return buf or None

    def turn_on(self) -> None:
        logger.info("Turning projector on")
        self._check_send_ack("on", self._transact(b"~0000 1\r"))

    def turn_off(self) -> None:
        logger.info("Turning projector off")
        # 2 is off; 0 is not a valid value.
        self._check_send_ack("off", self._transact(b"~0000 2\r"))

    def _check_send_ack(self, action: str, reply: Optional[bytes]) -> None:
        """Check a power command's P (pass) or F (fail) ack.

        reply is None when _transact already logged a failure.
        """
        if reply is None:
            return
        if reply.strip() == b"P":
            self.last_declined = None
            return
        # The projector refused the command (e.g. off while already off).
        # The link works, so last_error is left alone.
        self.last_declined = (
            "projector on %s declined the power-%s command (replied %r "
            "instead of 'P')" % (self.device_path, action, reply)
        )
        logger.info("PROJECTOR: %s", self.last_declined)

    def query_power(self) -> Optional[bool]:
        """Return True if on, False if off, or None if unknown.

        None means the projector declined (last_declined, normal in
        standby) or the link failed (last_error).
        """
        reply = self._transact(self._POWER_QUERY, expect_reply=True)
        if reply is None:
            return None
        stripped = reply.strip()
        if stripped[:2].upper() == b"OK" and len(stripped) > 2:
            value = stripped[2:3]
            if value == b"1":
                self.last_declined = None
                return True
            if value == b"0":
                self.last_declined = None
                return False
        if stripped.upper() == b"F":
            # The projector refuses this query in standby. Not a link fault.
            self.last_declined = (
                "projector on %s declined the power-status query (replied "
                "'F')" % self.device_path
            )
            logger.info("PROJECTOR: %s", self.last_declined)
            return None
        self.last_error = "unrecognized power-status reply from %s: %r" % (
            self.device_path,
            reply,
        )
        logger.error("PROJECTOR SERIAL LINK ERROR: %s", self.last_error)
        return None


#: Backends by 'projector_protocol' config value.
_BACKENDS: dict = {
    "optoma": OptomaProjectorBackend,
}


def build_projector_backend(config: dict) -> ProjectorBackend:
    """Build the backend selected by the parsed config dict.

    projector_control: false gives NullProjectorBackend. Otherwise
    projector_protocol (default 'optoma') picks from _BACKENDS, and the
    backend reads any other keys it needs, such as projector_device.
    """
    if not bool(config.get("projector_control", True)):
        return NullProjectorBackend()
    protocol = config.get("projector_protocol", "optoma")
    try:
        backend_cls = _BACKENDS[protocol]
    except KeyError:
        raise ValueError(
            "Config error: unknown projector_protocol %r, must be one of: %s"
            % (protocol, ", ".join(sorted(_BACKENDS)))
        )
    return backend_cls(config)
