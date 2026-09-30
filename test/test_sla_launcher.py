import os
import pathlib
import subprocess

import pytest

LAUNCHER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "scripts"
    / "sla"
    / "image-display-launcher.sh"
)


def _fake_python(path):
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/bash\necho "ran $0 $*"\n')
    path.chmod(0o755)


@pytest.fixture
def run_launcher(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    scripts = repo / "scripts" / "sla"
    bindir = tmp_path / "bin"
    for d in (home, scripts, bindir):
        d.mkdir(parents=True, exist_ok=True)
    xrandr = bindir / "xrandr"
    xrandr.write_text("#!/bin/bash\nexit 0\n")
    xrandr.chmod(0o755)

    def run(**extra_env):
        env = {k: v for k, v in os.environ.items() if k != "PYTHON_BIN"}
        env.update(
            HOME=str(home),
            PATH="%s:%s" % (bindir, os.environ["PATH"]),
            SCRIPTS_DIR=str(scripts),
            REPO_DIR=str(repo),
            CONFIG_PATH="/nonexistent/config.yaml",
            # Skip display detection so only the interpreter choice is tested.
            DISPLAY_VALUE=":0",
            XAUTHORITY_PATH="/tmp/xauth-for-test",
            WAIT_SECONDS="0",
        )
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(LAUNCHER)], capture_output=True, text=True, env=env
        )

    run.home, run.repo, run.scripts = home, repo, scripts
    return run


def test_prefers_the_repo_venv(run_launcher):
    _fake_python(run_launcher.repo / ".venv" / "bin" / "python")
    _fake_python(run_launcher.scripts / ".venv" / "bin" / "python")
    proc = run_launcher()
    assert proc.returncode == 0, proc.stderr
    assert "repo/.venv/bin/python" in proc.stdout
    assert "image-display.py /nonexistent/config.yaml" in proc.stdout


def test_falls_back_to_a_venv_beside_the_script(run_launcher):
    _fake_python(run_launcher.scripts / ".venv" / "bin" / "python")
    proc = run_launcher()
    assert proc.returncode == 0, proc.stderr
    assert "scripts/sla/.venv/bin/python" in proc.stdout


def test_falls_back_to_the_klipper_venv(run_launcher):
    _fake_python(run_launcher.home / "klippy-env" / "bin" / "python")
    proc = run_launcher()
    assert proc.returncode == 0, proc.stderr
    assert "klippy-env/bin/python" in proc.stdout


def test_an_explicit_interpreter_wins(run_launcher, tmp_path):
    _fake_python(run_launcher.repo / ".venv" / "bin" / "python")
    _fake_python(tmp_path / "mine" / "python")
    proc = run_launcher(PYTHON_BIN=str(tmp_path / "mine" / "python"))
    assert proc.returncode == 0, proc.stderr
    assert "mine/python" in proc.stdout


def test_reports_when_no_interpreter_exists(run_launcher):
    proc = run_launcher()
    assert proc.returncode == 1
    assert "No Python interpreter" in proc.stderr
