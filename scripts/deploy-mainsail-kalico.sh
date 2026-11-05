#!/bin/bash
# Deploy the local Kalico repository to a remote MainsailOS host.

set -euo pipefail

TARGET="${DEPLOY_TARGET:-}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DELETE_REMOTE=1
BACKUP_REMOTE=0
FORCE_RESTART=0
DRY_RUN=0
FORCE_PRINT_CHECK=0
ASSUME_YES=0

usage() {
    cat <<EOF
Usage: deploy-mainsail-kalico.sh [options]

Rsyncs this repo to the remote host, then runs
'biokalico-installer.sh update klipper moonraker mainsail' there (Klipper venv
and service, Moonraker, and a rebuild of Mainsail, then a restart of each;
see scripts/biokalico-installer.sh). The remote host must already have a
recursive Git checkout at ~/klipper.

Options:
  --target <user@host>   SSH target (required unless DEPLOY_TARGET is set)
  --source <path>        Local Kalico repo path (default: repo root)
  --backup                Backup remote ~/klipper before syncing
  --no-delete             Do not delete remote files missing locally
  --force-restart         Run biokalico-installer.sh even when rsync finds no changes
  --force                 Skip the check for an active print on the target
  -y, --yes               Skip the "are you sure" confirmation prompt
  --dry-run               Show actions without changing remote files
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET="$2"
            shift 2
            ;;
        --source)
            SRC_DIR="$2"
            shift 2
            ;;
        --backup)
            BACKUP_REMOTE=1
            shift
            ;;
        --no-delete)
            DELETE_REMOTE=0
            shift
            ;;
        --force-restart)
            FORCE_RESTART=1
            shift
            ;;
        --force)
            FORCE_PRINT_CHECK=1
            shift
            ;;
        -y|--yes)
            ASSUME_YES=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ ! -d "$SRC_DIR" ]]; then
    echo "Source directory not found: $SRC_DIR" >&2
    exit 1
fi

if [[ -z "$TARGET" ]]; then
    echo "An SSH target is required; use --target or DEPLOY_TARGET." >&2
    exit 2
fi

if [[ ! -f "$SRC_DIR/scripts/klippy-requirements.txt" ]]; then
    echo "Source path does not look like a Kalico repo: $SRC_DIR" >&2
    exit 1
fi

command -v ssh >/dev/null 2>&1 || {
    echo "ssh command not found" >&2
    exit 1
}

command -v rsync >/dev/null 2>&1 || {
    echo "rsync command not found" >&2
    exit 1
}

echo "==> Target: $TARGET"
echo "==> Source: $SRC_DIR"

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "==> Dry run mode enabled"
fi

# Exits the script if a print is printing or paused, or if the state can't
# be read. Run at the start and again just before the restart. --force skips
# it.
check_print_state() {
    echo "==> Checking $TARGET for an active print"
    if ! print_response="$(ssh "$TARGET" \
        "u='http://127.0.0.1:7125/printer/objects/query?print_stats'; python3 ~/klipper/scripts/moonraker_api.py \"\$u\" 2>/dev/null || curl -fsS \"\$u\"")"; then
        echo "ERROR: could not verify print state on $TARGET; refusing a service restart." >&2
        echo "       Resolve Moonraker/SSH connectivity or use --force to override." >&2
        exit 1
    fi
    if ! print_state="$(python3 -c \
        'import json,sys; print(json.load(sys.stdin)["result"]["status"]["print_stats"]["state"])' \
        <<<"$print_response")" || [[ -z "$print_state" ]]; then
        echo "ERROR: $TARGET returned an invalid print-state response; refusing a service restart." >&2
        echo "       Use --force only after independently confirming the printer is idle." >&2
        exit 1
    fi
    if [[ "$print_state" == "printing" || "$print_state" == "paused" ]]; then
        echo "ERROR: $TARGET reports an active print (print_stats.state=$print_state)." >&2
        echo "       This deploy restarts the klipper service remotely -- refusing. Use --force to override." >&2
        exit 1
    fi
}

if [[ "$DRY_RUN" -eq 0 ]]; then
    if [[ "$FORCE_PRINT_CHECK" -eq 1 ]]; then
        echo "==> Skipping active-print check (--force)"
    else
        check_print_state
    fi

    if [[ "$ASSUME_YES" -ne 1 ]]; then
        # `|| true` so a closed stdin aborts with a message below instead of
        # exiting silently under set -e.
        read -r -p "Deploy to $TARGET? This may restart klipper and delete remote files not present locally. [y/N] " confirm || true
        case "$confirm" in
            y|Y|yes|YES) ;;
            *)
                echo "Aborted."
                exit 1
                ;;
        esac
    fi

    if [[ "$BACKUP_REMOTE" -eq 1 ]]; then
        echo "==> Backing up remote ~/klipper"
        ssh "$TARGET" 'set -e; test -d ~/klipper; cp -a ~/klipper ~/klipper_backup_$(date +%F_%H%M%S)'
    else
        echo "==> Using in-place sync (no backup); use --backup for snapshot"
    fi
    # The deploy keeps the remote's .git metadata, so the remote must already
    # be a checkout with its submodules.
    if ! ssh "$TARGET" \
        'git -C ~/klipper rev-parse --is-inside-work-tree >/dev/null &&
         git -C ~/klipper/deps/mainsail rev-parse --is-inside-work-tree >/dev/null &&
         git -C ~/klipper/deps/moonraker rev-parse --is-inside-work-tree >/dev/null'; then
        echo "ERROR: snapshot deployment requires an existing recursive Git checkout at ~/klipper." >&2
        echo "       Clone it with --recurse-submodules before retrying." >&2
        exit 1
    fi
else
    echo "==> Skipping remote preparation because --dry-run was set"
fi

rsync_args=(
    -a
    --exclude ".git"
    --exclude ".venv"
    --exclude "__pycache__"
    --exclude "klippy/chelper/c_helper.so"
)

if [[ "$DELETE_REMOTE" -eq 1 ]]; then
    rsync_args+=(--delete)
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    rsync_args+=(-n -v)
fi

echo "==> Syncing repository to remote ~/klipper"
rsync_args+=(--itemize-changes)

RSYNC_OUTPUT="$(rsync "${rsync_args[@]}" "$SRC_DIR/" "$TARGET:~/klipper/")"
if [[ -n "$RSYNC_OUTPUT" ]]; then
    echo "$RSYNC_OUTPUT"
fi

HAS_CHANGES=0
if [[ -n "$RSYNC_OUTPUT" ]]; then
    HAS_CHANGES=1
fi

if [[ "$HAS_CHANGES" -eq 0 ]]; then
    echo "==> No file changes detected"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
    if [[ "$HAS_CHANGES" -eq 1 || "$FORCE_RESTART" -eq 1 ]]; then
        # A print may have started during the steps above.
        if [[ "$FORCE_PRINT_CHECK" -eq 1 ]]; then
            echo "==> Skipping pre-restart active-print check (--force)"
        else
            check_print_state
        fi
        echo "==> Updating Klipper, Moonraker, and Mainsail on remote"
        if ! ssh "$TARGET" \
            'BIOKALICO_SKIP_SUBMODULE_SYNC=1 bash ~/klipper/scripts/biokalico-installer.sh update klipper moonraker mainsail'; then
            echo "ERROR: remote update failed" >&2
            exit 1
        fi
        ssh "$TARGET" 'systemctl --no-pager --full status klipper -n 20' || true
    else
        echo "==> Skipping remote sync (no changes). Use --force-restart to override"
    fi
fi

echo "==> Done"
