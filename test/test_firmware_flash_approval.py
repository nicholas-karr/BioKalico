import pytest

from scripts.firmware import build_and_flash


def test_flash_approval_waits_for_explicit_ok(tmp_path, monkeypatch):
    approval = tmp_path / "approval"

    def approve(_target, phase, _line=""):
        if phase == "flash_check":
            approval.write_text("ok")

    monkeypatch.setattr(build_and_flash, "emit", approve)

    build_and_flash.wait_for_flash_approval("main", str(approval), timeout=0.2)
    assert not approval.exists()


def test_flash_approval_rejects_abort_decision(tmp_path, monkeypatch):
    approval = tmp_path / "approval"

    def reject(_target, phase, _line=""):
        if phase == "flash_check":
            approval.write_text("abort: print is active")

    monkeypatch.setattr(build_and_flash, "emit", reject)

    with pytest.raises(RuntimeError, match="print is active"):
        build_and_flash.wait_for_flash_approval(
            "main", str(approval), timeout=0.2
        )


def test_flash_approval_ignores_a_file_that_is_not_written_yet(
    tmp_path, monkeypatch
):
    # The approver creates the file and then writes the decision into it, so
    # an empty file is a decision in progress, not a denial.
    approval = tmp_path / "approval"
    real_sleep = build_and_flash.time.sleep

    def half_written(_target, phase, _line=""):
        if phase == "flash_check":
            approval.write_text("")

    def finish_writing(seconds):
        if approval.exists() and not approval.read_text():
            approval.write_text("ok")
        real_sleep(0)

    monkeypatch.setattr(build_and_flash, "emit", half_written)
    monkeypatch.setattr(build_and_flash.time, "sleep", finish_writing)

    build_and_flash.wait_for_flash_approval("main", str(approval), timeout=2)


def test_flash_approval_times_out_without_a_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(build_and_flash, "emit", lambda *args: None)

    with pytest.raises(RuntimeError, match="timed out"):
        build_and_flash.wait_for_flash_approval(
            "main", str(tmp_path / "approval"), timeout=0.1
        )


def test_source_fingerprint_changes_for_make_and_library_inputs(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    preset_dir = repo / "presets"
    (repo / "src").mkdir(parents=True)
    (repo / "lib" / "chip").mkdir(parents=True)
    (repo / "scripts").mkdir()
    preset_dir.mkdir()
    (repo / "src" / "main.c").write_text("int main(void) { return 0; }\n")
    (repo / "lib" / "chip" / "chip.h").write_text("#define CHIP 1\n")
    (repo / "Makefile").write_text("all:\n\t@true\n")
    (repo / "Kconfig").write_text('mainmenu "test"\n')
    (preset_dir / "board.config").write_text("CONFIG_TEST=y\n")
    monkeypatch.setattr(build_and_flash, "REPO_ROOT", str(repo))
    monkeypatch.setattr(build_and_flash, "PRESET_DIR", str(preset_dir))
    monkeypatch.setattr(
        build_and_flash.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 1, "stdout": b""}
        )(),
    )

    original = build_and_flash.source_fingerprint("board")
    (repo / "Makefile").write_text("all:\n\t@false\n")
    make_changed = build_and_flash.source_fingerprint("board")
    (repo / "lib" / "chip" / "chip.h").write_text("#define CHIP 2\n")
    library_changed = build_and_flash.source_fingerprint("board")

    assert original != make_changed
    assert make_changed != library_changed
