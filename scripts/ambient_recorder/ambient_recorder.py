#!/usr/bin/env python3
# Ambient recorder: continuous, low-rate H.264 timelapse of the printer,
# compiled into one video file per calendar day.
#
# Copyright (C) 2026 Nicholas Karr
#
# This file may be distributed under the terms of the GNU GPLv3 license
#
# Records whether or not a print is running, unlike the per-print timelapse.
# Off by default; see ~/printer_data/config/ambient-recorder-config.yaml.
#
# Snapshots from crowsnest are piped straight into ffmpeg/libx264, so an
# unchanging scene takes almost no space.
#
# Each run records a fragmented mp4 "session" file under <output_dir>/tmp/,
# which stays playable if the process is killed. After a day ends, that
# day's sessions are joined (without re-encoding) into
# <output_dir>/<YYYY-MM-DD>.mp4. Days missed while the host was down are
# joined at startup.

from __future__ import annotations

import datetime
import glob
import logging
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

CONFIG_ENV = "CONFIG_PATH"
DEFAULT_CONFIG_PATH = os.path.expanduser(
    "~/printer_data/config/ambient-recorder-config.yaml"
)
SESSION_PREFIX = "session-"
SESSION_SUFFIX = ".mp4"
# session-YYYY-MM-DD_HH-MM-SS.mp4, grouped by date when finalizing.
SESSION_TIME_FMT = "%Y-%m-%d_%H-%M-%S"
DAILY_DATE_FMT = "%Y-%m-%d"

DEFAULTS = {
    "enabled": False,
    "snapshot_url": "http://127.0.0.1:8080/snapshot",
    "capture_interval_seconds": 10.0,
    # Inside Moonraker's timelapse folder so Mainsail's Timelapse tab shows it.
    "output_dir": os.path.expanduser("~/printer_data/timelapse/ambient/"),
    # Playback rate. At 1 fps, seeking +-1s steps one captured frame.
    "output_framerate": 1,
    "crf": 28,
    "preset": "veryfast",
    # A bare name is looked up on PATH.
    "ffmpeg_binary_path": "ffmpeg",
    # How much wall-clock footage an unclean crash may lose (the open mp4
    # fragment). Also the keyframe spacing, so lower = safer but larger.
    "keyframe_interval_seconds": 60.0,
    "snapshot_timeout_seconds": 5.0,
    # Largest snapshot accepted.
    "max_snapshot_bytes": 10 * 1024 * 1024,
    # Daily videos older than this are deleted. 0 keeps them forever.
    "retention_days": 30,
}


def parse_config(path: str) -> dict:
    # Flat "key: value" lines, parsed without a YAML library so this runs
    # under the system python3. Unknown keys are ignored.
    config = dict(DEFAULTS)
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError as err:
        logging.warning(
            "could not read config %s (%s), using defaults", path, err
        )
        return config

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'").strip()
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                config[key] = value.lower() in ("true", "1", "yes", "on")
            elif isinstance(default, int):
                config[key] = int(value)
            elif isinstance(default, float):
                config[key] = float(value)
            else:
                config[key] = (
                    os.path.expanduser(value)
                    if key.endswith(("_dir", "_path"))
                    else value
                )
        except ValueError:
            logging.warning("bad value for %s: %r, keeping default", key, value)
    return config


def validate_config(config: dict) -> list[str]:
    errors = []
    for key in (
        "capture_interval_seconds",
        "output_framerate",
        "keyframe_interval_seconds",
        "snapshot_timeout_seconds",
        "max_snapshot_bytes",
    ):
        value = config[key]
        if not math.isfinite(value) or value <= 0:
            errors.append(f"{key} must be a finite number greater than zero")
    crf = config["crf"]
    if not isinstance(crf, int) or not 0 <= crf <= 51:
        errors.append("crf must be an integer from 0 through 51")
    for key in ("snapshot_url", "output_dir", "ffmpeg_binary_path"):
        if not str(config[key]).strip():
            errors.append(f"{key} must not be empty")
    return errors


class AmbientRecorder:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.output_dir = config["output_dir"]
        self.tmp_dir = os.path.join(self.output_dir, "tmp")
        self.proc: subprocess.Popen | None = None
        self.session_date: str | None = None
        self.running = True

    def _gop(self) -> int:
        frames = round(
            self.config["keyframe_interval_seconds"]
            / max(self.config["capture_interval_seconds"], 0.001)
        )
        return max(1, int(frames))

    def _start_session(self, now: datetime.datetime) -> None:
        os.makedirs(self.tmp_dir, exist_ok=True)
        session_stem = os.path.join(
            self.tmp_dir,
            SESSION_PREFIX + now.strftime(SESSION_TIME_FMT),
        )
        session_path = session_stem + SESSION_SUFFIX
        sequence = 2
        while os.path.exists(session_path):
            session_path = f"{session_stem}-{sequence}{SESSION_SUFFIX}"
            sequence += 1
        gop = self._gop()
        cmd = [
            self.config["ffmpeg_binary_path"],
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            # With the default probesize (5 MB) ffmpeg writes nothing until
            # hours of snapshots have arrived.
            "-probesize",
            "32",
            # Snapshots are always JPEG.
            "-f",
            "image2pipe",
            "-framerate",
            str(self.config["output_framerate"]),
            "-c:v",
            "mjpeg",
            "-i",
            "-",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            str(self.config["preset"]),
            # Without zerolatency, x264 holds 10+ frames (minutes of footage)
            # before writing anything.
            "-tune",
            "zerolatency",
            "-crf",
            str(self.config["crf"]),
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(gop),
            "-movflags",
            "+frag_keyframe+empty_moov",
            "-flush_packets",
            "1",
            "-f",
            "mp4",
            session_path,
        ]
        logging.info(
            "starting session %s (gop=%d)", os.path.basename(session_path), gop
        )
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.session_date = now.strftime(DAILY_DATE_FMT)

    def _stop_session(self) -> None:
        if self.proc is None:
            return
        logging.info("closing current session")
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logging.warning("ffmpeg did not exit in time, terminating")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self.session_date = None

    def _session_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write_frame(self, jpeg: bytes) -> bool:
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(jpeg)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            logging.warning("ffmpeg pipe broke mid-session")
            return False

    def _sessions_by_date(self) -> dict:
        groups: dict = {}
        pattern = os.path.join(
            self.tmp_dir, SESSION_PREFIX + "*" + SESSION_SUFFIX
        )
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)
            match = re.match(
                re.escape(SESSION_PREFIX) + r"(\d{4}-\d{2}-\d{2})_",
                name,
            )
            if match:
                groups.setdefault(match.group(1), []).append(path)
        return groups

    def finalize_past_dates(self) -> None:
        # Only dates before today; today isn't finished.
        today = datetime.date.today().strftime(DAILY_DATE_FMT)
        for date, sessions in sorted(self._sessions_by_date().items()):
            if date >= today:
                continue
            self._finalize_date(date, sessions)
        self._prune_old_dailies()

    def _prune_old_dailies(self) -> None:
        retention_days = self.config["retention_days"]
        if retention_days <= 0:
            return
        cutoff = datetime.date.today() - datetime.timedelta(days=retention_days)
        pattern = os.path.join(self.output_dir, "*.mp4")
        for path in glob.glob(pattern):
            match = re.match(
                r"(\d{4}-\d{2}-\d{2})\.mp4$", os.path.basename(path)
            )
            if not match:
                continue
            try:
                file_date = datetime.datetime.strptime(
                    match.group(1), DAILY_DATE_FMT
                ).date()
            except ValueError:
                continue
            if file_date < cutoff:
                logging.info(
                    "removing daily recording past retention_days: %s",
                    os.path.basename(path),
                )
                self._safe_remove(path)

    def _finalize_date(self, date: str, sessions: list) -> None:
        daily_path = os.path.join(self.output_dir, date + ".mp4")
        if os.path.exists(daily_path):
            # Already joined; a crash happened before cleanup.
            logging.info(
                "daily %s already exists, cleaning up %d leftover session(s)",
                os.path.basename(daily_path),
                len(sessions),
            )
            self._remove_sessions(sessions)
            return

        list_path = os.path.join(self.tmp_dir, ".concat-" + date + ".txt")
        with open(list_path, "w") as f:
            for path in sessions:
                escaped = path.replace("'", r"'\''")
                f.write(f"file '{escaped}'\n")

        tmp_out = daily_path + ".partial"
        cmd = [
            self.config["ffmpeg_binary_path"],
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            # ffmpeg can't infer the format from a ".partial" name.
            "-f",
            "mp4",
            tmp_out,
        ]
        logging.info("finalizing %s from %d session(s)", date, len(sessions))
        try:
            subprocess.run(cmd, check=True)
        except (subprocess.CalledProcessError, OSError) as err:
            logging.error(
                "finalizing %s failed (%s), keeping sessions", date, err
            )
            self._safe_remove(tmp_out)
            self._safe_remove(list_path)
            return
        os.replace(tmp_out, daily_path)
        self._safe_remove(list_path)
        self._remove_sessions(sessions)
        logging.info("wrote %s", os.path.basename(daily_path))

    def _remove_sessions(self, sessions: list) -> None:
        for path in sessions:
            self._safe_remove(path)

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def _fetch_snapshot(self) -> bytes | None:
        # The socket timeout applies per read, so a slow camera could exceed
        # it overall. Read in chunks and check a total deadline.
        timeout = self.config["snapshot_timeout_seconds"]
        deadline = time.monotonic() + timeout
        limit = self.config["max_snapshot_bytes"]
        chunk_size = 64 * 1024
        try:
            with urllib.request.urlopen(
                self.config["snapshot_url"],
                timeout=timeout,
            ) as resp:
                chunks = []
                total = 0
                while True:
                    if time.monotonic() >= deadline:
                        logging.warning(
                            "snapshot fetch exceeded snapshot_timeout_seconds "
                            "(%.1fs)",
                            timeout,
                        )
                        return None
                    chunk = resp.read(min(chunk_size, limit + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        logging.warning(
                            "snapshot exceeds max_snapshot_bytes (%d)", limit
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception as err:
            logging.warning("snapshot fetch failed: %s", err)
            return None

    def run(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        # Catch up on any day(s) that ended while we were down.
        self.finalize_past_dates()

        interval = self.config["capture_interval_seconds"]
        while self.running:
            cycle_start = time.monotonic()
            now = datetime.datetime.now()
            today = now.strftime(DAILY_DATE_FMT)

            # Day rolled over while a session was live: close it, finalize the
            # now-complete past date(s), start fresh for today.
            if self.session_date is not None and self.session_date != today:
                self._stop_session()
                self.finalize_past_dates()

            if not self._session_alive():
                if self.proc is not None:
                    logging.warning("ffmpeg died, starting a new session")
                    self.proc = None
                    self.session_date = None
                self._start_session(now)

            jpeg = self._fetch_snapshot()
            if jpeg is not None:
                if not self._write_frame(jpeg):
                    # Pipe broke - drop this session, next loop starts a new
                    # one. The partial session file stays valid on disk.
                    self._stop_session()

            elapsed = time.monotonic() - cycle_start
            sleep_for = interval - elapsed
            # Interruptible sleep so SIGTERM is handled promptly.
            while sleep_for > 0 and self.running:
                nap = min(sleep_for, 1.0)
                time.sleep(nap)
                sleep_for -= nap

        # On stop, close the session and join past days. Today's sessions
        # are joined after today ends.
        self._stop_session()
        self.finalize_past_dates()
        logging.info("ambient recorder stopped")

    def request_stop(self, *_args) -> None:
        logging.info("stop requested")
        self.running = False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s ambient-recorder: %(message)s",
    )
    config_path = os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    config = parse_config(config_path)

    if not config["enabled"]:
        # Exit 0 so Restart=on-failure doesn't restart it.
        logging.info("disabled in %s, exiting", config_path)
        return 0
    config_errors = validate_config(config)
    if config_errors:
        for error in config_errors:
            logging.error("invalid config: %s", error)
        return 2

    recorder = AmbientRecorder(config)
    signal.signal(signal.SIGTERM, recorder.request_stop)
    signal.signal(signal.SIGINT, recorder.request_stop)
    recorder.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
