"""Tests for _projector_transition_pending in scripts/sla/image-display.py.

The projector reports ON while cooling down after a power-off. Readings
taken then must not set _projector_on back to True, or the idle check sends
a second power-off.

Skipped when pyglet can't import (no display).
"""

import asyncio
import importlib.util
import os
import sys
import time

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "sla", "image-display.py"
)


def _load_image_display_module():
    sla_dir = os.path.dirname(_MODULE_PATH)
    if sla_dir not in sys.path:
        sys.path.insert(0, sla_dir)
    spec = importlib.util.spec_from_file_location(
        "_image_display_under_test_transition", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    image_display = _load_image_display_module()
    _import_error = None
except Exception as exc:  # pragma: no cover - environment-dependent
    image_display = None
    _import_error = exc

pytestmark = pytest.mark.skipif(
    image_display is None,
    reason="scripts/sla/image-display.py needs a display to import "
    f"(pyglet): {_import_error!r}",
)


class _FakeBackend:
    """Stand-in for OptomaProjectorBackend: no serial link, just a script."""

    enabled = True
    description = "fake projector"

    def __init__(self):
        self.last_error = None
        self.query_power_return = None
        self.turn_off_calls = 0
        self.turn_on_calls = 0

    def turn_on(self):
        self.turn_on_calls += 1

    def turn_off(self):
        self.turn_off_calls += 1

    def query_power(self):
        return self.query_power_return


def _make_server(tmp_path):
    config = {
        "host": "127.0.0.1",
        "port": 0,
        "auth_token": "",
        "allow_remote_image_urls": False,
        "allow_private_image_urls": False,
        "max_download_bytes": 1024 * 1024,
        "projector_control": False,  # replaced with _FakeBackend right after
        "video_cache_dir": str(tmp_path / "cache"),
    }
    server = image_display.ImageDisplayServer(config)
    server.projector = _FakeBackend()
    return server


def _idle_since(server, seconds_ago):
    server._last_content_change = time.monotonic() - seconds_ago


def test_confirm_sets_and_clears_transition_pending(tmp_path, monkeypatch):
    server = _make_server(tmp_path)
    monkeypatch.setattr(
        image_display, "PROJECTOR_OFF_CONFIRM_DELAY", 0.01, raising=True
    )
    server.projector.query_power_return = False

    assert server._projector_transition_pending is False
    server._turn_off_and_confirm()
    assert server._projector_transition_pending is True

    deadline = time.monotonic() + 2
    while server._projector_transition_pending and time.monotonic() < deadline:
        time.sleep(0.005)
    assert server._projector_transition_pending is False


def test_idle_scan_does_not_refire_off_while_transition_pending(
    tmp_path, monkeypatch
):
    """The exact race from the incident: idle-scan tick lands mid-cooldown."""
    server = _make_server(tmp_path)
    monkeypatch.setattr(server, "_connected_outputs", lambda max_age=0: [])
    server._projector_available = False
    server._projector_on = True
    server._projector_transition_pending = True
    _idle_since(server, image_display.PROJECTOR_IDLE_TIMEOUT + 5)

    server._projector_scan(dt=5.0)

    assert server.projector.turn_off_calls == 0


def test_idle_scan_fires_off_once_transition_pending_clears(
    tmp_path, monkeypatch
):
    server = _make_server(tmp_path)
    monkeypatch.setattr(server, "_connected_outputs", lambda max_age=0: [])
    server._projector_available = False
    server._projector_on = True
    server._projector_transition_pending = False
    _idle_since(server, image_display.PROJECTOR_IDLE_TIMEOUT + 5)
    server.projector.query_power_return = False

    server._projector_scan(dt=5.0)

    assert server.projector.turn_off_calls == 1


def test_link_probe_reading_ignored_while_transition_pending():
    """A probe tick landing mid-cooldown must not undo the pending off."""
    server = image_display.ImageDisplayServer.__new__(
        image_display.ImageDisplayServer
    )
    server.projector = _FakeBackend()
    server.projector.query_power_return = True
    server._projector_on = False
    server._projector_transition_pending = True

    server._link_probe_tick()

    assert server._projector_on is False


def test_link_probe_reading_applied_once_transition_clears():
    server = image_display.ImageDisplayServer.__new__(
        image_display.ImageDisplayServer
    )
    server.projector = _FakeBackend()
    server.projector.query_power_return = True
    server._projector_on = False
    server._projector_transition_pending = False

    server._link_probe_tick()

    assert server._projector_on is True


def test_projector_on_restarts_idle_timer(tmp_path, monkeypatch):
    """PROJECTOR_ON after a long idle must not be undone by the next scan."""
    server = _make_server(tmp_path)
    monkeypatch.setattr(server, "_connected_outputs", lambda max_age=0: [])
    monkeypatch.setattr(
        image_display, "_activate_modeless_outputs", lambda: None
    )
    server._projector_available = False
    server._projector_on = False
    _idle_since(server, image_display.PROJECTOR_IDLE_TIMEOUT * 10)

    asyncio.run(server.handle_projector_on())
    server._projector_scan(dt=5.0)

    assert server.projector.turn_on_calls == 1
    assert server.projector.turn_off_calls == 0
    assert server._projector_on is True
