#!/bin/bash
# End-to-end test of scripts/biokalico-installer.sh. Runs `install` on the
# current working tree, including uncommitted changes, in a Debian container
# (see ./Dockerfile) and checks that the services came up.
#
# Requires Docker. See README.md for setup and for running the steps by hand.
#
# Usage: scripts/docker-test/run.sh [--sim]
#   (run from anywhere; paths below are resolved relative to this script)
#
#   --sim   after the install checks, turn the container into a simulated
#           printer and run the end-to-end tests against it (see
#           README.md, "Simulated printer").

set -uo pipefail

SIM=0
for arg in "$@"; do
    case "$arg" in
        --sim) SIM=1 ;;
        *) echo "usage: $0 [--sim]" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-biokalico-install-test}"
CONTAINER_NAME="${CONTAINER_NAME:-biokalico-install-test}"
HTTP_PORT="${HTTP_PORT:-18080}"
MOONRAKER_PORT="${MOONRAKER_PORT:-17125}"
SNAPSHOT_DIR="$(mktemp -d -t biokalico-snapshot-XXXXXX)"
# Submodules tested from the local working tree, including uncommitted changes.
LOCAL_FORK_SUBMODULES=(mainsail moonraker crowsnest ad246s-mscope)
declare -A SUBMODULE_SNAPSHOT_DIRS

DOCKER="docker"
# Use sudo if this user can't reach the Docker daemon.
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

log()  { echo "==> $*"; }
fail() { echo "==> FAIL: $*" >&2; exit 1; }

# Tracked files (even if also gitignored) plus untracked, non-ignored files,
# NUL-separated for rsync --files-from.
worktree_files() {
    local root="$1" path
    while IFS= read -r -d '' path; do
        # Skip files deleted from the worktree but still in the index.
        if [[ -e "$root/$path" || -L "$root/$path" ]]; then
            printf '%s\0' "$path"
        fi
    done < <(
        git -C "$root" ls-files -z
        git -C "$root" ls-files -z --others --exclude-standard
    )
}

cleanup_snapshot() {
    # The snapshots are mounted into the container, which is left running
    # for inspection, so keep them while it runs.
    local running
    running="$($DOCKER inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)"
    if [[ "$running" == "true" ]]; then
        log "Container '$CONTAINER_NAME' is still running -- leaving $SNAPSHOT_DIR (and submodule snapshots) in place (bind-mounted into it)"
        return
    fi
    rm -rf "$SNAPSHOT_DIR"
    for name in "${LOCAL_FORK_SUBMODULES[@]}"; do
        [[ -n "${SUBMODULE_SNAPSHOT_DIRS[$name]:-}" ]] && rm -rf "${SUBMODULE_SNAPSHOT_DIRS[$name]}"
    done
}
trap cleanup_snapshot EXIT

log "Snapshotting current working tree (including uncommitted changes) into $SNAPSHOT_DIR"
# deps/ is left out here and added back as submodules below.
worktree_files "$REPO_ROOT" | grep -z -v '^deps/' \
    | rsync -a --files-from=- --from0 "$REPO_ROOT/" "$SNAPSHOT_DIR/" \
    || fail "rsync snapshot failed"

# Snapshot mainsail and moonraker the same way. Each snapshot is mounted into
# the container, and .gitmodules is pointed at those mounts below.
for name in "${LOCAL_FORK_SUBMODULES[@]}"; do
    sm_dir="$(mktemp -d -t "biokalico-snapshot-$name-XXXXXX")"
    SUBMODULE_SNAPSHOT_DIRS[$name]="$sm_dir"
    log "Snapshotting deps/$name's working tree (including uncommitted changes) into $sm_dir"
    worktree_files "$REPO_ROOT/deps/$name" \
        | rsync -a --files-from=- --from0 "$REPO_ROOT/deps/$name/" "$sm_dir/" \
        || fail "rsync snapshot of deps/$name failed"
    (
        cd "$sm_dir"
        git init -q
        git config user.email "test@localhost"
        git config user.name "docker-test"
        # -f: keep files that are tracked but also gitignored.
        git add -A -f
        git commit -q -m "snapshot for docker-test/run.sh"
    ) || fail "snapshot git commit for deps/$name failed"
done

(
    cd "$SNAPSHOT_DIR"
    git init -q
    git config user.email "test@localhost"
    git config user.name "docker-test"
    for name in "${LOCAL_FORK_SUBMODULES[@]}"; do
        # Mount point inside the container (see `docker run` below).
        git config -f .gitmodules "submodule.deps/$name.url" \
            "file:///mnt/biokalico-submodule-$name"
    done
    git add -A
    for name in "${LOCAL_FORK_SUBMODULES[@]}"; do
        pinned_sha="$(git -C "${SUBMODULE_SNAPSHOT_DIRS[$name]}" rev-parse HEAD)" \
            || fail "couldn't resolve deps/$name snapshot's commit"
        git update-index --add --cacheinfo 160000,"$pinned_sha",deps/$name
    done
    git commit -q -m "snapshot for docker-test/run.sh"
) || fail "snapshot git commit failed"

log "Building test image ($IMAGE_NAME) from Dockerfile"
$DOCKER build -q -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR" \
    || fail "docker build failed"

log "Removing any previous test container named $CONTAINER_NAME"
$DOCKER rm -f "$CONTAINER_NAME" >/dev/null 2>&1

SUBMODULE_MOUNT_ARGS=()
for name in "${LOCAL_FORK_SUBMODULES[@]}"; do
    SUBMODULE_MOUNT_ARGS+=(-v "${SUBMODULE_SNAPSHOT_DIRS[$name]}:/mnt/biokalico-submodule-$name:ro")
done

log "Starting container (systemd as PID 1)"
$DOCKER run -d --name "$CONTAINER_NAME" \
    --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock \
    -v "$SNAPSHOT_DIR:/mnt/biokalico-snapshot:ro" \
    "${SUBMODULE_MOUNT_ARGS[@]}" \
    -p "127.0.0.1:${HTTP_PORT}:80" \
    -p "127.0.0.1:${MOONRAKER_PORT}:7125" \
    "$IMAGE_NAME" \
    || fail "docker run failed"

log "Waiting for systemd to finish booting"
for _ in $(seq 1 30); do
    state="$($DOCKER exec "$CONTAINER_NAME" systemctl is-system-running 2>/dev/null || true)"
    [[ "$state" == "running" || "$state" == "degraded" ]] && break
    sleep 1
done
log "systemd state: ${state:-unknown}"

log "Cloning the snapshot as ~/klipper inside the container (mirrors QUICKSTART.md section 6, step 1)"
$DOCKER exec -u biokalico "$CONTAINER_NAME" \
    git clone -q /mnt/biokalico-snapshot /home/biokalico/klipper \
    || fail "git clone inside container failed"

# git refuses file:// submodule URLs by default.
$DOCKER exec -u biokalico "$CONTAINER_NAME" \
    git config --global protocol.file.allow always \
    || fail "git config protocol.file.allow failed"

log "Running biokalico-installer.sh install (mirrors QUICKSTART.md section 6, step 2)"
log "(BIOKALICO_ORIGIN is overridden to the mounted snapshot, so this exercises"
log " today's working tree - see run.sh comments - not whatever's on GitHub;"
log " every deps/ submodule is the same - their uncommitted working trees are"
log " snapshotted and mounted too, so this doesn't require pushing to GitHub"
log " first)"
$DOCKER exec -u biokalico -e BIOKALICO_ORIGIN=/mnt/biokalico-snapshot "$CONTAINER_NAME" \
    bash /home/biokalico/klipper/scripts/biokalico-installer.sh install
install_status=$?

checks_failed=0
if [[ $install_status -ne 0 ]]; then
    echo "==> biokalico-installer.sh install exited non-zero ($install_status) - continuing to verification anyway to see what did/didn't come up"
    checks_failed=$((checks_failed + 1))
fi

echo
echo "==================== Verification ===================="

check_active() {
    local svc="$1"
    if $DOCKER exec "$CONTAINER_NAME" systemctl is-active --quiet "$svc"; then
        echo "PASS: $svc.service is active"
    else
        echo "FAIL: $svc.service is not active"
        $DOCKER exec "$CONTAINER_NAME" systemctl status "$svc" --no-pager -l 2>&1 | sed 's/^/       /'
        checks_failed=$((checks_failed + 1))
    fi
}

check_active moonraker
check_active nginx

if $DOCKER exec "$CONTAINER_NAME" test -f /home/biokalico/klipper/deps/moonraker/moonraker/server.py \
    && $DOCKER exec "$CONTAINER_NAME" test -f /home/biokalico/klipper/deps/mainsail/package.json \
    && $DOCKER exec "$CONTAINER_NAME" test -f /home/biokalico/klipper/deps/crowsnest/tools/install.sh \
    && $DOCKER exec "$CONTAINER_NAME" test -f /home/biokalico/klipper/deps/ad246s-mscope/scan/mon.py; then
    echo "PASS: deps/{moonraker,mainsail,crowsnest,ad246s-mscope} submodules were checked out"
else
    echo "FAIL: one or more deps/ submodules is missing - git submodule update did not run or failed"
    checks_failed=$((checks_failed + 1))
fi

if $DOCKER exec "$CONTAINER_NAME" test -f /home/biokalico/klipper/deps/mainsail/dist/index.html; then
    echo "PASS: Mainsail was built from source (deps/mainsail/dist/index.html exists)"
else
    echo "FAIL: deps/mainsail/dist/index.html missing - on-host npm build did not produce output"
    checks_failed=$((checks_failed + 1))
fi

# klipper.service is expected to fail because the installer doesn't create
# printer.cfg. Check klippy.log to make sure that is the reason.
klippy_log="$($DOCKER exec "$CONTAINER_NAME" cat /home/biokalico/printer_data/logs/klippy.log 2>&1)"
if echo "$klippy_log" | grep -qi "Unable to open config file.*printer.cfg"; then
    echo "PASS: klipper.service is failing for the expected reason (missing printer.cfg)"
else
    echo "FAIL: klipper.service is failing, but not (visibly) because of a missing printer.cfg - check the log below"
    echo "$klippy_log" | tail -20 | sed 's/^/       /'
    checks_failed=$((checks_failed + 1))
fi

# crowsnest.service is expected to crash-loop, since there is no camera.
crowsnest_status="$($DOCKER exec "$CONTAINER_NAME" systemctl status crowsnest --no-pager -l 2>&1)"
if $DOCKER exec "$CONTAINER_NAME" systemctl is-active --quiet crowsnest; then
    echo "PASS: crowsnest.service is active"
elif $DOCKER exec "$CONTAINER_NAME" systemctl cat crowsnest >/dev/null 2>&1 \
    && echo "$crowsnest_status" | grep -qi "auto-restart\|activating\|start-limit"; then
    echo "PASS: crowsnest.service is installed and enabled, crash-looping only because there's no real webcam here (expected)"
else
    echo "FAIL: crowsnest.service is in an unexpected state"
    echo "$crowsnest_status" | sed 's/^/       /'
    checks_failed=$((checks_failed + 1))
fi

moonraker_http="$($DOCKER exec "$CONTAINER_NAME" curl -sS -o /dev/null \
    -w '%{http_code}' http://127.0.0.1:7125/server/info 2>/dev/null || true)"
if [[ "$moonraker_http" == "200" || "$moonraker_http" == "401" || "$moonraker_http" == "403" ]]; then
    echo "PASS: Moonraker's API responds (HTTP $moonraker_http; authentication may be required)"
else
    echo "FAIL: Moonraker's API did not respond successfully (HTTP ${moonraker_http:-none})"
    checks_failed=$((checks_failed + 1))
fi

mainsail_body="$($DOCKER exec "$CONTAINER_NAME" curl -sf http://127.0.0.1/ 2>/dev/null)"
if echo "$mainsail_body" | grep -qi "<title>Mainsail"; then
    echo "PASS: Mainsail is served over nginx (http://127.0.0.1/ contains Mainsail's <title>)"
else
    echo "FAIL: nginx did not serve Mainsail's index.html at http://127.0.0.1/"
    checks_failed=$((checks_failed + 1))
fi

moonraker_conf="$($DOCKER exec "$CONTAINER_NAME" cat /home/biokalico/printer_data/config/moonraker.conf 2>/dev/null)"
if echo "$moonraker_conf" | grep -q "biokalico_extras"; then
    echo "PASS: moonraker.conf was seeded from bio_config/moonraker.conf.example (BioKalico includes present)"
else
    echo "FAIL: moonraker.conf missing or doesn't reference biokalico_extras"
    checks_failed=$((checks_failed + 1))
fi

# ambient-recorder must be installed but not enabled by default.
if $DOCKER exec "$CONTAINER_NAME" test -f /etc/systemd/system/ambient-recorder.service \
    && $DOCKER exec "$CONTAINER_NAME" test -f /home/biokalico/printer_data/config/ambient-recorder-config.yaml; then
    if $DOCKER exec "$CONTAINER_NAME" systemctl is-enabled --quiet ambient-recorder; then
        echo "FAIL: ambient-recorder.service is enabled, but should be off by default"
        checks_failed=$((checks_failed + 1))
    else
        echo "PASS: ambient-recorder.service is installed but disabled by default (config seeded)"
    fi
else
    echo "FAIL: ambient-recorder.service unit or its seeded config is missing"
    checks_failed=$((checks_failed + 1))
fi

echo "========================================================"
echo

if [[ $SIM -eq 1 ]]; then
    log "Turning the container into a simulated printer (scripts/docker-test/sim/setup.sh)"
    if ! $DOCKER exec -u biokalico "$CONTAINER_NAME" \
        bash /home/biokalico/klipper/scripts/docker-test/sim/setup.sh; then
        echo "FAIL: the simulated printer did not come up"
        checks_failed=$((checks_failed + 1))
    else
        ui_args=()
        if command -v chromium >/dev/null 2>&1 && python3 -c 'import websocket' 2>/dev/null; then
            shots_dir="${SIM_SHOTS_DIR:-$(mktemp -d -t biokalico-sim-shots-XXXXXX)}"
            ui_args=(--ui --shots "$shots_dir")
            log "UI screenshots will be written to $shots_dir"
        else
            log "chromium or python3-websocket is missing on this host: running the API checks only"
        fi
        if ! CONTAINER_NAME="$CONTAINER_NAME" HTTP_PORT="$HTTP_PORT" MOONRAKER_PORT="$MOONRAKER_PORT" \
            python3 "$SCRIPT_DIR/sim/e2e.py" "${ui_args[@]}"; then
            checks_failed=$((checks_failed + 1))
        fi
    fi
    echo
fi

if [[ $checks_failed -eq 0 ]]; then
    echo "==> ALL CHECKS PASSED"
else
    echo "==> $checks_failed CHECK(S) FAILED"
fi

echo "==> You can also open http://127.0.0.1:${HTTP_PORT}/ in a browser on this host"
echo "    (mapped to the container's nginx/Mainsail) while it's still running."
echo "==> Container '$CONTAINER_NAME' is still running for inspection."
echo "==> To get a shell inside it:   $DOCKER exec -it -u biokalico $CONTAINER_NAME bash"
echo "==> To tear it down when done:  $DOCKER rm -f $CONTAINER_NAME"

[[ $checks_failed -eq 0 ]]
