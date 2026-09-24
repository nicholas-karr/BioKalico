#!/usr/bin/env python3
"""
Image Display Server
Displays images and responds to commands via TCP.

Run with "python image-display.py <config.yaml>", e.g. a copy of
image-display-config.yaml.template filled out and placed in
~/printer_data/config/image-display-config.yaml.
Close by pressing ESC.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Optional

import numpy as np
import pyglet
import yaml
from PIL import Image
from sla_video_runtime import VideoRegistry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

PROJECTOR_HINTS = (
    "projector",
    "epson",
    "benq",
    "optoma",
    "viewsonic",
    "vivitek",
    "infocus",
    "nec",
)

INTERNAL_DISPLAY_HINTS = (
    "edp",
    "lvds",
    "internal",
    "builtin",
    "built-in",
)

DEFAULT_ALLOWED_ASPECT_RATIOS = {
    (16, 9),
    (256, 135),  # DCI 4K (4096x2160)
    (4, 3),
    (1, 1),
}

PROJECTOR_SCORE_THRESHOLD = (
    50  # minimum score to qualify as a projector display
)
_OUTPUTS_CACHE_TTL = 0.5  # seconds to cache connected-output enumeration (avoids xrandr spawn churn)
PROJECTOR_IDLE_TIMEOUT = (
    300  # seconds of inactivity before turning projector off (5 minutes)
)
PROJECTOR_SCAN_INTERVAL = 5  # seconds between projector detection scans
PROJECTOR_OFF_CONFIRM_DELAY = (
    5.0  # seconds to wait before verifying the bulb actually turned off
)


def default_video_cache_dir() -> str:
    """Pick a cache directory that matches common MainsailOS layouts."""
    candidate_roots = [
        os.environ.get("BIOSLICER_CACHE_ROOT"),
        os.path.join(os.path.expanduser("~"), "printer_data", "cache"),
    ]

    for root in candidate_roots:
        if root and os.path.isdir(root):
            return os.path.join(root, "bioslicer-sla-video-cache")

    return "/tmp/bioslicer-sla-video-cache"


def _screen_signature(screen) -> tuple[int, int, int, int]:
    return (int(screen.width), int(screen.height), int(screen.x), int(screen.y))


def _parse_geometry(text: Optional[str]) -> Optional[tuple[int, int, int, int]]:
    if not text:
        return None
    match = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", text)
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )


def _normalize_ratio(width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid aspect ratio {width}x{height}")
    divisor = math.gcd(width, height)
    return width // divisor, height // divisor


def _parse_allowed_aspect_ratios(raw_value) -> set[tuple[int, int]]:
    if raw_value is None:
        return set(DEFAULT_ALLOWED_ASPECT_RATIOS)

    parsed: set[tuple[int, int]] = set()
    for item in raw_value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            parsed.add(_normalize_ratio(int(item[0]), int(item[1])))
        elif isinstance(item, str):
            cleaned = item.replace("x", ":").replace("/", ":")
            parts = cleaned.split(":", 1)
            if len(parts) == 2:
                parsed.add(_normalize_ratio(int(parts[0]), int(parts[1])))

    return parsed or set(DEFAULT_ALLOWED_ASPECT_RATIOS)


def _format_aspect_ratios(ratios: set[tuple[int, int]]) -> str:
    return ", ".join(f"{w}:{h}" for w, h in sorted(ratios))


def _drm_connected_outputs() -> list[dict]:
    """Enumerate connected displays via /sys/class/drm (no X11/Wayland required)."""
    drm_root = "/sys/class/drm"
    outputs = []
    try:
        entries = sorted(os.listdir(drm_root))
    except OSError:
        return []

    for entry in entries:
        # Only process named connectors, e.g. "card0-HDMI-A-1"
        parts = entry.split("-", 1)
        if len(parts) < 2:
            continue
        connector = parts[1]

        base = os.path.join(drm_root, entry)
        try:
            status = open(os.path.join(base, "status")).read().strip()
        except OSError:
            continue
        if status != "connected":
            continue

        try:
            enabled = open(os.path.join(base, "enabled")).read().strip()
        except OSError:
            enabled = "unknown"

        try:
            modes = [
                l.strip()
                for l in open(os.path.join(base, "modes"))
                if l.strip()
            ]
        except OSError:
            modes = []

        # First listed mode is the preferred/current resolution
        preferred_mode = modes[0] if modes else None
        width, height = None, None
        if preferred_mode:
            m = re.match(r"^(\d+)x(\d+)", preferred_mode)
            if m:
                width, height = int(m.group(1)), int(m.group(2))

        outputs.append(
            {
                "name": connector,
                "descriptor": connector.lower(),
                "enabled": enabled == "enabled",
                "width": width,
                "height": height,
                "modes": modes,
            }
        )

    return outputs


def _xrandr_connected_outputs() -> list[dict]:
    display = os.environ.get("DISPLAY")
    if not display:
        return []

    try:
        proc = subprocess.run(
            ["xrandr", "--current"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logger.warning("xrandr --current timed out")
        return []
    except Exception:
        return []

    output_re = re.compile(
        r"^(?P<name>\S+)\s+connected(?P<primary>\s+primary)?\s+"
        r"(?P<w>\d+)x(?P<h>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
    )
    outputs = []
    for line in proc.stdout.splitlines():
        match = output_re.match(line.strip())
        if not match:
            continue

        width = int(match.group("w"))
        height = int(match.group("h"))
        x_pos = int(match.group("x"))
        y_pos = int(match.group("y"))
        outputs.append(
            {
                "name": match.group("name"),
                "descriptor": match.group("name").lower(),
                "primary": bool(match.group("primary")),
                "width": width,
                "height": height,
                "geometry": (width, height, x_pos, y_pos),
            }
        )

    return outputs


def _score_projector_output(output: dict) -> int:
    name = output.get("name", "").lower()
    descriptor = output.get("descriptor", "")
    geometry = output.get("geometry")

    score = 0
    if any(hint in descriptor for hint in INTERNAL_DISPLAY_HINTS):
        score -= 300
    if any(hint in descriptor for hint in PROJECTOR_HINTS):
        score += 300
    if name.startswith("hdmi"):
        score += 70
    elif name.startswith("dp"):
        score += 35
    if not output.get("primary", False):
        score += 45
    # geometry tuple (w, h, x, y) used by the xrandr path; DRM path uses width/height directly
    if geometry:
        if geometry[2] != 0 or geometry[3] != 0:
            score += 30
        if geometry[0] * geometry[1] >= 1920 * 1080:
            score += 15
    else:
        w = output.get("width") or 0
        h = output.get("height") or 0
        if w * h >= 1920 * 1080:
            score += 15

    return score


def _best_projector_output(outputs: list[dict]) -> Optional[dict]:
    """Return the highest-scoring output that clears the projector threshold.

    Unlike a bare ``max(outputs, key=_score_projector_output)`` this refuses to
    return a non-projector output (e.g. the internal panel) when no projector is
    connected. Callers can treat ``None`` as "no projector present".
    """
    candidates = [
        o
        for o in outputs
        if _score_projector_output(o) >= PROJECTOR_SCORE_THRESHOLD
    ]
    if not candidates:
        return None
    return max(candidates, key=_score_projector_output)


def _match_screen_for_output(output, screens):
    """Map an xrandr/DRM output to the pyglet screen it corresponds to.

    Prefer an exact (w, h, x, y) signature, but fall back to matching by
    resolution alone when positions disagree: pyglet and xrandr don't always
    report the same (x, y) for an extended second monitor, and the offset can
    change across a replug. Among same-resolution screens, prefer an offset
    (non-origin) one, since the projector is normally the extended display.
    """
    geometry = output.get("geometry")
    if geometry:
        exact = next(
            (s for s in screens if _screen_signature(s) == geometry), None
        )
        if exact is not None:
            return exact
        w, h = geometry[0], geometry[1]
    else:
        w, h = output.get("width"), output.get("height")
    if not w or not h:
        return None
    same_size = [s for s in screens if int(s.width) == w and int(s.height) == h]
    if not same_size:
        return None
    offset = [s for s in same_size if (int(s.x), int(s.y)) != (0, 0)]
    return (offset or same_size)[0]


def guess_projector_monitor(screens):
    outputs = _xrandr_connected_outputs() or _drm_connected_outputs()
    best = None
    best_score = -1

    for output in outputs:
        matched_screen = _match_screen_for_output(output, screens)
        if not matched_screen:
            continue

        score = _score_projector_output(output)
        if score > best_score:
            best = (matched_screen, output)
            best_score = score

    if best is not None:
        screen, output = best
        logger.info(
            "Auto-selected monitor: %s %sx%s@(%s,%s)",
            output.get("name", "unknown"),
            screen.width,
            screen.height,
            screen.x,
            screen.y,
        )
        return screen

    secondary_screens = [s for s in screens if (int(s.x), int(s.y)) != (0, 0)]
    if secondary_screens:
        chosen = max(
            secondary_screens, key=lambda s: int(s.width) * int(s.height)
        )
        logger.info(
            "Auto-selected non-primary monitor fallback: %sx%s@(%s,%s)",
            chosen.width,
            chosen.height,
            chosen.x,
            chosen.y,
        )
        return chosen

    chosen = max(screens, key=lambda s: int(s.width) * int(s.height))
    logger.info(
        "Auto-selected largest monitor fallback: %sx%s@(%s,%s)",
        chosen.width,
        chosen.height,
        chosen.x,
        chosen.y,
    )
    return chosen


def guess_projector_monitor_with_allowed_aspect(
    screens, allowed_aspect_ratios: set[tuple[int, int]]
):
    allowed = allowed_aspect_ratios or set()
    if not allowed:
        return guess_projector_monitor(screens)

    filtered_screens = [
        screen
        for screen in screens
        if _normalize_ratio(int(screen.width), int(screen.height)) in allowed
    ]
    if not filtered_screens:
        return guess_projector_monitor(screens)

    return guess_projector_monitor(filtered_screens)


def enumerate_monitors():
    try:
        display = pyglet.display.get_display()
        screens = display.get_screens()

        print("\nAvailable Monitors: index: widthxheight@(x,y)")
        for i, screen in enumerate(screens):
            print(
                f"Monitor {i}: {screen.width}x{screen.height}@({screen.x},{screen.y})"
            )

        if screens:
            guessed = guess_projector_monitor(screens)
            idx = next(
                (i for i, screen in enumerate(screens) if screen == guessed), 0
            )
            print(f"Auto-detect guess: Monitor {idx}")
    except Exception as e:
        logger.warning(
            "Could not enumerate monitors via pyglet (%s), falling back to DRM sysfs",
            e,
        )
        outputs = _drm_connected_outputs()
        if not outputs:
            print(
                "No monitors found (no display connection and /sys/class/drm reported no connected outputs)"
            )
            return

        print("\nAvailable Monitors (via DRM sysfs): index: name widthxheight")
        for i, output in enumerate(outputs):
            w = output.get("width")
            h = output.get("height")
            res = f"{w}x{h}" if w and h else "unknown resolution"
            enabled = " [enabled]" if output.get("enabled") else ""
            print(f"Monitor {i}: {output['name']} {res}{enabled}")

        best = max(outputs, key=_score_projector_output)
        idx = next((i for i, o in enumerate(outputs) if o is best), 0)
        print(f"Auto-detect guess: Monitor {idx} ({best['name']})")


def _inhibit_gnome_suspend():
    """Register a GNOME session inhibitor to block idle-suspend and the suspend dialog.

    Flags: 4 = inhibit suspend, 8 = inhibit idle (prevents both the countdown dialog
    and the eventual suspend).  The inhibit is kept alive for the lifetime of the process;
    GNOME automatically removes it when the process exits.
    """
    import threading

    def _worker():
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib

            session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            result = session.call_sync(
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager",
                "Inhibit",
                GLib.Variant(
                    "(susu)",
                    (
                        "image-display",  # app_id
                        0,  # toplevel_xid (0 = none)
                        "SLA printer display active",
                        4 | 8,  # inhibit suspend (4) + inhibit idle (8)
                    ),
                ),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            cookie = result.unpack()[0]
            logger.info("GNOME suspend inhibited (cookie=%d)", cookie)
        except Exception as e:
            logger.debug("Could not inhibit GNOME suspend: %s", e)

    threading.Thread(target=_worker, daemon=True).start()


class ProjectorController:
    """Serial-port power control for projectors that accept ~0000 commands."""

    _POWER_QUERY = b"~0000 ?\r"
    _READ_TIMEOUT = 2.0  # seconds to wait for a reply to a query

    def __init__(self, device_path: Optional[str], enabled: bool = True):
        if enabled and not device_path:
            raise ValueError(
                "ProjectorController: 'device_path' (the serial adapter "
                "connected to the projector) is required when projector "
                "control is enabled"
            )
        self.device_path = device_path
        self.enabled = enabled

    def _transact(
        self, cmd: bytes, expect_reply: bool = False
    ) -> Optional[bytes]:
        """Send cmd over the serial link, optionally waiting for a reply.

        Returns the raw reply bytes, or None if control is disabled, the
        link could not be opened, or no reply arrived in time (the failure
        itself is logged as an error so it's impossible to miss).
        """
        if not self.enabled:
            return None
        try:
            fd = os.open(self.device_path, os.O_RDWR | os.O_NOCTTY)
        except OSError as e:
            logger.error(
                "PROJECTOR SERIAL LINK ERROR: cannot open %s (%s), check that "
                "the adapter is plugged in and that 'projector_device' in the "
                "config points to the right port",
                self.device_path,
                e,
            )
            return None
        try:
            os.write(fd, cmd)
            logger.info(
                "Projector command sent to %s: %r", self.device_path, cmd
            )
            if not expect_reply:
                return None
            ready, _, _ = select.select([fd], [], [], self._READ_TIMEOUT)
            if not ready:
                logger.error(
                    "PROJECTOR SERIAL LINK ERROR: no response from %s within "
                    "%.1fs, the projector may be unplugged, powered off at "
                    "the wall, or the adapter is misconfigured",
                    self.device_path,
                    self._READ_TIMEOUT,
                )
                return None
            return os.read(fd, 256)
        except OSError as e:
            logger.error(
                "PROJECTOR SERIAL LINK ERROR: communication with %s failed: %s",
                self.device_path,
                e,
            )
            return None
        finally:
            os.close(fd)

    def turn_on(self):
        logger.info("Turning projector on")
        self._transact(b"~0000 1\r")

    def turn_off(self):
        logger.info("Turning projector off")
        self._transact(b"~0000 0\r")

    def query_power(self) -> Optional[bool]:
        """Query the projector's current power state over the serial link.

        Returns True if on, False if off, or None if control is disabled or
        the query failed.
        """
        if not self.enabled:
            return None
        reply = self._transact(self._POWER_QUERY, expect_reply=True)
        if reply is None:
            return None
        if b"1" in reply:
            return True
        if b"0" in reply:
            return False
        logger.error(
            "PROJECTOR SERIAL LINK ERROR: unrecognized power-status reply "
            "from %s: %r",
            self.device_path,
            reply,
        )
        return None


def find_monitor(
    display,
    monitor_index=None,
    monitor_size=None,
    monitor_position=None,
    monitor_auto_detect=True,
    allowed_aspect_ratios: Optional[set[tuple[int, int]]] = None,
):
    screens = display.get_screens()

    if not screens:
        raise RuntimeError("No monitors found")

    # If both size and position specified, match both
    if monitor_size and monitor_position:
        for screen in screens:
            if (
                screen.width == monitor_size[0]
                and screen.height == monitor_size[1]
                and screen.x == monitor_position[0]
                and screen.y == monitor_position[1]
            ):
                logger.info(
                    f"Selected monitor by size {monitor_size} and position {monitor_position}"
                )
                return screen
        logger.warning(
            f"Monitor with size {monitor_size} and position {monitor_position} not found, using fallback"
        )

    # If only size specified, match size
    if monitor_size:
        for screen in screens:
            if (
                screen.width == monitor_size[0]
                and screen.height == monitor_size[1]
            ):
                logger.info(f"Selected monitor by size {monitor_size}")
                return screen
        logger.warning(
            f"Monitor with size {monitor_size} not found, using fallback"
        )

    # If only position specified, match position
    if monitor_position:
        for screen in screens:
            if (
                screen.x == monitor_position[0]
                and screen.y == monitor_position[1]
            ):
                logger.info(f"Selected monitor by position {monitor_position}")
                return screen
        logger.warning(
            f"Monitor at position {monitor_position} not found, using fallback"
        )

    if monitor_index is None and monitor_auto_detect:
        if allowed_aspect_ratios:
            return guess_projector_monitor_with_allowed_aspect(
                screens, allowed_aspect_ratios
            )
        return guess_projector_monitor(screens)

    if monitor_index is not None:
        try:
            monitor_index = int(monitor_index)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid monitor index %r, using monitor 0", monitor_index
            )
            monitor_index = 0

    # Fallback to index
    if monitor_index is None:
        monitor_index = 0

    if monitor_index < 0:
        logger.warning(
            f"Negative monitor index {monitor_index} is invalid, using monitor 0"
        )
        monitor_index = 0

    if monitor_index >= len(screens):
        logger.warning(
            f"Monitor index {monitor_index} not found, using monitor 0"
        )
        monitor_index = 0

    logger.info(f"Selected monitor {monitor_index}")
    return screens[monitor_index]


# Handle --enum-monitors before pyglet.window is accessed. Accessing pyglet.window
# triggers X11 display connection at class-definition time, which fails without a display.
if __name__ == "__main__" and "--enum-monitors" in sys.argv:
    enumerate_monitors()
    sys.exit(0)


class ImageDisplayWindow(pyglet.window.Window):
    """Fullscreen window for image display."""

    @staticmethod
    def _best_mode(screen):
        """Return the highest-pixel-count mode for this screen, or None to use current."""
        try:
            modes = screen.get_modes()
        except Exception:
            return None
        if not modes:
            return None
        best = max(modes, key=lambda m: m.width * m.height)
        logger.info(
            "Selecting display mode %sx%s (current: %sx%s)",
            best.width,
            best.height,
            screen.width,
            screen.height,
        )
        return best

    def __init__(self, screen, **kwargs):
        config = pyglet.gl.Config(
            double_buffer=True, sample_buffers=0, samples=0
        )
        mode = self._best_mode(screen)

        super().__init__(
            caption="Image Display Server",
            fullscreen=True,
            screen=screen,
            mode=mode,
            vsync=True,
            config=config,
            **kwargs,
        )

        self.current_sprite = None
        pyglet.clock.schedule_once(
            lambda dt: self._suppress_gnome_overlays(), 0
        )

    @staticmethod
    def _suppress_gnome_overlays():
        """Dismiss GNOME compositor overlays (Activities overview + notification banners).

        Both are Wayland surfaces rendered by Mutter above all Xwayland windows.
        X11 hints (_NET_WM_STATE_ABOVE etc.) cannot outrank them; D-Bus is the only
        path in from an Xwayland client.

        Runs in a daemon thread so it never blocks the pyglet event loop.
        """
        import threading

        threading.Thread(
            target=ImageDisplayWindow._suppress_gnome_overlays_worker,
            daemon=True,
        ).start()

    @staticmethod
    def _suppress_gnome_overlays_worker():
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib

            session = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            # 1. Close the Activities overview.
            try:
                session.call_sync(
                    "org.gnome.Shell",
                    "/org/gnome/Shell",
                    "org.freedesktop.DBus.Properties",
                    "Set",
                    GLib.Variant(
                        "(ssv)",
                        (
                            "org.gnome.Shell",
                            "OverviewActive",
                            GLib.Variant("b", False),
                        ),
                    ),
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
            except Exception as e:
                logger.debug("Could not close GNOME overview: %s", e)

            # 2. Close any visible notification banners by cycling through IDs.
            #    CloseNotification silently ignores IDs that don't exist.
            for notif_id in range(1, 200):
                try:
                    session.call_sync(
                        "org.freedesktop.Notifications",
                        "/org/freedesktop/Notifications",
                        "org.freedesktop.Notifications",
                        "CloseNotification",
                        GLib.Variant("(u)", (notif_id,)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        200,
                        None,
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("GNOME overlay suppression failed: %s", e)

    def on_mouse_enter(self, x, y):
        self.set_mouse_visible(False)

    def on_mouse_leave(self, x, y):
        self.set_mouse_visible(True)

    def on_resize(self, width, height):
        # width/height are logical pixels; use the framebuffer for a 1:1 physical-pixel
        # coordinate system so sprites are never scaled by OS HiDPI factors.
        fb_w, fb_h = self.get_framebuffer_size()
        self.viewport = (0, 0, fb_w, fb_h)
        self.projection = pyglet.math.Mat4.orthogonal_projection(
            0, fb_w, 0, fb_h, -1, 1
        )

    def on_draw(self):
        """Render the current image."""
        self.clear()

        if self.current_sprite:
            self.current_sprite.draw()

    def _set_texture(
        self, texture, width: int, height: int, h_offset: float = 0.0
    ):
        fb_w, fb_h = self.get_framebuffer_size()
        self.current_sprite = pyglet.sprite.Sprite(texture, x=0, y=0)
        self.current_sprite.scale = 1
        self.current_sprite.x = int((fb_w - width) / 2 + round(h_offset))
        self.current_sprite.y = (fb_h - height) // 2

    def load_image(
        self, image_path: str, rotation: int = 0, h_offset: float = 0.0
    ):
        """Load an image from a local file path.

        Args:
            image_path: Local filesystem path to the image (URLs are already
                resolved to a local path by the caller)
            rotation: Rotation angle in degrees (0, 90, 180, 270)
            h_offset: Horizontal pixel offset from centre (positive = right)
        """
        try:
            # Load image using PIL
            logger.info(f"Loading image: {image_path}")
            img = Image.open(image_path)
            img = img.convert("RGBA")

            if rotation:
                logger.info(f"Rotating image by {rotation} degrees")
                img = img.rotate(-rotation, expand=True)

            # Get image data
            img_data = np.array(img)
            height, width = img_data.shape[:2]

            # PIL uses top-down, OpenGL uses bottom-up.
            img_data = np.flipud(img_data)
            raw_data = img_data.tobytes()

            pyglet_image = pyglet.image.ImageData(
                width, height, "RGBA", raw_data, pitch=-width * 4
            )

            texture = pyglet_image.get_texture()

            self._set_texture(texture, width, height, h_offset)

            logger.info(
                f"Image loaded successfully: {width}x{height}, rotation={rotation}°, h_offset={h_offset}"
            )

        except Exception as e:
            logger.error(f"Failed to load image: {e}")
            raise

    def load_rgba_frame(
        self, width: int, height: int, frame_rgba: bytes, h_offset: float = 0.0
    ):
        """Load a raw RGBA frame and render it fullscreen."""
        try:
            frame = np.frombuffer(frame_rgba, dtype=np.uint8)
            frame = frame.reshape((height, width, 4))
            frame = np.flipud(frame)

            pyglet_image = pyglet.image.ImageData(
                width, height, "RGBA", frame.tobytes(), pitch=-width * 4
            )

            texture = pyglet_image.get_texture()
            self._set_texture(texture, width, height, h_offset)
        except Exception as e:
            logger.error(f"Failed to load RGBA frame: {e}")
            raise

    def clear_image(self):
        """Clear the current image."""
        self.current_sprite = None


class ImageDisplayServer:
    """TCP server for handling image display commands."""

    def __init__(self, config: dict):
        self.config = config
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 5555)
        self.require_exact_resolution = bool(
            config.get("require_exact_resolution", True)
        )
        self.monitor_auto_detect = bool(config.get("monitor_auto_detect", True))
        self.allowed_aspect_ratios = _parse_allowed_aspect_ratios(
            config.get("allowed_aspect_ratios")
        )
        self.bound_aspect_ratio: Optional[tuple[int, int]] = None

        cache_dir = config.get("video_cache_dir")
        if not cache_dir:
            cache_dir = default_video_cache_dir()
            logger.info("Using default video cache dir: %s", cache_dir)
        else:
            cache_dir = os.path.expanduser(cache_dir)

        self.window: Optional[ImageDisplayWindow] = None
        self.server = None

        self.video_registry = VideoRegistry(
            cache_dir=cache_dir,
            hwaccel=config.get("ffmpeg_hwaccel", "auto"),
            hw_decoder=config.get("ffmpeg_hw_decoder"),
        )

        self.projector = ProjectorController(
            device_path=config.get("projector_device"),
            enabled=bool(config.get("projector_control", True)),
        )
        self._projector_available: bool = False
        self._projector_on: bool = False
        self._has_content: bool = False
        self._last_content_change: Optional[float] = None

        # Window-binding snapshot. Written on the pyglet main thread
        # (create_window / _move_window_to_projector); read from the asyncio
        # command thread (handle_status, _window_bound_to_projector, ...).
        # _bind_lock guards the whole group so a cross-thread reader never sees
        # a half-updated snapshot straddling a rebind. Only cross-thread readers
        # take the lock. Main-thread code that swaps the window is serialized
        # with itself already.
        self._bind_lock = threading.Lock()
        self._current_screen_w: Optional[int] = None
        self._current_screen_h: Optional[int] = None
        # Note: window (x, y) is deliberately not tracked, it's an arbitrary,
        # sometimes-unstable offset for an extended second monitor, so the
        # display is identified by output name + resolution instead.
        # Name of the xrandr/DRM output the window is bound to, recorded at bind
        # time. Used to identify the display without relying on (x, y), which is
        # an arbitrary offset when the projector is an extended second monitor.
        self._bound_output_name: Optional[str] = None

        # Short-TTL cache for connected-output enumeration. Readiness polling
        # hits this every ~0.5s during PROJECTOR_ON; without the cache each poll
        # spawns an xrandr subprocess. The lock serializes the refresh so a
        # burst of pollers triggers at most one spawn per TTL window.
        self._outputs_cache: Optional[list[dict]] = None
        self._outputs_cache_time: float = 0.0
        self._outputs_lock = threading.Lock()

    def _connected_outputs(
        self, max_age: float = _OUTPUTS_CACHE_TTL
    ) -> list[dict]:
        """Connected outputs, cached for a short TTL to avoid spawning an
        xrandr/DRM query on every readiness poll. Pass max_age=0 to force a
        fresh read."""
        now = time.monotonic()
        with self._outputs_lock:
            if (
                self._outputs_cache is not None
                and now - self._outputs_cache_time < max_age
            ):
                return self._outputs_cache
            outputs = _xrandr_connected_outputs() or _drm_connected_outputs()
            self._outputs_cache = outputs
            self._outputs_cache_time = now
            return outputs

    def _output_name_for_screen(self, screen) -> Optional[str]:
        """Best-effort xrandr output name for the pyglet screen being bound.

        Matches the best-scoring projector output by resolution only, never by
        position, so an extended second monitor at an arbitrary (x, y) is still
        identified correctly.
        """
        best = _best_projector_output(self._connected_outputs())
        if (
            best
            and best.get("width") == int(screen.width)
            and best.get("height") == int(screen.height)
        ):
            return best.get("name")
        return None

    def _record_binding(self, window, screen):
        """Atomically publish a new window-binding snapshot (main thread)."""
        output_name = self._output_name_for_screen(screen)
        with self._bind_lock:
            self.window = window
            self.bound_aspect_ratio = _normalize_ratio(
                int(screen.width), int(screen.height)
            )
            self._current_screen_w = int(screen.width)
            self._current_screen_h = int(screen.height)
            self._bound_output_name = output_name

    def _binding_snapshot(self) -> dict:
        """Atomically read the current window-binding snapshot (any thread)."""
        with self._bind_lock:
            return {
                "window": self.window,
                "width": self._current_screen_w,
                "height": self._current_screen_h,
                "output_name": self._bound_output_name,
                "aspect": self.bound_aspect_ratio,
            }

    def _aspect_matches_bound(self, width: int, height: int) -> bool:
        bound = self._binding_snapshot()["aspect"]
        if bound is None:
            return False
        return _normalize_ratio(width, height) == bound

    def _selected_monitor_message(self, screen) -> str:
        ratio = _normalize_ratio(int(screen.width), int(screen.height))
        return f"{screen.width}x{screen.height} ({ratio[0]}:{ratio[1]})"

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        addr = writer.get_extra_info("peername")
        logger.debug(f"Client connected: {addr}")

        try:
            while True:
                # Read data until newline
                data = await reader.readline()
                if not data:
                    break

                # Parse JSON command
                try:
                    command = json.loads(data.decode().strip())
                    logger.debug(f"Received command: {command}")

                    response = await self.process_command(command)

                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()

                except json.JSONDecodeError as e:
                    error_response = {
                        "status": "error",
                        "message": f"Invalid JSON: {e}",
                    }
                    writer.write(json.dumps(error_response).encode() + b"\n")
                    await writer.drain()

        except asyncio.CancelledError:
            logger.info(f"Client handler cancelled: {addr}")
        except Exception as e:
            logger.error(f"Error handling client {addr}: {e}")
        finally:
            logger.debug(f"Client disconnected: {addr}")
            writer.close()
            await writer.wait_closed()

    async def process_command(self, command: dict) -> dict:
        """Process a command and return response."""
        cmd_type = command.get("type", "").upper()

        if cmd_type == "DISPLAY_IMAGE":
            return await self.handle_display_image(command)
        elif cmd_type == "LOAD_VIDEO":
            return await self.handle_load_video(command)
        elif cmd_type == "LOAD_VIDEOS_FROM_GCODE":
            return await self.handle_load_videos_from_gcode(command)
        elif cmd_type == "SHOW_VIDEO_FRAME":
            return await self.handle_show_video_frame(command)
        elif cmd_type == "UNLOAD_VIDEO":
            return await self.handle_unload_video(command)
        elif cmd_type == "UNLOAD_ALL":
            return await self.handle_unload_all_videos()
        elif cmd_type == "LIST_VIDEOS":
            return await self.handle_list_videos()
        elif cmd_type == "CLEAR":
            return await self.handle_clear()
        elif cmd_type == "PROJECTOR_OFF":
            return await self.handle_projector_off()
        elif cmd_type == "PROJECTOR_ON":
            return await self.handle_projector_on()
        elif cmd_type == "STATUS":
            return await self.handle_status()
        else:
            return {
                "status": "error",
                "message": f"Unknown command type: {cmd_type}",
            }

    def _ensure_projector_on(self) -> Optional[dict]:
        """Check projector readiness and power it on if needed.

        Returns an error dict if the display is not ready to receive an image, or
        None if the window is bound to the projector output at its native
        resolution. Requiring the bound state (not merely electrical connection)
        prevents an image from being loaded onto a transient fallback mode.
        """
        if not self._projector_available:
            self.projector.turn_on()
            self._projector_on = True
            return {
                "status": "error",
                "message": (
                    "No projector display detected; sent turn-on command. "
                    "Retry when the projector is ready."
                ),
            }
        if not self._projector_on:
            self.projector.turn_on()
            self._projector_on = True
        if not self._window_bound_to_projector():
            return {
                "status": "error",
                "message": (
                    "Projector connected but display window is not yet bound at its "
                    "native resolution; run PROJECTOR_ON and retry."
                ),
            }
        return None

    async def handle_display_image(self, command: dict) -> dict:
        try:
            image_path = command.get("path")
            rotation = int(command.get("rotation", 0))
            h_offset = float(command.get("h_offset", 0.0))

            if not image_path:
                return {"status": "error", "message": "Missing path parameter"}

            if rotation not in (0, 90, 180, 270):
                return {
                    "status": "error",
                    "message": f"Invalid rotation {rotation}; must be one of 0, 90, 180, 270",
                }

            projector_err = self._ensure_projector_on()
            if projector_err:
                return projector_err

            local_path, is_temp = await asyncio.to_thread(
                self._resolve_image_path, image_path
            )
            try:
                width, height = self._read_image_size(local_path)
            except Exception:
                if is_temp:
                    self._cleanup_temp_file(local_path)
                raise

            if rotation % 180 != 0:
                rotated_size = (height, width)
            else:
                rotated_size = (width, height)

            if self.require_exact_resolution and not self._aspect_matches_bound(
                *rotated_size
            ):
                allowed = _format_aspect_ratios(self.allowed_aspect_ratios)
                bound = (
                    _format_aspect_ratios({self.bound_aspect_ratio})
                    if self.bound_aspect_ratio
                    else "unknown"
                )
                logger.error(
                    "Rejected image: %sx%s (rot=%s) does not match bound aspect ratio %s",
                    width,
                    height,
                    rotation,
                    bound,
                )
                pyglet.clock.schedule_once(
                    lambda dt: self.window.clear_image(), 0
                )
                if is_temp:
                    self._cleanup_temp_file(local_path)
                return {
                    "status": "error",
                    "message": (
                        f"Image aspect ratio {rotated_size[0]}x{rotated_size[1]} does not match "
                        f"bound monitor aspect ratio {bound}. Allowed ratios: {allowed}"
                    ),
                }

            # Schedule image loading on the main pyglet thread, then clean up
            # any downloaded temp file regardless of whether the load succeeded.
            def _load_and_cleanup(
                dt, p=local_path, r=rotation, o=h_offset, temp=is_temp
            ):
                try:
                    self.window.load_image(p, r, o)
                finally:
                    if temp:
                        self._cleanup_temp_file(p)

            pyglet.clock.schedule_once(_load_and_cleanup, 0)

            self._has_content = True
            self._last_content_change = time.monotonic()

            return {
                "status": "success",
                "message": f"Displaying image: {image_path} (rotation={rotation}°, h_offset={h_offset})",
            }

        except Exception as e:
            logger.error(f"Error displaying image: {e}")
            return {"status": "error", "message": str(e)}

    async def handle_load_video(self, command: dict) -> dict:
        try:
            name = command.get("name")
            path = command.get("path")
            if not name:
                return {"status": "error", "message": "Missing name parameter"}
            if not path:
                return {"status": "error", "message": "Missing path parameter"}

            meta = await asyncio.to_thread(
                self.video_registry.load_video,
                str(name),
                str(path),
                command.get("hwaccel"),
                command.get("hw_decoder"),
            )
            return {"status": "success", "video": meta}
        except Exception as e:
            logger.error(f"Error loading video: {e}")
            return {"status": "error", "message": str(e)}

    async def handle_load_videos_from_gcode(self, command: dict) -> dict:
        try:
            gcode_path = command.get("gcode_path")
            if not gcode_path:
                return {
                    "status": "error",
                    "message": "Missing gcode_path parameter",
                }

            loaded = await asyncio.to_thread(
                self.video_registry.load_videos_from_gcode,
                str(gcode_path),
            )
            return {"status": "success", "loaded": loaded}
        except Exception as e:
            logger.error(f"Error loading videos from G-code: {e}")
            return {"status": "error", "message": str(e)}

    async def handle_show_video_frame(self, command: dict) -> dict:
        try:
            name = command.get("name")
            frame = command.get("frame")
            h_offset = float(command.get("h_offset", 0.0))
            if not name:
                return {"status": "error", "message": "Missing name parameter"}
            if frame is None:
                return {"status": "error", "message": "Missing frame parameter"}

            projector_err = self._ensure_projector_on()
            if projector_err:
                return projector_err

            frame_index = int(frame)
            width, height, rgba = await asyncio.to_thread(
                self.video_registry.get_frame,
                str(name),
                frame_index,
            )

            window = self._binding_snapshot()["window"]
            if window is None:
                return {"status": "error", "message": "No display window bound"}
            _, fb_h = window.get_framebuffer_size()
            if height != fb_h:
                return {
                    "status": "error",
                    "message": (
                        f"Frame height {height} does not match projector height "
                        f"{fb_h}; video must vertically fill the projector"
                    ),
                }

            # Look up the window fresh when the callback actually fires
            # (matching handle_display_image's pattern), and only proceed if
            # it's still the window that was validated above. A resolution
            # change can rebind self.window and close this one between
            # scheduling and execution, so a stale/closed reference here
            # must be a safe no-op rather than an error.
            def _show_frame(
                dt, w=width, h=height, r=rgba, o=h_offset, expected=window
            ):
                current = self.window
                if current is None or current is not expected:
                    return
                current.load_rgba_frame(w, h, r, o)

            pyglet.clock.schedule_once(_show_frame, 0)
            self._has_content = True
            self._last_content_change = time.monotonic()
            return {
                "status": "success",
                "message": f"Displayed frame {frame_index} from {name}",
                "frame": frame_index,
                "name": name,
            }
        except Exception as e:
            logger.error(f"Error showing video frame: {e}")
            return {"status": "error", "message": str(e)}

    async def handle_unload_video(self, command: dict) -> dict:
        try:
            name = command.get("name")
            if not name:
                return {"status": "error", "message": "Missing name parameter"}

            unloaded = await asyncio.to_thread(
                self.video_registry.unload_video, str(name)
            )
            if unloaded:
                return {"status": "success", "message": f"Unloaded {name}"}
            return {"status": "error", "message": f"Video not loaded: {name}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def handle_unload_all_videos(self) -> dict:
        try:
            count = await asyncio.to_thread(self.video_registry.unload_all)
            return {
                "status": "success",
                "message": f"Unloaded {count} video(s)",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def handle_list_videos(self) -> dict:
        try:
            videos = await asyncio.to_thread(self.video_registry.list_videos)
            return {"status": "success", "videos": videos}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _resolve_image_path(self, image_path: str) -> tuple[str, bool]:
        """Resolve image_path to a local path, downloading URLs to a temp file.

        Returns (local_path, is_temp) where is_temp indicates the caller is
        responsible for deleting local_path once it's done with it.

        This performs blocking network I/O for http(s) URLs, so callers must
        invoke it via asyncio.to_thread rather than directly on the event
        loop. The temp file is created and cleaned up here on any failure so
        a failed/timed-out download never leaks an orphaned .tmp file.
        """
        if image_path.startswith(("http://", "https://")):
            logger.info(f"Downloading image from URL: {image_path}")
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as tmp_file:
                    with urllib.request.urlopen(image_path, timeout=15) as resp:
                        shutil.copyfileobj(resp, tmp_file)
                return tmp_path, True
            except Exception:
                self._cleanup_temp_file(tmp_path)
                raise
        return image_path, False

    @staticmethod
    def _cleanup_temp_file(path: str) -> None:
        try:
            os.remove(path)
        except OSError as e:
            logger.warning(f"Failed to remove temp file {path}: {e}")

    def _read_image_size(self, image_path: str) -> tuple[int, int]:
        try:
            with Image.open(image_path) as img:
                return img.size
        except Exception as e:
            raise RuntimeError(f"Failed to read image size: {e}")

    async def handle_clear(self) -> dict:
        try:
            window = self._binding_snapshot()["window"]
            if window:
                # Re-check self.window fresh when the callback fires (see
                # handle_show_video_frame for the same rebind-race guard) so
                # a resolution-change rebind between scheduling and execution
                # is a safe no-op instead of calling into a closed window.
                def _clear(dt, expected=window):
                    current = self.window
                    if current is None or current is not expected:
                        return
                    current.clear_image()

                pyglet.clock.schedule_once(_clear, 0)
            self._has_content = False
            self._last_content_change = time.monotonic()
            return {"status": "success", "message": "Image cleared"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def handle_status(self) -> dict:
        outputs = self._connected_outputs()

        displays = [
            {
                "name": o.get("name", ""),
                "width": o.get("width"),
                "height": o.get("height"),
            }
            for o in outputs
        ]

        # Identify which connected output the window currently lives on WITHOUT
        # relying on (x, y): as an extended second monitor the projector sits at
        # an arbitrary offset, and pyglet's screen coordinates need not agree
        # with xrandr's. Prefer the output name recorded at bind time; if that
        # output is gone, fall back to the best-scoring projector output whose
        # resolution matches the bound window.
        bound = self._binding_snapshot()
        window_info: Optional[dict] = None
        if bound["width"] is not None:
            window_info = {
                "width": bound["width"],
                "height": bound["height"],
            }
            match_index: Optional[int] = None
            if bound["output_name"] is not None:
                match_index = next(
                    (
                        i
                        for i, o in enumerate(outputs)
                        if o.get("name") == bound["output_name"]
                    ),
                    None,
                )
            if match_index is None:
                best = _best_projector_output(outputs)
                if (
                    best
                    and best.get("width") == bound["width"]
                    and best.get("height") == bound["height"]
                ):
                    match_index = outputs.index(best)
            if match_index is not None:
                window_info["display_name"] = outputs[match_index].get("name")
                window_info["display_index"] = match_index

        idle_seconds: Optional[float] = None
        last_activity_unix_time: Optional[float] = None
        if self._last_content_change is not None:
            idle_seconds = time.monotonic() - self._last_content_change
            # Convert monotonic reference to wall-clock epoch; stable until next activity.
            last_activity_unix_time = time.time() - idle_seconds

        return {
            "status": "success",
            "projector_available": self._projector_available,
            "projector_on": self._projector_on,
            "ready": self._projector_available,
            # display_ready is the rigorous readiness signal: the window is
            # bound to the projector output at its native resolution. Prefer
            # this over 'ready' (which only reports electrical connection).
            "display_ready": self._window_bound_to_projector(),
            "has_content": self._has_content,
            "idle_seconds": idle_seconds,
            "last_activity_unix_time": last_activity_unix_time,
            "displays": displays,
            "window": window_info,
        }

    async def handle_projector_on(self) -> dict:
        self.projector.turn_on()
        self._projector_on = True

        # "Ready" means the display window is actually bound to the projector
        # output at its native resolution, not merely that a projector-like
        # display is electrically connected. The compositor may still be
        # applying its saved mode, or the window move may still be retrying.
        if self._window_bound_to_projector():
            return {
                "status": "success",
                "message": "Projector on and display ready",
            }

        if self._projector_available:
            return {
                "status": "not_ready",
                "message": "Projector connected; waiting for window to bind at native resolution",
            }

        return {
            "status": "not_ready",
            "message": "Projector on command sent; waiting for display to connect",
        }

    async def handle_projector_off(self) -> dict:
        self._turn_off_and_confirm()
        return {"status": "success", "message": "Projector turned off"}

    def _turn_off_and_confirm(self):
        """Send the power-off command, then verify the bulb actually went out.

        The verification runs in a background thread (with a blocking serial
        read) after a short delay, so it never stalls the pyglet render loop
        or the asyncio command handler.
        """
        self.projector.turn_off()
        self._projector_on = False

        def _confirm():
            time.sleep(PROJECTOR_OFF_CONFIRM_DELAY)
            state = self.projector.query_power()
            if state is True:
                logger.error(
                    "PROJECTOR ERROR: power-off command was sent but the "
                    "projector still reports power ON %.0fs later, the bulb "
                    "may not have actually turned off; check the projector "
                    "and the serial adapter",
                    PROJECTOR_OFF_CONFIRM_DELAY,
                )
                self._projector_on = True
            elif state is None and self.projector.enabled:
                logger.error(
                    "PROJECTOR SERIAL LINK ERROR: could not confirm power-off "
                    "state for %s after sending the off command",
                    self.projector.device_path,
                )

        threading.Thread(target=_confirm, daemon=True).start()

    def _window_bound_to_projector(self) -> bool:
        """Authoritative "display ready" signal.

        Returns True only when the pyglet window is currently bound to the
        best-scoring projector output *at that output's real resolution* and
        that resolution has an allowed aspect ratio. This deliberately rejects
        the transient fallback modes Mutter presents before applying saved
        config (e.g. 1920x1080 before switching to the projector's native
        3840x2160), both may be 16:9, but only the native one matches the
        pixel size the window is actually bound to.
        """
        bound = self._binding_snapshot()
        if bound["window"] is None or bound["width"] is None:
            return False
        best = _best_projector_output(self._connected_outputs())
        if best is None:
            return False
        width, height = best.get("width"), best.get("height")
        if not width or not height:
            return False
        if width != bound["width"] or height != bound["height"]:
            return False
        if (
            self.allowed_aspect_ratios
            and _normalize_ratio(width, height)
            not in self.allowed_aspect_ratios
        ):
            return False
        return True

    def _check_projector_resolution_change(self):
        """Detect the projector's connector reporting a new resolution without a
        connect/disconnect event.

        Mutter can briefly present a fallback mode (e.g. 1920x1080) before applying the
        saved monitors.xml config and switching to the projector's true native mode (e.g.
        3840x2160). Both are 16:9, so the aspect-ratio check that gates startup and
        connect/disconnect handling can't tell them apart and happily binds to the wrong
        one. Re-bind whenever the best-scoring output's actual pixel size no longer matches
        what the window is currently showing.
        """
        best = _best_projector_output(self._connected_outputs())
        if best is None:
            return
        width, height = best.get("width"), best.get("height")
        if not width or not height:
            return
        bound = self._binding_snapshot()
        if width != bound["width"] or height != bound["height"]:
            logger.info(
                "Projector resolution changed from %sx%s to %sx%s; re-binding window",
                bound["width"],
                bound["height"],
                width,
                height,
            )
            self._move_window_to_projector()

    def _projector_scan(self, dt: float):
        """Periodic projector detection and idle-timeout check (runs on main pyglet thread)."""
        # max_age=0: this is the authoritative 5s scan, take a fresh reading so
        # a connect/disconnect isn't masked by a cached enumeration.
        available = (
            _best_projector_output(self._connected_outputs(max_age=0))
            is not None
        )
        if available != self._projector_available:
            logger.info(
                "Projector display %s",
                "detected" if available else "no longer detected",
            )
            self._projector_available = available
            if available:
                self._move_window_to_projector()
        elif available:
            self._check_projector_resolution_change()

        if self._projector_on and self._last_content_change is not None:
            idle = time.monotonic() - self._last_content_change
            if idle >= PROJECTOR_IDLE_TIMEOUT:
                logger.info("Projector idle for %.0fs, turning off", idle)
                self._turn_off_and_confirm()

    async def start(self):
        """Start the TCP server."""
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )

        addr = self.server.sockets[0].getsockname()
        logger.info(f"Server started on {addr[0]}:{addr[1]}")

        async with self.server:
            await self.server.serve_forever()

    def _find_valid_screen(self, display):
        """Return the first screen with an allowed aspect ratio, waiting indefinitely.

        Mutter may briefly present a projector at its native DCI resolution before applying the
        saved monitors.xml config (e.g. 4096x2160 before switching to 3840x2160). On a cold boot
        the compositor itself may not be fully ready yet. We loop forever so the service never
        crashes waiting for hardware that just needs more time.
        """
        allowed = _format_aspect_ratios(self.allowed_aspect_ratios)
        last_msg: Optional[str] = None

        while True:
            try:
                screen = find_monitor(
                    display,
                    monitor_index=self.config.get("monitor_index"),
                    monitor_size=self.config.get("monitor_size"),
                    monitor_position=self.config.get("monitor_position"),
                    monitor_auto_detect=self.monitor_auto_detect,
                    allowed_aspect_ratios=self.allowed_aspect_ratios,
                )
                screen_aspect = _normalize_ratio(
                    int(screen.width), int(screen.height)
                )
                if screen_aspect in self.allowed_aspect_ratios:
                    return screen

                msg = (
                    f"Monitor {self._selected_monitor_message(screen)} has aspect ratio "
                    f"{screen_aspect[0]}:{screen_aspect[1]}; waiting for compositor to apply "
                    f"saved config (allowed: {allowed})"
                )
            except Exception as e:
                msg = f"No usable monitor yet ({e}); waiting for display to appear"

            if msg != last_msg:
                logger.info(msg)
                last_msg = msg

            time.sleep(2)

    def _move_window_to_projector(self):
        """Close the current window and reopen on the projector display (main thread)."""
        if not self._projector_available:
            return
        display = pyglet.display.get_display()
        try:
            screen = find_monitor(
                display,
                monitor_index=self.config.get("monitor_index"),
                monitor_size=self.config.get("monitor_size"),
                monitor_position=self.config.get("monitor_position"),
                monitor_auto_detect=self.monitor_auto_detect,
                allowed_aspect_ratios=self.allowed_aspect_ratios,
            )
            screen_aspect = _normalize_ratio(
                int(screen.width), int(screen.height)
            )
            if screen_aspect not in self.allowed_aspect_ratios:
                raise RuntimeError(
                    f"Monitor {screen.width}x{screen.height} aspect ratio "
                    f"{screen_aspect[0]}:{screen_aspect[1]} not in allowed list; "
                    "compositor may not have applied config yet"
                )
        except Exception as e:
            logger.info(
                "Projector window move not ready (%s); will retry in 2s", e
            )
            pyglet.clock.schedule_once(
                lambda dt: self._move_window_to_projector(), 2.0
            )
            return

        logger.info(
            "Moving window to projector display: %sx%s@(%s,%s)",
            screen.width,
            screen.height,
            screen.x,
            screen.y,
        )
        new_window = ImageDisplayWindow(screen=screen)
        old_window = self._binding_snapshot()["window"]
        self._record_binding(new_window, screen)
        if old_window is not None:
            old_window.close()
        self._projector_on = True
        self._last_content_change = time.monotonic()
        _inhibit_gnome_suspend()
        ImageDisplayWindow._suppress_gnome_overlays()

    def create_window(self):
        """Create the display window."""
        display = pyglet.display.get_display()
        self._projector_available = (
            _best_projector_output(self._connected_outputs(max_age=0))
            is not None
        )

        # Query the serial link for the projector's actual power state rather
        # than assuming it's off, the previous run may have left it on.
        initial_power = self.projector.query_power()
        if initial_power is None:
            if self.projector.enabled:
                logger.error(
                    "PROJECTOR SERIAL LINK ERROR: could not determine power "
                    "state for %s at startup; assuming off",
                    self.projector.device_path,
                )
            initial_power = False
        self._projector_on = initial_power
        logger.info(
            "Projector power state at startup (via serial link): %s",
            "on" if initial_power else "off",
        )

        if self._projector_available:
            # Projector already connected, find a screen with the right aspect ratio.
            screen = self._find_valid_screen(display)
            logger.info("Projector display available at startup")
            self._last_content_change = time.monotonic()
        else:
            # No projector yet, open on whatever screen is available and wait.
            screen = find_monitor(
                display,
                monitor_index=self.config.get("monitor_index"),
                monitor_size=self.config.get("monitor_size"),
                monitor_position=self.config.get("monitor_position"),
                monitor_auto_detect=self.monitor_auto_detect,
                allowed_aspect_ratios=self.allowed_aspect_ratios,
            )
            logger.warning(
                "No projector display detected at startup; will move window when one appears"
            )
            if self._projector_on:
                pyglet.clock.schedule_once(
                    lambda dt: self._turn_off_and_confirm(), 0
                )

        self._record_binding(ImageDisplayWindow(screen=screen), screen)
        logger.info("Display window created")
        _inhibit_gnome_suspend()
        # Retry suppressing GNOME overlays (overview + notifications) several times
        # during boot, the compositor may reshow them as the desktop settles.
        for _delay in (1.0, 3.0, 6.0, 10.0):
            pyglet.clock.schedule_once(
                lambda dt: ImageDisplayWindow._suppress_gnome_overlays(), _delay
            )
        pyglet.clock.schedule_interval(
            self._projector_scan, PROJECTOR_SCAN_INTERVAL
        )


def run_async_server(
    server: ImageDisplayServer, loop: asyncio.AbstractEventLoop
):
    """Run the async server in a background thread."""
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.start())
    except Exception as e:
        logger.error(f"Server error: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Image Display Server - Display images fullscreen via TCP commands"
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="image-display-config.yaml",
        help="Configuration file path (default: image-display-config.yaml)",
    )
    parser.add_argument(
        "--enum-monitors",
        action="store_true",
        help="Enumerate available monitors and exit",
    )

    args = parser.parse_args()

    # Handle monitor enumeration
    if args.enum_monitors:
        enumerate_monitors()
        sys.exit(0)

    config_path = args.config

    # Load configuration
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        logger.info(f"Configuration loaded from {config_path}")
    except FileNotFoundError:
        logger.warning(f"Config file not found: {config_path}, using defaults")
        config = {}
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        sys.exit(1)

    # The serial adapter that controls the projector's power must be
    # explicitly configured, no silent fallback to a guessed device path.
    if bool(config.get("projector_control", True)) and not config.get(
        "projector_device"
    ):
        logger.error(
            "Config error: 'projector_device' is required in %s. Set it to "
            "the serial port for the adapter connected to the projector "
            "(e.g. /dev/ttyUSB0), or set projector_control: false to disable "
            "projector power control entirely.",
            config_path,
        )
        sys.exit(1)

    # Create server
    server = ImageDisplayServer(config)

    # Create window on main thread
    server.create_window()

    # Start async server in background thread
    loop = asyncio.new_event_loop()
    server_thread = threading.Thread(
        target=run_async_server, args=(server, loop), daemon=True
    )
    server_thread.start()

    # Run pyglet on main thread
    try:
        logger.info("Starting pyglet event loop on main thread")
        pyglet.app.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        if server.server:
            loop.call_soon_threadsafe(server.server.close)
        server.video_registry.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
