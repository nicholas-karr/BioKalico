"""Test patterns for the projector calibration wizard.

Pure numpy so they can be built and tested without a display. Both return an
RGBA array of shape (height, width, 4), top row first.
"""

from __future__ import annotations

import numpy as np


def _blank(width: int, height: int) -> np.ndarray:
    frame = np.zeros((height, width, 4), dtype=np.uint8)
    frame[..., 3] = 255
    return frame


def clamp_rect(rect, width: int, height: int):
    """Clip (x, y, w, h) to the frame. Returns None when nothing is left."""
    x, y, w, h = (int(round(v)) for v in rect)
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, width), min(y + h, height)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def focus_pattern(width: int, height: int) -> np.ndarray:
    # One-pixel lines and fine checkers: they only look sharp when the image
    # is in focus. The frame edge and corner brackets show the corners.
    frame = _blank(width, height)
    white = (255, 255, 255, 255)

    frame[0, :] = white
    frame[-1, :] = white
    frame[:, 0] = white
    frame[:, -1] = white

    arm = max(min(width, height) // 12, 8)
    for ys in (slice(0, arm), slice(height - arm, height)):
        for xs in (slice(0, arm), slice(width - arm, width)):
            frame[ys, xs] = white
            frame[ys.start + 3 : ys.stop - 3, xs.start + 3 : xs.stop - 3] = (
                0,
                0,
                0,
                255,
            )

    cx, cy = width // 2, height // 2
    frame[cy, :] = white
    frame[:, cx] = white

    # Alternating 1 px checkers around the centre and inside each corner.
    size = max(min(width, height) // 10, 8)
    anchors = [
        (cx - size // 2, cy - size // 2),
        (arm + 6, arm + 6),
        (width - arm - 6 - size, arm + 6),
        (arm + 6, height - arm - 6 - size),
        (width - arm - 6 - size, height - arm - 6 - size),
    ]
    yy, xx = np.mgrid[0:size, 0:size]
    checker = ((yy + xx) % 2 == 0)[..., None] * np.array(white, dtype=np.uint8)
    checker[..., 3] = 255
    for ax, ay in anchors:
        if ax < 0 or ay < 0 or ax + size > width or ay + size > height:
            continue
        frame[ay : ay + size, ax : ax + size] = checker
    return frame


def rect_pattern(width: int, height: int, rect) -> np.ndarray:
    frame = _blank(width, height)
    clipped = clamp_rect(rect, width, height)
    if clipped is not None:
        x, y, w, h = clipped
        frame[y : y + h, x : x + w, :3] = 255
    return frame
