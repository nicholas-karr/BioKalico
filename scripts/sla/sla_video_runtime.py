#!/usr/bin/env python3
"""Utilities for loading and streaming SLA video payloads for Kalico macros."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)
MAX_EMBEDDED_VIDEO_BYTES = 128 * 1024 * 1024
FRAME_READ_TIMEOUT_SECONDS = 30.0


def _decode_b64_ascii(value: str) -> str:
    data = base64.b64decode(value.encode("ascii"), validate=True)
    return data.decode("utf-8")


def _safe_name_for_file(name: str) -> str:
    """Sanitize a video name for use as a filename.

    Two different original names can sanitize to the same string (e.g. names
    that differ only in characters replaced by "_"). A short deterministic
    hash of the original, pre-sanitization name is appended so that distinct
    names can never collide on disk, while re-processing the same name
    always yields the same path.
    """
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    suffix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{suffix}"


def extract_videos_from_gcode(
    gcode_path: str, output_dir: str
) -> Dict[str, str]:
    """Extract embedded videos and references from G-code comments.

    Supported comment protocols:
    New format:
    - ; bioslicer_sla_video begin name=<name> extruder=<n> bytes=<n> sha256=<hex>
    - ; bioslicer_sla_video <base64>
    - ; bioslicer_sla_video end name=<name>
    Old format (no sha256, underscore keywords, bare base64 data lines):
    - ; bioslicer_sla_video_begin name=<name> extruder=<n> bytes=<n>
    - ; <base64>
    - ; bioslicer_sla_video_end
    Reference:
    - ; bioslicer_sla_video ref name=<name> path=<utf8 path>
    """
    begin_re = re.compile(
        r"^;\s*bioslicer_sla_video\s+begin\s+name=(\S+)\s+extruder=(\d+)\s+bytes=(\d+)\s+sha256=([0-9a-fA-F]{64})\s*$"
    )
    begin_re_old = re.compile(
        r"^;\s*bioslicer_sla_video_begin\s+name=(\S+)\s+extruder=(\d+)\s+bytes=(\d+)\s*$"
    )
    data_re = re.compile(
        r"^;\s*bioslicer_sla_video\s+(?:data\s+)?([A-Za-z0-9+/=]+)\s*$"
    )
    data_re_old = re.compile(r"^;\s+([A-Za-z0-9+/=]+)\s*$")
    end_re = re.compile(r"^;\s*bioslicer_sla_video\s+end\s+name=(\S+)\s*$")
    end_re_old = re.compile(r"^;\s*bioslicer_sla_video_end\s*$")
    ref_path_re = re.compile(
        r"^;\s*bioslicer_sla_video\s+ref\s+name=(\S+)\s+extruder=(\d+)\s+path=(.+)$"
    )
    ref_path_b64_re = re.compile(
        r"^;\s*bioslicer_sla_video\s+ref\s+name=(\S+)\s+extruder=(\d+)\s+path_b64=(\S+)\s*$"
    )

    extracted: Dict[str, str] = {}
    active_name: Optional[str] = None
    active_expected_bytes = 0
    active_expected_sha256 = ""
    active_chunks: list[str] = []
    active_b64_chars = 0
    active_old_format = False

    output_base = Path(output_dir)
    output_base.mkdir(parents=True, exist_ok=True)

    with open(gcode_path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")

            ref_match = ref_path_re.match(line)
            if ref_match:
                name = ref_match.group(1)
                extracted[name] = ref_match.group(3).strip()
                continue

            ref_b64_match = ref_path_b64_re.match(line)
            if ref_b64_match:
                name = ref_b64_match.group(1)
                path_b64 = ref_b64_match.group(3)
                extracted[name] = _decode_b64_ascii(path_b64)
                continue

            begin_match = begin_re.match(line)
            if begin_match:
                if active_name is not None:
                    raise RuntimeError(
                        f"Found nested embedded video block while parsing {gcode_path}."
                    )
                active_name = begin_match.group(1)
                active_expected_bytes = int(begin_match.group(3))
                if active_expected_bytes > MAX_EMBEDDED_VIDEO_BYTES:
                    raise RuntimeError(
                        f"Embedded payload for {active_name} exceeds "
                        f"{MAX_EMBEDDED_VIDEO_BYTES} bytes."
                    )
                active_expected_sha256 = begin_match.group(4).lower()
                active_chunks = []
                active_b64_chars = 0
                active_old_format = False
                continue

            begin_match_old = begin_re_old.match(line)
            if begin_match_old:
                if active_name is not None:
                    raise RuntimeError(
                        f"Found nested embedded video block while parsing {gcode_path}."
                    )
                active_name = begin_match_old.group(1)
                active_expected_bytes = int(begin_match_old.group(3))
                if active_expected_bytes > MAX_EMBEDDED_VIDEO_BYTES:
                    raise RuntimeError(
                        f"Embedded payload for {active_name} exceeds "
                        f"{MAX_EMBEDDED_VIDEO_BYTES} bytes."
                    )
                active_expected_sha256 = ""
                active_chunks = []
                active_b64_chars = 0
                active_old_format = True
                continue

            if active_name is None:
                continue

            end_match = end_re.match(line)
            if end_match:
                end_name = end_match.group(1)
                if end_name != active_name:
                    raise RuntimeError(
                        f"Mismatched embedded video terminator: expected {active_name}, got {end_name}."
                    )

                payload = base64.b64decode(
                    "".join(active_chunks).encode("ascii"), validate=True
                )
                if len(payload) != active_expected_bytes:
                    raise RuntimeError(
                        f"Embedded payload length mismatch for {active_name}: "
                        f"expected {active_expected_bytes}, got {len(payload)}."
                    )

                digest = hashlib.sha256(payload).hexdigest()
                if digest != active_expected_sha256:
                    raise RuntimeError(
                        f"Embedded payload digest mismatch for {active_name}: "
                        f"expected {active_expected_sha256}, got {digest}."
                    )

                output_path = (
                    output_base / f"{_safe_name_for_file(active_name)}.mkv"
                )
                with open(output_path, "wb") as video_handle:
                    video_handle.write(payload)

                extracted[active_name] = str(output_path)
                active_name = None
                active_expected_bytes = 0
                active_expected_sha256 = ""
                active_chunks = []
                active_b64_chars = 0
                active_old_format = False
                continue

            if active_old_format and end_re_old.match(line):
                payload = base64.b64decode(
                    "".join(active_chunks).encode("ascii"), validate=True
                )
                if len(payload) != active_expected_bytes:
                    raise RuntimeError(
                        f"Embedded payload length mismatch for {active_name}: "
                        f"expected {active_expected_bytes}, got {len(payload)}."
                    )

                output_path = (
                    output_base / f"{_safe_name_for_file(active_name)}.mkv"
                )
                with open(output_path, "wb") as video_handle:
                    video_handle.write(payload)

                extracted[active_name] = str(output_path)
                active_name = None
                active_expected_bytes = 0
                active_expected_sha256 = ""
                active_chunks = []
                active_b64_chars = 0
                active_old_format = False
                continue

            data_match = data_re.match(line)
            if data_match:
                chunk = data_match.group(1)
                active_chunks.append(chunk)
                active_b64_chars += len(chunk)
                if active_b64_chars > (
                    (active_expected_bytes + 2) // 3 * 4 + 4
                ):
                    raise RuntimeError(
                        f"Embedded payload data exceeds declared length "
                        f"for {active_name}."
                    )
                continue

            if active_old_format:
                data_match_old = data_re_old.match(line)
                if data_match_old:
                    chunk = data_match_old.group(1)
                    active_chunks.append(chunk)
                    active_b64_chars += len(chunk)
                    if active_b64_chars > (
                        (active_expected_bytes + 2) // 3 * 4 + 4
                    ):
                        raise RuntimeError(
                            f"Embedded payload data exceeds declared length "
                            f"for {active_name}."
                        )

    if active_name is not None:
        raise RuntimeError(
            f"Unterminated embedded video block for {active_name} in {gcode_path}."
        )

    return extracted


@dataclass
class VideoMetadata:
    name: str
    path: str
    width: int
    height: int
    fps: float
    frame_count: int
    codec: str


class FFmpegVideoStream:
    """Persistent ffmpeg raw RGBA stream with frame-index state."""

    def __init__(
        self,
        name: str,
        path: str,
        hwaccel: str = "auto",
        hw_decoder: Optional[str] = None,
    ) -> None:
        self.name = name
        self.path = os.path.abspath(os.path.expanduser(path))
        self.hwaccel = hwaccel
        self.hw_decoder = hw_decoder
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.current_frame = 0
        self._lock = threading.RLock()

        self.metadata = self._probe_video(self.path)
        self.frame_size_bytes = self.metadata.width * self.metadata.height * 4

    def _probe_video(self, path: str) -> VideoMetadata:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration",
            "-of",
            "json",
            path,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            # This runs while the caller (VideoRegistry.load_video) holds the
            # registry's shared lock, so a hung ffprobe must fail cleanly for
            # this one video rather than blocking the lock indefinitely.
            raise RuntimeError(f"ffprobe timed out probing {path}")
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed for {path}: {proc.stderr.strip()}"
            )

        payload = json.loads(proc.stdout)
        streams = payload.get("streams", [])
        if not streams:
            raise RuntimeError(f"No video stream found in {path}")

        stream = streams[0]
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
        if width <= 0 or height <= 0:
            raise RuntimeError(
                f"Invalid video dimensions in {path}: {width}x{height}"
            )

        fps = 0.0
        avg_frame_rate = stream.get("avg_frame_rate", "0/1")
        try:
            num_str, den_str = avg_frame_rate.split("/", 1)
            num = float(num_str)
            den = float(den_str)
            if den > 0:
                fps = num / den
        except Exception:
            fps = 0.0

        frame_count = 0
        nb_frames = stream.get("nb_frames")
        if nb_frames is not None and str(nb_frames).isdigit():
            frame_count = int(nb_frames)
        else:
            duration_s = 0.0
            fmt = payload.get("format", {})
            try:
                duration_s = float(fmt.get("duration", 0.0))
            except Exception:
                duration_s = 0.0
            if fps > 0 and duration_s > 0:
                frame_count = int(round(duration_s * fps))

        return VideoMetadata(
            name=self.name,
            path=path,
            width=width,
            height=height,
            fps=fps,
            frame_count=frame_count,
            codec=str(stream.get("codec_name", "unknown")),
        )

    def close(self) -> None:
        with self._lock:
            if self.process is None:
                return

            proc = self.process
            self.process = None

            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

    def _spawn(self, start_frame: int) -> None:
        self.close()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
        ]

        if self.hwaccel:
            cmd.extend(["-hwaccel", self.hwaccel])
        if self.hw_decoder:
            cmd.extend(["-c:v", self.hw_decoder])

        cmd.extend(["-i", self.path])

        if start_frame > 0:
            cmd.extend(["-vf", f"select='gte(n,{start_frame})'"])

        cmd.extend(
            [
                "-an",
                "-sn",
                "-dn",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgba",
                "-vsync",
                "0",
                "-",
            ]
        )

        logger.info(
            "Starting ffmpeg stream for %s at frame %d", self.name, start_frame
        )
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            # An undrained stderr PIPE can fill and block ffmpeg. Failures
            # show up in the exit status anyway.
            stderr=subprocess.DEVNULL,
        )
        self.current_frame = start_frame

    def _read_exact_frame(self) -> Optional[bytes]:
        if self.process is None or self.process.stdout is None:
            return None

        out = bytearray()
        deadline = time.monotonic() + FRAME_READ_TIMEOUT_SECONDS
        fd = self.process.stdout.fileno()
        while len(out) < self.frame_size_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise RuntimeError(
                    f"Timed out reading frame for {self.name} after "
                    f"{FRAME_READ_TIMEOUT_SECONDS:g} seconds."
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                self.close()
                raise RuntimeError(
                    f"Timed out reading frame for {self.name} after "
                    f"{FRAME_READ_TIMEOUT_SECONDS:g} seconds."
                )
            chunk = os.read(fd, self.frame_size_bytes - len(out))
            if not chunk:
                break
            out.extend(chunk)

        if len(out) == 0:
            return None

        if len(out) != self.frame_size_bytes:
            raise RuntimeError(
                f"Incomplete frame read for {self.name}: expected {self.frame_size_bytes}, "
                f"got {len(out)}."
            )

        return bytes(out)

    def get_frame(self, frame_index: int) -> bytes:
        with self._lock:
            if frame_index < 0:
                raise ValueError("frame index must be >= 0")

            if self.process is None or frame_index < self.current_frame:
                self._spawn(frame_index)

            while self.current_frame < frame_index:
                skipped = self._read_exact_frame()
                if skipped is None:
                    raise IndexError(
                        f"Requested frame {frame_index}, but stream ended at "
                        f"frame {self.current_frame}."
                    )
                self.current_frame += 1

            frame = self._read_exact_frame()
            if frame is None:
                raise IndexError(
                    f"Requested frame {frame_index}, but stream ended at "
                    f"frame {self.current_frame}."
                )
            self.current_frame += 1
            return frame

    def to_dict(self) -> dict:
        return {
            "name": self.metadata.name,
            "path": self.metadata.path,
            "width": self.metadata.width,
            "height": self.metadata.height,
            "fps": self.metadata.fps,
            "frame_count": self.metadata.frame_count,
            "codec": self.metadata.codec,
            "current_frame": self.current_frame,
            "hwaccel": self.hwaccel,
            "hw_decoder": self.hw_decoder,
        }


class VideoRegistry:
    """Named video collection with persistent ffmpeg decode state.

    All public methods hold ``self._lock`` for their full body. The registry is
    called from multiple connection-handler threads via ``asyncio.to_thread``,
    so without a lock a concurrent unload/reload can close and null out an
    ``FFmpegVideoStream`` while another thread is mid-read on it (torn-down
    pipe -> spurious errors), or two concurrent loads of the same name can both
    pass the "already loaded" check and leak an ffmpeg subprocess. The lock is
    reentrant because load_videos_from_gcode calls load_video internally.
    """

    def __init__(
        self,
        cache_dir: str,
        hwaccel: str = "auto",
        hw_decoder: Optional[str] = None,
    ) -> None:
        self.cache_dir = os.path.abspath(os.path.expanduser(cache_dir))
        self.hwaccel = hwaccel
        self.hw_decoder = hw_decoder
        self.videos: Dict[str, FFmpegVideoStream] = {}
        self._lock = threading.RLock()

        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        with self._lock:
            for stream in self.videos.values():
                stream.close()
            self.videos.clear()

    def load_video(
        self,
        name: str,
        path: str,
        hwaccel: Optional[str] = None,
        hw_decoder: Optional[str] = None,
    ) -> dict:
        if not name:
            raise ValueError("name is required")
        if not path:
            raise ValueError("path is required")

        resolved_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(resolved_path):
            raise FileNotFoundError(f"Video file not found: {resolved_path}")

        with self._lock:
            if name in self.videos:
                old_stream = self.videos[name]
                aliases = [k for k, v in self.videos.items() if v is old_stream]
                for k in aliases:
                    del self.videos[k]
                old_stream.close()

            stream = FFmpegVideoStream(
                name=name,
                path=resolved_path,
                hwaccel=hwaccel or self.hwaccel,
                hw_decoder=hw_decoder or self.hw_decoder,
            )
            self.videos[name] = stream
            return stream.to_dict()

    def unload_video(self, name: str) -> bool:
        with self._lock:
            stream = self.videos.pop(name, None)
            if stream is None:
                return False
            # Remove any alias keys that point to the same stream object.
            aliases = [k for k, v in self.videos.items() if v is stream]
            for k in aliases:
                del self.videos[k]
            stream.close()
            return True

    def unload_all(self) -> int:
        with self._lock:
            count = len(self.videos)
            for stream in self.videos.values():
                stream.close()
            self.videos.clear()
            return count

    def list_videos(self) -> dict:
        with self._lock:
            # Numeric keys are internal 1-based aliases registered by
            # load_videos_from_gcode (see below) so slicers can reference
            # videos as NAME=1, NAME=2... They point at the same
            # FFmpegVideoStream as a real name and aren't videos in their
            # own right, so exclude them from the listing.
            return {
                name: stream.to_dict()
                for name, stream in self.videos.items()
                if not name.isdigit()
            }

    def get_frame(self, name: str, frame_index: int) -> Tuple[int, int, bytes]:
        with self._lock:
            if name not in self.videos:
                raise KeyError(f"Video not loaded: {name}")

            stream = self.videos[name]
            # Pin the stream against unload/reload, then release the global
            # registry lock before the potentially slow ffmpeg read.
            stream._lock.acquire()
            width = stream.metadata.width
            height = stream.metadata.height
        try:
            frame = stream.get_frame(frame_index)
            return width, height, frame
        finally:
            stream._lock.release()

    def load_videos_from_gcode(self, gcode_path: str) -> dict:
        if not gcode_path:
            raise ValueError("gcode_path is required")

        resolved = os.path.abspath(os.path.expanduser(gcode_path))
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"G-code file not found: {resolved}")

        extracted = extract_videos_from_gcode(resolved, self.cache_dir)
        with self._lock:
            loaded = {}
            for idx, (name, path) in enumerate(extracted.items(), start=1):
                info = self.load_video(name, path)
                loaded[name] = info
                # Also register a 1-based sequential index alias so slicers that
                # reference videos as NAME=1, NAME=2... work alongside named lookups.
                index_key = str(idx)
                if index_key != name:
                    self.videos[index_key] = self.videos[name]
            return loaded
