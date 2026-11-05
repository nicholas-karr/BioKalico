import subprocess
import sys

import pytest

from scripts import flash_usb

REAL_POPEN = subprocess.Popen
LEAVE_FLAGS = ["-p", "1-3.4", "-R", "-a", "0", "-s", "0x8000000:leave"]


def fake_dfuutil(monkeypatch, output, exit_code):
    script = "import sys; sys.stdout.write(%r); sys.exit(%d)" % (
        output,
        exit_code,
    )

    def popen(args, **kwargs):
        return REAL_POPEN([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(flash_usb.subprocess, "Popen", popen)


def test_leave_reset_after_complete_download_is_success(monkeypatch, capsys):
    fake_dfuutil(
        monkeypatch,
        "Download done.\nFile downloaded successfully\n"
        "Submitting leave request...\n"
        "dfu-util: Error during download get_status\n",
        74,
    )
    flash_usb.call_dfuutil(LEAVE_FLAGS, "klipper.bin", sudo=False)
    err = capsys.readouterr().err
    assert "File downloaded successfully" in err
    assert "download completed" in err


def test_incomplete_download_still_fails(monkeypatch):
    fake_dfuutil(
        monkeypatch,
        "Downloading element to address = 0x08000000\n"
        "dfu-util: Error during download get_status\n",
        74,
    )
    with pytest.raises(flash_usb.error):
        flash_usb.call_dfuutil(LEAVE_FLAGS, "klipper.bin", sudo=False)


def test_error_without_leave_request_still_fails(monkeypatch):
    fake_dfuutil(monkeypatch, "File downloaded successfully\n", 74)
    with pytest.raises(flash_usb.error):
        flash_usb.call_dfuutil(
            ["-p", "1-3.4", "-a", "0"], "klipper.bin", sudo=False
        )


def test_other_exit_codes_still_fail(monkeypatch):
    fake_dfuutil(monkeypatch, "File downloaded successfully\n", 1)
    with pytest.raises(flash_usb.error):
        flash_usb.call_dfuutil(LEAVE_FLAGS, "klipper.bin", sudo=False)
