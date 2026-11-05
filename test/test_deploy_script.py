import json
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "deploy-mainsail-kalico.sh"

SSH_STUB = """#!/bin/bash
# $1 is the target, $2 the remote command.
case "$2" in
    *curl*)
        [ "${STUB_CURL_FAIL:-0}" = 1 ] && exit 22
        printf '%s' "$STUB_PRINT_JSON"
        ;;
    *biokalico-installer*)
        echo "installer ran" >> "$STUB_LOG"
        ;;
esac
exit 0
"""

RSYNC_STUB = """#!/bin/bash
echo ">f+++++++++ klippy/example.py"
"""


def _status(state):
    return json.dumps({"result": {"status": {"print_stats": {"state": state}}}})


@pytest.fixture
def run_deploy(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("ssh", SSH_STUB), ("rsync", RSYNC_STUB)):
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "log"

    def run(print_json, *args, curl_fails=False):
        env = dict(
            os.environ,
            PATH="%s:%s" % (bindir, os.environ["PATH"]),
            STUB_PRINT_JSON=print_json,
            STUB_LOG=str(log),
            STUB_CURL_FAIL="1" if curl_fails else "0",
        )
        proc = subprocess.run(
            [str(SCRIPT), "--target", "printer", "-y", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
        )
        installer_ran = log.exists() and "installer ran" in log.read_text()
        return proc, installer_ran

    return run


def test_deploys_when_the_printer_is_idle(run_deploy):
    proc, installer_ran = run_deploy(_status("standby"))
    assert proc.returncode == 0, proc.stderr
    assert installer_ran


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_refuses_during_a_print(run_deploy, state):
    proc, installer_ran = run_deploy(_status(state))
    assert proc.returncode == 1
    assert "active print" in proc.stderr
    assert not installer_ran


@pytest.mark.parametrize(
    "response", ["", "not json", "{}", '{"error": {"code": 401}}']
)
def test_refuses_when_the_print_state_cannot_be_read(run_deploy, response):
    proc, installer_ran = run_deploy(response)
    assert proc.returncode == 1
    assert "invalid print-state response" in proc.stderr
    assert not installer_ran


def test_refuses_when_moonraker_is_unreachable(run_deploy):
    proc, installer_ran = run_deploy("", curl_fails=True)
    assert proc.returncode == 1
    assert "could not verify print state" in proc.stderr
    assert not installer_ran


def test_force_skips_the_print_check(run_deploy):
    proc, installer_ran = run_deploy(_status("printing"), "--force")
    assert proc.returncode == 0, proc.stderr
    assert installer_ran
