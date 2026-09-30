import threading
import time
from types import SimpleNamespace

import pytest

from scripts.sla import sla_video_runtime


def test_embedded_video_declared_size_is_bounded(tmp_path):
    gcode = tmp_path / "oversized.gcode"
    gcode.write_text(
        "; bioslicer_sla_video begin name=huge extruder=0 "
        f"bytes={sla_video_runtime.MAX_EMBEDDED_VIDEO_BYTES + 1} "
        f"sha256={'0' * 64}\n"
    )

    with pytest.raises(RuntimeError, match="exceeds"):
        sla_video_runtime.extract_videos_from_gcode(
            str(gcode), str(tmp_path / "cache")
        )


def test_frame_read_times_out_instead_of_blocking(monkeypatch):
    stream = sla_video_runtime.FFmpegVideoStream.__new__(
        sla_video_runtime.FFmpegVideoStream
    )
    stream.name = "stalled"
    stream.frame_size_bytes = 4
    stream.process = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 123))
    closed = []
    stream.close = lambda: closed.append(True)
    monkeypatch.setattr(
        sla_video_runtime.select, "select", lambda *_args: ([], [], [])
    )

    with pytest.raises(RuntimeError, match="Timed out"):
        stream._read_exact_frame()

    assert closed == [True]


def test_unload_blocks_until_concurrent_read_finishes(tmp_path):
    """VideoRegistry.get_frame() takes stream._lock before calling
    stream.get_frame(), so an unload can't close the stream mid-read.

    fake_get_frame sleeps before touching stream._lock, so this only passes
    if the registry's own lock blocks the unload.
    """
    registry = sla_video_runtime.VideoRegistry(str(tmp_path / "cache"))

    stream = sla_video_runtime.FFmpegVideoStream.__new__(
        sla_video_runtime.FFmpegVideoStream
    )
    stream.name = "slow"
    stream._lock = threading.RLock()
    stream.process = None
    stream.metadata = SimpleNamespace(width=4, height=4)

    dispatched = threading.Event()
    release_read = threading.Event()
    close_calls = []

    def fake_get_frame(frame_index):
        # Runs before the read takes stream._lock, so only a caller holding
        # the lock already (as VideoRegistry.get_frame does) prevents a
        # concurrent close().
        dispatched.set()
        time.sleep(0.2)
        with stream._lock:
            finished_waiting = release_read.wait(timeout=5)
            if not finished_waiting:
                raise AssertionError("test never released the simulated read")
            return b"\x00" * 64

    def fake_close():
        with stream._lock:
            close_calls.append(time.monotonic())

    stream.get_frame = fake_get_frame
    stream.close = fake_close

    registry.videos["slow"] = stream

    read_result = {}

    def do_read():
        read_result["frame"] = registry.get_frame("slow", 0)

    reader_thread = threading.Thread(target=do_read)
    reader_thread.start()
    assert dispatched.wait(timeout=2), "reader never started"

    unload_result = {}
    unload_finished = threading.Event()

    def do_unload():
        unload_result["returned"] = registry.unload_video("slow")
        unload_finished.set()

    unloader_thread = threading.Thread(target=do_unload)
    unloader_thread.start()

    # unload_video() must wait in close() while the read holds
    # stream._lock. Give it time to finish early if it were going to.
    still_blocked = not unload_finished.wait(timeout=0.3)
    assert still_blocked, (
        "unload_video() returned while a read on the same stream was still "
        "in flight - the per-stream lock hand-off did not block it"
    )
    assert close_calls == [], "close() ran before the in-flight read finished"

    # Let the simulated slow read complete, which should unblock the unload.
    release_read.set()

    reader_thread.join(timeout=2)
    unloader_thread.join(timeout=2)

    assert read_result["frame"] == (4, 4, b"\x00" * 64)
    assert unload_result["returned"] is True
    assert len(close_calls) == 1
    assert "slow" not in registry.videos
