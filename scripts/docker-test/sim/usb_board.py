#!/usr/bin/env python3
"""Plug the simulated non-critical board of the sim printer in and out.

Klippy decides a non-critical MCU is present by opening its serial port, and a
pseudo-terminal opens like one. `plug` creates one, links it at the path the
sim config uses, and keeps it alive in the background; `unplug` removes it.
"""

import os
import pty
import signal
import sys
import time

LINK = "/tmp/sim-mcu-usb"
PIDFILE = "/tmp/sim-mcu-usb.pid"


def unplug():
    if os.path.exists(PIDFILE):
        try:
            os.kill(int(open(PIDFILE).read()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        os.unlink(PIDFILE)
    if os.path.islink(LINK) or os.path.exists(LINK):
        os.unlink(LINK)


def plug():
    unplug()
    if os.fork():
        # Wait until the child has created the link, so the caller can rely on it.
        for _ in range(50):
            if os.path.islink(LINK):
                return
            time.sleep(0.1)
        sys.exit("the pty did not appear")
    os.setsid()
    # Do not hold the caller's pipes open, or a caller that captures output
    # waits for this process to exit.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    master, slave = pty.openpty()
    os.symlink(os.ttyname(slave), LINK)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:  # the master must stay open, or the slave vanishes
        time.sleep(3600)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("plug", "unplug"):
        sys.exit("usage: usb_board.py plug|unplug")
    plug() if sys.argv[1] == "plug" else unplug()
