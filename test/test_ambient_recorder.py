import datetime
import math
import time
from unittest import mock

from scripts.ambient_recorder.ambient_recorder import (
    DAILY_DATE_FMT,
    DEFAULTS,
    AmbientRecorder,
    parse_config,
    validate_config,
)


def test_parse_config_and_validate_valid_file(tmp_path):
    config_path = tmp_path / "ambient.yaml"
    config_path.write_text(
        "enabled: true\n"
        "capture_interval_seconds: 2.5\n"
        "output_framerate: 2\n"
        "crf: 30\n"
    )

    config = parse_config(str(config_path))

    assert config["enabled"] is True
    assert config["capture_interval_seconds"] == 2.5
    assert config["output_framerate"] == 2
    assert config["crf"] == 30
    assert config["ffmpeg_binary_path"] == "ffmpeg"
    assert validate_config(config) == []


def test_validate_config_rejects_values_that_would_restart_loop():
    config = dict(DEFAULTS)
    config.update(
        {
            "capture_interval_seconds": 0,
            "output_framerate": 0,
            "snapshot_timeout_seconds": math.inf,
            "crf": 99,
        }
    )

    errors = validate_config(config)

    assert any("capture_interval_seconds" in error for error in errors)
    assert any("output_framerate" in error for error in errors)
    assert any("snapshot_timeout_seconds" in error for error in errors)
    assert any("crf" in error for error in errors)


def test_fetch_snapshot_rejects_oversized_response():
    recorder = AmbientRecorder.__new__(AmbientRecorder)
    recorder.config = dict(DEFAULTS)
    recorder.config["max_snapshot_bytes"] = 4
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = b"12345"

    with mock.patch(
        "scripts.ambient_recorder.ambient_recorder.urllib.request.urlopen",
        return_value=response,
    ):
        assert recorder._fetch_snapshot() is None
        response.__enter__.return_value.read.assert_called_once_with(5)


def test_fetch_snapshot_returns_full_response_under_the_limit():
    recorder = AmbientRecorder.__new__(AmbientRecorder)
    recorder.config = dict(DEFAULTS)
    recorder.config["max_snapshot_bytes"] = 1024
    response = mock.MagicMock()
    # A short first chunk, then EOF: all chunks must be joined.
    response.__enter__.return_value.read.side_effect = [
        b"hello ",
        b"world",
        b"",
    ]

    with mock.patch(
        "scripts.ambient_recorder.ambient_recorder.urllib.request.urlopen",
        return_value=response,
    ):
        assert recorder._fetch_snapshot() == b"hello world"


def test_fetch_snapshot_aborts_on_slow_drip_past_deadline(monkeypatch):
    # A source that keeps sending a byte at a time must still hit the total
    # snapshot_timeout_seconds deadline.
    recorder = AmbientRecorder.__new__(AmbientRecorder)
    recorder.config = dict(DEFAULTS)
    recorder.config["snapshot_timeout_seconds"] = 5.0
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = b"x"  # drips forever

    # Move the clock past the deadline after the first chunk.
    times = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times, 100.0))

    with mock.patch(
        "scripts.ambient_recorder.ambient_recorder.urllib.request.urlopen",
        return_value=response,
    ):
        assert recorder._fetch_snapshot() is None


def test_prune_old_dailies_removes_only_past_retention(tmp_path):
    recorder = AmbientRecorder.__new__(AmbientRecorder)
    recorder.config = dict(DEFAULTS)
    recorder.config["retention_days"] = 30
    recorder.output_dir = str(tmp_path)

    today = datetime.date.today()
    old_date = (today - datetime.timedelta(days=31)).strftime(DAILY_DATE_FMT)
    recent_date = (today - datetime.timedelta(days=5)).strftime(DAILY_DATE_FMT)
    old_path = tmp_path / f"{old_date}.mp4"
    recent_path = tmp_path / f"{recent_date}.mp4"
    old_path.write_bytes(b"old")
    recent_path.write_bytes(b"recent")

    recorder._prune_old_dailies()

    assert not old_path.exists()
    assert recent_path.exists()


def test_prune_old_dailies_disabled_when_retention_is_zero(tmp_path):
    recorder = AmbientRecorder.__new__(AmbientRecorder)
    recorder.config = dict(DEFAULTS)
    recorder.config["retention_days"] = 0
    recorder.output_dir = str(tmp_path)

    ancient_date = "2000-01-01"
    ancient_path = tmp_path / f"{ancient_date}.mp4"
    ancient_path.write_bytes(b"ancient")

    recorder._prune_old_dailies()

    assert ancient_path.exists()
