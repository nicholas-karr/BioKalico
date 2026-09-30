"""Tests for projector detection in scripts/sla/image-display.py.

The projector is recognized by the monitor name in its EDID, since connector
names like HDMI-2 never carry a brand.

Skipped when pyglet can't import (no display).
"""

import importlib.util
import os
import subprocess
import sys

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "sla", "image-display.py"
)


def _load_image_display_module():
    sla_dir = os.path.dirname(_MODULE_PATH)
    if sla_dir not in sys.path:
        sys.path.insert(0, sla_dir)
    spec = importlib.util.spec_from_file_location(
        "_image_display_under_test_detection", _MODULE_PATH
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


def _edid(monitor_name=None):
    """A 128-byte EDID base block, with a 0xFC name descriptor if given."""
    edid = bytearray(128)
    edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    if monitor_name is not None:
        text = monitor_name.encode("ascii") + b"\n"
        edid[72:77] = b"\x00\x00\x00\xfc\x00"
        edid[77:90] = text.ljust(13, b" ")
    return bytes(edid)


def _hex_lines(data):
    return "".join(
        f"\t\t{data[i : i + 16].hex()}\n" for i in range(0, len(data), 16)
    )


def test_edid_monitor_name():
    assert image_display._edid_monitor_name(_edid("Optoma UHD")) == "Optoma UHD"
    assert image_display._edid_monitor_name(_edid()) is None
    assert image_display._edid_monitor_name(b"") is None
    assert image_display._edid_monitor_name(b"\x01" * 128) is None


def test_xrandr_outputs_carry_edid_name_and_modes(monkeypatch):
    # Layout of `xrandr --verbose --current`, trimmed to the parsed parts.
    stdout = "".join(
        [
            "Screen 0: minimum 320 x 200, current 6016 x 2160\n",
            "eDP-1 connected primary 1920x1080+0+0 (0x46) normal 344mm\n",
            "\tEDID: \n",
            _hex_lines(_edid()),
            "\tColorspace: Default \n",
            "  1920x1080 (0x46) 138.700MHz +HSync -VSync *current\n",
            "HDMI-1 disconnected (normal left inverted right x axis y axis)\n",
            "HDMI-2 connected 4096x2160+1920+0 (0x51b) normal 800mm\n",
            "\tEDID: \n",
            _hex_lines(_edid("Optoma UHD")),
            "  4096x2160 (0x51b) 297.000MHz +HSync +VSync *current\n",
            "        h: width  4096 start 4184 end 4272 total 4400\n",
            "  4096x2160 (0x51c) 297.000MHz +HSync +VSync\n",
            "  3840x2160 (0x520) 297.000MHz +HSync +VSync\n",
            "  1920x1080i (0x530) 74.250MHz +HSync +VSync Interlace\n",
        ]
    )

    def fake_run(args, **kwargs):
        assert args == ["xrandr", "--verbose", "--current"]
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(image_display.subprocess, "run", fake_run)

    outputs = image_display._xrandr_connected_outputs()

    assert [(o["name"], o["monitor_name"]) for o in outputs] == [
        ("eDP-1", None),
        ("HDMI-2", "Optoma UHD"),
    ]
    assert outputs[1]["geometry"] == (4096, 2160, 1920, 0)
    assert outputs[0]["modes"] == ["1920x1080"]
    assert outputs[1]["modes"] == ["4096x2160", "3840x2160"]


def test_largest_allowed_mode_skips_dci_4k():
    output = {"modes": ["4096x2160", "3840x2160", "1920x1080", "1280x1024"]}
    allowed = image_display.DEFAULT_ALLOWED_ASPECT_RATIOS
    assert image_display._largest_allowed_mode(output, allowed) == (3840, 2160)
    assert image_display._largest_allowed_mode({"modes": []}, allowed) is None


def _optoma_output(width, height):
    return {
        "name": "HDMI-2",
        "descriptor": "hdmi-2",
        "monitor_name": "Optoma UHD",
        "primary": False,
        "width": width,
        "height": height,
        "geometry": (width, height, 1920, 0),
        "modes": ["4096x2160", "3840x2160", "1920x1080"],
    }


def _make_server(tmp_path, monkeypatch, outputs):
    server = image_display.ImageDisplayServer(
        {
            "projector_control": False,
            "video_cache_dir": str(tmp_path / "cache"),
        }
    )
    monkeypatch.setattr(server, "_connected_outputs", lambda max_age=0: outputs)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(image_display.subprocess, "run", fake_run)
    return server, calls


def test_projector_switched_from_dci_4k_to_uhd_once(tmp_path, monkeypatch):
    server, calls = _make_server(
        tmp_path, monkeypatch, [_optoma_output(4096, 2160)]
    )

    server._use_largest_allowed_mode()
    server._use_largest_allowed_mode()

    # A refused switch isn't retried on every scan.
    assert calls == [["xrandr", "--output", "HDMI-2", "--mode", "3840x2160"]]


def test_projector_already_at_largest_allowed_mode_is_left_alone(
    tmp_path, monkeypatch
):
    server, calls = _make_server(
        tmp_path, monkeypatch, [_optoma_output(3840, 2160)]
    )

    server._use_largest_allowed_mode()

    assert calls == []


def test_projector_brand_in_edid_qualifies_on_its_own():
    # Primary at the origin on DisplayPort: not enough without the EDID name.
    output = {
        "name": "DP-1",
        "descriptor": "dp-1",
        "primary": True,
        "width": 1280,
        "height": 800,
        "geometry": (1280, 800, 0, 0),
        "monitor_name": None,
    }
    threshold = image_display.PROJECTOR_SCORE_THRESHOLD
    assert image_display._score_projector_output(output) < threshold

    output["monitor_name"] = "Optoma UHD"
    assert image_display._score_projector_output(output) >= threshold


def test_projector_hints_match_whole_words_only():
    output = {"name": "DP-1", "descriptor": "dp-1", "primary": True}
    output["monitor_name"] = "Epsonic 27"
    assert image_display._score_projector_output(output) < 300
    output["monitor_name"] = "EPSON PJ"
    assert image_display._score_projector_output(output) >= 300


def _hdmi_monitor(monitor_name=None):
    return {
        "name": "HDMI-1",
        "descriptor": "hdmi-1",
        "primary": True,
        "width": 1920,
        "height": 1080,
        "geometry": (1920, 1080, 0, 0),
        "monitor_name": monitor_name,
    }


def test_desktop_monitor_on_hdmi_is_not_a_projector(monkeypatch):
    threshold = image_display.PROJECTOR_SCORE_THRESHOLD
    assert image_display._score_projector_output(_hdmi_monitor()) >= threshold

    monkeypatch.setattr(image_display, "_desktop_session", True)
    assert image_display._score_projector_output(_hdmi_monitor()) < threshold
    assert (
        image_display._score_projector_output(_hdmi_monitor("Optoma UHD"))
        >= threshold
    )

    monkeypatch.setattr(image_display, "_monitor_configured", True)
    assert image_display._score_projector_output(_hdmi_monitor()) >= threshold


def test_desktop_outputs_are_not_switched_on(monkeypatch):
    calls = []
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        image_display.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args),
    )
    monkeypatch.setattr(image_display, "_desktop_session", True)

    image_display._activate_modeless_outputs()

    assert calls == []
