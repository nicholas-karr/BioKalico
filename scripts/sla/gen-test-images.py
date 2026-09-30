#!/usr/bin/env python3
"""
Generate and display test images via the image-display TCP server.

Available patterns:
- blue: solid blue frame for display verification
- checkerboard: 64-region checkerboard calibration pattern with increasing
    square sizes
- nested-squares: square outlines sized to the screen height with a
    configurable horizontal offset for build-plate alignment
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def _drm_preferred_resolution() -> tuple[int, int] | None:
    """Read the preferred resolution from /sys/class/drm (no X11 needed)."""
    try:
        entries = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        return None
    for entry in entries:
        if "-" not in entry:
            continue
        base = f"/sys/class/drm/{entry}"
        try:
            if open(f"{base}/status").read().strip() != "connected":
                continue
            if open(f"{base}/enabled").read().strip() != "enabled":
                continue
            modes = [l.strip() for l in open(f"{base}/modes") if l.strip()]
        except OSError:
            continue
        if not modes:
            continue
        m = re.match(r"^(\d+)x(\d+)", modes[0])
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _xrandr_preferred_resolution() -> tuple[int, int] | None:
    display = os.environ.get("DISPLAY")
    if not display:
        return None

    try:
        proc = subprocess.run(
            ["xrandr", "--current"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None

    output_re = re.compile(
        r"^(?P<name>\S+)\s+connected(?:\s+primary)?\s+(?P<w>\d+)x(?P<h>\d+)\+"
    )
    current_re = re.compile(
        r"^Screen\s+\d+:\s+minimum\s+\d+\s+x\s+\d+,\s+current\s+(\d+)\s+x\s+(\d+),"
    )
    best_output = None
    screen_size = None
    for line in proc.stdout.splitlines():
        match = output_re.match(line.strip())
        if match:
            width = int(match.group("w"))
            height = int(match.group("h"))
            if (
                best_output is None
                or width * height > best_output[0] * best_output[1]
            ):
                best_output = (width, height)
            continue

        match = current_re.match(line.strip())
        if match:
            screen_size = (int(match.group(1)), int(match.group(2)))

    if best_output is not None:
        return best_output

    return screen_size


def make_blue_image(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), (0, 80, 220))


def make_white_image(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), (255, 255, 255))


def make_checkerboard_image(
    width: int, height: int, regions: int = 8
) -> Image.Image:
    tile_w = width // regions
    tile_h = height // regions
    image = np.full((height, width, 3), 245, dtype=np.uint8)

    # Square sizes range from crisp to coarse across the 8x8 grid.
    min_square = 4
    max_square = max(min(tile_w, tile_h) // 2, min_square)
    progression = np.geomspace(min_square, max_square, num=regions * regions)

    for row in range(regions):
        for col in range(regions):
            index = row * regions + col
            square = max(1, int(round(progression[index])))

            x0 = col * tile_w
            y0 = row * tile_h
            x1 = width if col == regions - 1 else (col + 1) * tile_w
            y1 = height if row == regions - 1 else (row + 1) * tile_h

            xs = np.arange(x0, x1)
            ys = np.arange(y0, y1)
            grid_x = (xs - x0) // square
            grid_y = (ys - y0) // square
            pattern = (grid_y[:, None] + grid_x[None, :]) % 2

            # Alternate the phase between neighboring tiles so edges are easy to
            # read while the local frequency still increases across the grid.
            if (row + col) % 2 == 1:
                pattern = 1 - pattern

            # Neutral gray background and strong black/white checker contrast.
            tile = np.where(pattern[:, :, None] == 1, 20, 235).astype(np.uint8)
            image[y0:y1, x0:x1] = tile

    # Draw thin dividers so the 64 regions remain clearly segmented.
    image[:, ::tile_w] = 0
    image[::tile_h, :] = 0

    return Image.fromarray(image, "RGB")


_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
    "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
]

_TEXT_BLOCK = (
    "The quick brown fox jumps over the lazy dog. "
    "0123456789 !@#$%^&*()-_=+[]{}|;:,.<>?/\\ "
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz "
    "Sphinx of black quartz, judge my vow. "
    "Pack my box with five dozen liquor jugs. "
)


def make_dense_text_image(
    width: int, height: int, font_size: int = 0
) -> Image.Image:
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)

    font = None
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            size = font_size or max(10, height // 80)
            font = ImageFont.truetype(path, size)
            break
    if font is None:
        font = ImageFont.load_default()

    # Measure a single character to compute the grid
    bbox = font.getbbox("A")
    ch_w = bbox[2] - bbox[0]
    ch_h = bbox[3] - bbox[1]
    line_h = int(ch_h * 1.15)
    cols = max(1, width // max(1, ch_w))

    text_cycle = _TEXT_BLOCK * ((cols // len(_TEXT_BLOCK)) + 2)
    y = 0
    row = 0
    while y < height:
        # Stagger each line by one character so vertical runs don't align
        offset = row % len(_TEXT_BLOCK)
        line = text_cycle[offset : offset + cols]
        draw.text((0, y), line, font=font, fill=(220, 220, 220))
        y += line_h
        row += 1

    return image


def make_nested_squares_image(
    width: int,
    height: int,
    horizontal_offset: int = 0,
    vertical_offset: int = 0,
    count: int = 24,
    line_width: int = 4,
) -> Image.Image:
    image = np.full((height, width, 3), 250, dtype=np.uint8)

    center_x = (width // 2) + int(horizontal_offset)
    center_y = (height // 2) + int(vertical_offset)

    max_side = max(1, min(width, height))
    min_side = max(8, max_side // (count * 6))
    sizes = np.geomspace(min_side, max_side, num=count)

    for index, raw_size in enumerate(sizes):
        side = max(1, int(round(raw_size)))
        half = side // 2

        x0 = max(0, center_x - half)
        y0 = max(0, center_y - half)
        x1 = min(width, x0 + side)
        y1 = min(height, y0 + side)

        color = 0 if index % 2 == 0 else 255

        top = slice(y0, min(height, y0 + line_width))
        bottom = slice(max(0, y1 - line_width), y1)
        left = slice(x0, min(width, x0 + line_width))
        right = slice(max(0, x1 - line_width), x1)

        image[top, x0:x1] = color
        image[bottom, x0:x1] = color
        image[y0:y1, left] = color
        image[y0:y1, right] = color

    # Add a subtle crosshair to make centering easier when dialing in offsets.
    cross_color = np.array([40, 40, 40], dtype=np.uint8)
    if 0 <= center_x < width:
        image[:, max(0, center_x - 1) : min(width, center_x + 1)] = cross_color
    if 0 <= center_y < height:
        image[max(0, center_y - 1) : min(height, center_y + 1), :] = cross_color

    return Image.fromarray(image, "RGB")


def choose_dimensions(
    screen_width: int, screen_height: int, aspect: str
) -> tuple[int, int]:
    if aspect == "screen":
        return screen_width, screen_height
    if aspect == "16:9":
        unit = max(1, screen_height // 9)
        return unit * 16, unit * 9
    if aspect == "4:3":
        unit = max(1, screen_height // 3)
        return unit * 4, unit * 3
    if aspect in {"1:1", "square"}:
        side = max(1, min(screen_width, screen_height))
        return side, side
    raise ValueError(f"Unsupported aspect selector: {aspect}")


def send(host: str, port: int, command: dict) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect((host, port))
        s.sendall((json.dumps(command) + "\n").encode())
        with s.makefile("r") as f:
            return json.loads(f.readline())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a display test image and send it to the SLA image server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "host",
        nargs="?",
        default="localhost",
        help="Server hostname or IP address",
    )
    parser.add_argument(
        "port", nargs="?", default=5555, type=int, help="Server port number"
    )
    parser.add_argument(
        "--aspect",
        choices=["screen", "16:9", "4:3", "1:1", "square"],
        default="screen",
        help="Output aspect ratio to generate",
    )
    parser.add_argument(
        "--pattern",
        choices=[
            "blue",
            "white",
            "checkerboard",
            "nested-squares",
            "dense-text",
        ],
        default="checkerboard",
        help="Test image to generate",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=0,
        help="Font size for dense-text (0 = auto)",
    )
    parser.add_argument(
        "--regions", type=int, default=8, help="Checkerboard grid size per axis"
    )
    parser.add_argument(
        "--offset-x",
        type=int,
        default=0,
        help="Horizontal offset for nested squares",
    )
    parser.add_argument(
        "--offset-y",
        type=int,
        default=0,
        help="Vertical offset for nested squares",
    )
    parser.add_argument(
        "--nested-count",
        type=int,
        default=24,
        help="Number of nested square outlines",
    )
    parser.add_argument(
        "--line-width",
        type=int,
        default=4,
        help="Outline width for nested squares",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Generate the image but do not send it to the display server",
    )
    return parser


def main():
    args = build_parser().parse_args()

    res = _xrandr_preferred_resolution() or _drm_preferred_resolution()
    if res:
        screen_w, screen_h = res
        print(f"Detected display: {screen_w}x{screen_h}")
    else:
        screen_w, screen_h = 4096, 2160
        print(
            f"Could not detect display resolution, using {screen_w}x{screen_h}"
        )

    w, h = choose_dimensions(screen_w, screen_h, args.aspect)
    if args.aspect != "screen":
        print(f"Using {args.aspect} output size: {w}x{h}")

    outdir = tempfile.mkdtemp(prefix="bioslicer-test-")

    if args.pattern == "blue":
        image_path = os.path.join(outdir, "blue.png")
        print(f"Generating {w}x{h} solid blue…")
        make_blue_image(w, h).save(image_path)
    elif args.pattern == "white":
        image_path = os.path.join(outdir, "white.png")
        print(f"Generating {w}x{h} solid white…")
        make_white_image(w, h).save(image_path)
    elif args.pattern == "checkerboard":
        image_path = os.path.join(outdir, "checkerboard.png")
        print(
            f"Generating {w}x{h} {args.regions}x{args.regions} checkerboard calibration…"
        )
        make_checkerboard_image(w, h, regions=args.regions).save(image_path)
    elif args.pattern == "dense-text":
        image_path = os.path.join(outdir, "dense-text.png")
        print(f"Generating {w}x{h} dense text…")
        make_dense_text_image(w, h, font_size=args.font_size).save(image_path)
    else:
        image_path = os.path.join(outdir, "nested-squares.png")
        print(
            f"Generating {w}x{h} nested squares with offset "
            f"({args.offset_x}, {args.offset_y})…"
        )
        make_nested_squares_image(
            w,
            h,
            horizontal_offset=args.offset_x,
            vertical_offset=args.offset_y,
            count=args.nested_count,
            line_width=args.line_width,
        ).save(image_path)

    print(f"  → {image_path}")

    if args.no_send:
        print(
            "\nGeneration complete; image not sent because --no-send was set."
        )
        return

    print(f"\nSending {args.pattern} image to {args.host}:{args.port}…")
    try:
        r = send(
            args.host, args.port, {"type": "DISPLAY_IMAGE", "path": image_path}
        )
        print(f"  {r}")
    except Exception as e:
        print(f"  Error: {e}")


if __name__ == "__main__":
    main()
