#!/usr/bin/env python3
# Build (and optionally flash) one or more [firmware_build <name>] targets
# declared in printer.cfg. Invoked by
# biokalico_extras/moonraker/firmware_build.py via Moonraker's shell_command
# component; also runnable by hand for testing.
#
# Builds run in parallel (isolated per-target OUT/KCONFIG_CONFIG dirs under
# firmware_builds/<name>/, shared ccache). Flashing is always strictly
# sequential, one target at a time - see biokalico_extras/firmware_flash.md.
#
# Emits newline-delimited JSON progress records to stdout:
#   {"target": "<name>", "phase": "queued|building|built|flashing|done|error",
#    "line": "..."}
#
# Note: this script does not check for an active print job before flashing -
# that's the caller's (Moonraker component's) responsibility, done before
# this process is even spawned, since it already has cheap access to live
# print state.
#
# Usage:
#   build_and_flash.py --action build --all
#   build_and_flash.py --action build_and_flash --targets main,nhk
#   build_and_flash.py --action build_and_flash --all --printer-cfg <path>

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
BUILD_ROOT = os.path.join(REPO_ROOT, "firmware_builds")
PRESET_DIR = os.path.join(REPO_ROOT, "biokalico_extras", "firmware_presets")
DEFAULT_PRINTER_CFG = os.path.expanduser("~/printer_data/config/printer.cfg")
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")

_emit_lock = threading.Lock()


def emit(target, phase, line=""):
    with _emit_lock:
        sys.stdout.write(
            json.dumps({"target": target, "phase": phase, "line": line}) + "\n"
        )
        sys.stdout.flush()


class Interrupted(Exception):
    pass


class ConfigError(Exception):
    # Raised for printer.cfg problems (missing preset, unresolvable mcu,
    # unknown/missing target). A normal Exception, not SystemExit - this
    # module is imported directly by biokalico_extras/moonraker/firmware_build.py,
    # and SystemExit is a BaseException that would escape Moonraker's
    # (and tornado's, and asyncio.run()'s) exception handling and crash the
    # whole server instead of producing a clean API error. main() below is
    # the only place this gets translated into a process exit.
    pass


# Popen objects for currently-running `make`/flash subprocesses, tracked so
# a SIGINT/SIGTERM can terminate them immediately rather than relying on
# Python-level exception propagation to reach a blocked worker thread (see
# _handle_signal and _run).
_active_procs = set()
_active_procs_lock = threading.Lock()


def _terminate_proc_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass


def _kill_proc_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _handle_signal(signum, frame):
    # Terminate every currently-running subprocess's whole process group
    # (make -j spawns compiler children) immediately, regardless of which
    # thread is currently running Python bytecode - a signal handler only
    # runs in the main thread, but a worker thread blocked reading a
    # subprocess's stdout won't see this signal at all otherwise, and would
    # keep the ThreadPoolExecutor (and any `make` it started) alive for its
    # full natural duration on cancel.
    with _active_procs_lock:
        procs = list(_active_procs)
    for p in procs:
        _terminate_proc_group(p)
    raise Interrupted("received signal %d" % signum)


######################################################################
# Minimal printer.cfg reader
######################################################################

# Only understands what firmware_build sections need: [section] headers,
# 'key: value' / 'key = value' lines, '#'/';' comments, and non-glob
# [include ...] directives (resolved relative to the including file).
# Glob includes are skipped - firmware_build/mcu sections are expected
# directly in printer.cfg (see biokalico_extras/firmware_flash.md), and a full
# Klipper-config-engine reimplementation isn't needed for that.

SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*(?:[#;].*)?$")
KV_RE = re.compile(r"^([A-Za-z0-9_.]+)\s*[:=]\s*(.*?)\s*$")
# Matches Klipper's own parser (configparser inline_comment_prefixes=(';','#')):
# a '#'/';' preceded by whitespace starts an inline comment.
INLINE_COMMENT_RE = re.compile(r"\s+[#;].*$")


def _strip_inline_comment(value):
    return INLINE_COMMENT_RE.sub("", value).rstrip()


def _parse_cfg_file(path, sections, seen_files):
    path = os.path.realpath(path)
    if path in seen_files or not os.path.isfile(path):
        return
    seen_files.add(path)
    cur = None
    multiline_key = (
        None  # key currently accumulating indented continuation lines
    )
    with open(path) as f:
        for raw in f:
            stripped = raw.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or stripped.startswith(";")
            ):
                continue
            if raw[:1].isspace():
                # Continuation line (Klipper's multi-line value syntax: any
                # 'key: value' line, whether or not it already has a value
                # on the same line, followed by indented lines that get
                # appended (newline-joined) to that key's value - used for
                # [firmware_build ...]'s 'overrides', see resolve_targets).
                if cur is not None and multiline_key is not None:
                    existing = cur.get(multiline_key, "")
                    line = _strip_inline_comment(stripped)
                    cur[multiline_key] = (
                        existing + "\n" + line if existing else line
                    )
                continue
            multiline_key = None  # any non-indented line ends a continuation
            m = SECTION_RE.match(stripped)
            if m:
                name = m.group(1).strip()
                if name.startswith("include "):
                    inc = name[len("include ") :].strip()
                    if "*" not in inc and "?" not in inc:
                        inc_path = (
                            inc
                            if os.path.isabs(inc)
                            else os.path.join(os.path.dirname(path), inc)
                        )
                        _parse_cfg_file(inc_path, sections, seen_files)
                    cur = None
                    continue
                cur = sections.setdefault(name, {})
                continue
            if cur is None:
                continue
            kv = KV_RE.match(stripped)
            if kv:
                key = kv.group(1)
                value = _strip_inline_comment(kv.group(2))
                cur[key] = value
                # Any 'key:'/'key=' line can be followed by indented
                # continuation lines, regardless of whether it already has a
                # value on the same line (matches configparser/Klipper's own
                # config-continuation semantics) - see the continuation
                # handling above, which appends to whatever's already there.
                multiline_key = key


def load_printer_cfg(path):
    sections = {}
    _parse_cfg_file(path, sections, set())
    return sections


def resolve_targets(sections):
    targets = {}
    for name, opts in sections.items():
        parts = name.split()
        if len(parts) != 2 or parts[0] != "firmware_build":
            continue
        tname = parts[1]
        preset = opts.get("preset")
        if not preset:
            raise ConfigError("firmware_build %s: missing 'preset'" % tname)
        mcu_name = opts.get("mcu", tname)
        # 'device' is mandatory and never auto-derived from a referenced
        # [mcu ...] section's serial:/canbus_uuid: - a bare device path
        # there (e.g. /dev/ttyACM0 instead of a stable
        # /dev/serial/by-id/... symlink) isn't guaranteed to still point at
        # the same physical board by the time a flash actually runs, and
        # flashing whatever now happens to be at a drifted path is a much
        # worse failure mode than Klipper's own "failed to connect" when
        # the same thing happens during normal operation. Require it
        # explicitly so the user has to consciously pick a stable
        # identifier rather than inherit one implicitly.
        device = opts.get("device")
        if not device:
            raise ConfigError(
                "firmware_build %s: missing 'device' (must be set "
                "explicitly - not derived from an [mcu ...] section's "
                "serial:/canbus_uuid:, see biokalico_extras/firmware_flash.md)"
                % tname
            )
        # Optional: extra Kconfig lines applied on top of the preset, for a
        # one-off tweak without forking the whole (possibly shared) preset
        # file. Kconfig itself resolves duplicate symbols by taking the
        # last-seen value (confirmed directly against kconfiglib - it warns
        # but doesn't error), so appending these after the preset's own
        # content in build_target() is sufficient - no merge logic needed.
        overrides = opts.get("overrides", "")
        targets[tname] = {
            "preset": preset,
            "mcu": mcu_name,
            "device": device,
            "overrides": overrides,
        }
    return targets


######################################################################
# Build and flash
######################################################################


# Compiling two targets in parallel can spawn up to 2x nproc compiler
# processes (each `make -j$(nproc)`) - enough to starve everything else on
# the host of CPU, including Moonraker and Klipper. `nice`/`ionice` let a
# build still use every otherwise-idle core (so it's not slower in the
# common case) while yielding immediately to anything at normal priority -
# Moonraker, Klipper, the desktop - under contention, rather than capping
# parallelism outright.
_NICE_PREFIX = ["nice", "-n", "15", "ionice", "-c3"]


def _run(cmd, cwd, env, target, phase, nice=True):
    proc = subprocess.Popen(
        (_NICE_PREFIX + cmd) if nice else cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,  # own process group, so cancel can kill make -j's children too
    )
    with _active_procs_lock:
        _active_procs.add(proc)
    try:
        try:
            for out_line in proc.stdout:
                emit(target, phase, out_line.rstrip("\n"))
            ret = proc.wait()
        except BaseException:
            _terminate_proc_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_proc_group(proc)
            raise
    finally:
        with _active_procs_lock:
            _active_procs.discard(proc)
    if ret != 0:
        raise RuntimeError(
            "command failed (exit %d): %s" % (ret, " ".join(cmd))
        )


def build_target(name, preset, overrides=""):
    preset_path = os.path.join(PRESET_DIR, preset + ".config")
    if not os.path.isfile(preset_path):
        raise RuntimeError(
            "unknown preset '%s' (expected %s)" % (preset, preset_path)
        )

    build_dir = os.path.join(BUILD_ROOT, name)
    os.makedirs(build_dir, exist_ok=True)
    config_path = os.path.join(build_dir, ".config")
    out_dir = os.path.join(build_dir, "out") + os.sep

    with open(preset_path) as src, open(config_path, "w") as dst:
        dst.write(src.read())
        if overrides.strip():
            dst.write("\n# --- overrides from [firmware_build %s] ---\n" % name)
            dst.write(overrides.strip() + "\n")

    ccache_dir = os.path.join(BUILD_ROOT, ".ccache")
    os.makedirs(ccache_dir, exist_ok=True)
    env = dict(os.environ)
    env["CCACHE_DIR"] = ccache_dir

    make_common = [
        "make",
        "KCONFIG_CONFIG=%s" % config_path,
        "OUT=%s" % out_dir,
        "CC=ccache arm-none-eabi-gcc",
    ]

    emit(name, "building", "expanding preset (olddefconfig)")
    _run(make_common + ["olddefconfig"], REPO_ROOT, env, name, "building")

    emit(name, "building", "compiling")
    nproc = str(os.cpu_count() or 1)
    _run(make_common + ["-j", nproc], REPO_ROOT, env, name, "building")


def source_fingerprint(preset, overrides=""):
    # Scoped deliberately, not a whole-repo hash: most of this repo (docs,
    # klippy's own host-side Python, other targets' presets, this very
    # driver script) has no effect on what actually gets compiled into a
    # given target's klipper.bin, and a repo-wide fingerprint would flag a
    # false "rebuild needed" for every unrelated edit. Only `src/` (the
    # shared C source tree every architecture compiles from), this target's
    # own Kconfig preset, and any printer.cfg 'overrides:' lines for it
    # (see resolve_targets/build_target) actually affect the output bytes.
    #
    # This is also why it's a plain filesystem content hash rather than a
    # git-based one (like `git stash create`, used for mcu_version - see
    # docs/Bootloader_Entry.md's comment thread for why that's a boolean,
    # not a content hash, and insufficient here too): a git-based hash only
    # sees *tracked* files, so a new untracked .c file under src/ wouldn't
    # register as a change. Walking the actual files on disk catches that.
    h = hashlib.sha256()
    for rel in ("src",):
        base = os.path.join(REPO_ROOT, rel)
        for root, dirs, files in os.walk(base):
            dirs.sort()
            for fname in sorted(files):
                full = os.path.join(root, fname)
                h.update(os.path.relpath(full, REPO_ROOT).encode())
                try:
                    with open(full, "rb") as f:
                        h.update(f.read())
                except OSError:
                    pass
    preset_path = os.path.join(PRESET_DIR, preset + ".config")
    try:
        with open(preset_path, "rb") as f:
            h.update(f.read())
    except OSError:
        pass
    h.update(overrides.strip().encode())
    return h.hexdigest()


def write_last_flashed(name, preset, overrides, config_path):
    build_dir = os.path.dirname(config_path)
    # git_commit is informational only (shown in the UI) - source_fingerprint
    # (src/ + this target's preset + overrides, see source_fingerprint()
    # above) is what actually drives the "needs rebuild & reflash"
    # comparison now, since a commit hash doesn't change for uncommitted
    # edits and a repo-wide content hash would false-positive on edits
    # unrelated to this target.
    commit_proc = subprocess.run(
        ["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    # Explicitly "unknown" (not a silently empty string) on failure, so a
    # broken git invocation is visibly distinguishable in the UI from a
    # successful run.
    commit = (
        commit_proc.stdout.strip() if commit_proc.returncode == 0 else "unknown"
    )
    data = {
        "git_commit": commit,
        "source_fingerprint": source_fingerprint(preset, overrides),
        "flashed_at": time.time(),
    }
    with open(os.path.join(build_dir, "last_flashed.json"), "w") as f:
        json.dump(data, f, indent=2)


def _systemctl(action, target):
    result = subprocess.run(
        ["sudo", "systemctl", action, "klipper"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        emit(
            target,
            "flashing",
            "warning: systemctl %s klipper failed (exit %d): %s"
            % (
                action,
                result.returncode,
                (result.stderr or result.stdout).strip(),
            ),
        )
        return False
    return True


def _wait_for_klippy_ready(target, timeout=60):
    url = MOONRAKER_URL + "/server/info"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data.get("result", {}).get("klippy_state") == "ready":
                    emit(target, "flashing", "klipper reconnected")
                    return
        except Exception:
            pass
        time.sleep(1)
    emit(
        target,
        "flashing",
        "warning: klipper did not report ready within %ds" % timeout,
    )


def flash_target(name, preset, overrides, device):
    build_dir = os.path.join(BUILD_ROOT, name)
    config_path = os.path.join(build_dir, ".config")
    out_dir = os.path.join(build_dir, "out") + os.sep

    emit(name, "flashing", "stopping klipper service")
    if not _systemctl("stop", name):
        raise RuntimeError(
            "failed to stop the klipper service - refusing to flash while "
            "it may still hold the serial port"
        )
    try:
        env = dict(os.environ)
        cmd = [
            "make",
            "KCONFIG_CONFIG=%s" % config_path,
            "OUT=%s" % out_dir,
            "flash",
            "FLASH_DEVICE=%s" % device,
        ]
        emit(name, "flashing", "make flash FLASH_DEVICE=%s" % device)
        # No _NICE_PREFIX here, unlike build_target's _run() calls: deprior-
        # itizing I/O (ionice -c3) during a timing-sensitive bootloader
        # write is actively risky, unlike compiling where it's genuinely
        # wanted (see _NICE_PREFIX's comment).
        _run(cmd, REPO_ROOT, env, name, "flashing", nice=False)
        write_last_flashed(name, preset, overrides, config_path)
    finally:
        emit(name, "flashing", "starting klipper service")
        if not _systemctl("start", name):
            emit(
                name,
                "error",
                "klipper service failed to restart - check it manually "
                "(sudo systemctl status klipper)",
            )
        _wait_for_klippy_ready(name)


######################################################################
# Orchestration
######################################################################


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--printer-cfg", default=DEFAULT_PRINTER_CFG)
    ap.add_argument("--targets", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument(
        "--action", choices=["build", "build_and_flash"], required=True
    )
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        sections = load_printer_cfg(args.printer_cfg)
        all_targets = resolve_targets(sections)
    except ConfigError as e:
        raise SystemExit(str(e))
    if not all_targets:
        raise SystemExit(
            "no [firmware_build ...] sections found in %s" % args.printer_cfg
        )

    if args.all:
        names = list(all_targets)
    else:
        names = [t.strip() for t in args.targets.split(",") if t.strip()]
    if not names:
        raise SystemExit("no targets specified (use --all or --targets a,b)")
    unknown = [n for n in names if n not in all_targets]
    if unknown:
        raise SystemExit(
            "unknown firmware_build target(s): %s" % ", ".join(unknown)
        )

    os.makedirs(BUILD_ROOT, exist_ok=True)

    failed = []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {
            pool.submit(
                build_target,
                n,
                all_targets[n]["preset"],
                all_targets[n]["overrides"],
            ): n
            for n in names
        }
        for fut in as_completed(futures):
            n = futures[fut]
            try:
                fut.result()
                emit(n, "built")
            except Interrupted:
                raise
            except Exception as e:
                emit(n, "error", str(e))
                failed.append(n)

    if failed:
        raise SystemExit("build failed for: %s" % ", ".join(failed))

    if args.action == "build_and_flash":
        for n in names:
            try:
                flash_target(
                    n,
                    all_targets[n]["preset"],
                    all_targets[n]["overrides"],
                    all_targets[n]["device"],
                )
                emit(n, "done")
            except Interrupted:
                # Cancel: flash_target's own finally already restarted
                # klipper for this target - stop here rather than
                # continuing to stop/flash targets the user didn't ask to
                # touch during a cancel.
                raise
            except Exception as e:
                emit(n, "error", str(e))
                failed.append(n)
        if failed:
            raise SystemExit("flash failed for: %s" % ", ".join(failed))


if __name__ == "__main__":
    try:
        main()
    except Interrupted as e:
        emit("*", "error", "cancelled: %s" % e)
        sys.exit(130)
