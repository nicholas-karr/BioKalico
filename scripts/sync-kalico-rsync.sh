#!/bin/bash
# Pull latest Kalico tree from a remote host via rsync over ssh.

set -euo pipefail

DEFAULT_REMOTE=""
DEFAULT_PORT="22"
DEFAULT_REMOTE_PATH="~/BioKalico"

REMOTE="${KALICO_SYNC_REMOTE:-$DEFAULT_REMOTE}"
PORT="${KALICO_SYNC_PORT:-$DEFAULT_PORT}"
REMOTE_PATH="${KALICO_SYNC_REMOTE_PATH:-$DEFAULT_REMOTE_PATH}"
DRY_RUN=0
DELETE_MODE=1
CONFIRMED=0

SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_PATH="${KALICO_SYNC_LOCAL_PATH:-$SRCDIR}"

EXCLUDES=(
    ".git/"
    "**/__pycache__/"
    "*.pyc"
    ".pytest_cache/"
    ".mypy_cache/"
    ".ruff_cache/"
)

usage() {
    cat <<'EOF'
Usage: sync-kalico-rsync.sh [options]

Options:
  --remote <user@host>       Remote ssh target (required unless KALICO_SYNC_REMOTE is set)
  --port <port>              Remote ssh port (default: 22)
  --remote-path <path>       Remote Kalico path (default: ~/BioKalico)
  --local-path <path>        Local destination path (default: repo root)
  --exclude <pattern>        Add an rsync exclude pattern (repeatable)
  --no-delete                Do not delete local files removed remotely
  --dry-run                  Show what would change without writing
  --yes                      Skip the confirmation prompt before a real
                              (non-dry-run) --delete sync
  -h, --help                 Show this help

Environment overrides:
  KALICO_SYNC_REMOTE
  KALICO_SYNC_PORT
  KALICO_SYNC_REMOTE_PATH
  KALICO_SYNC_LOCAL_PATH
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote)
            REMOTE="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --remote-path)
            REMOTE_PATH="$2"
            shift 2
            ;;
        --local-path)
            LOCAL_PATH="$2"
            shift 2
            ;;
        --exclude)
            EXCLUDES+=("$2")
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --no-delete)
            DELETE_MODE=0
            shift
            ;;
        --yes)
            CONFIRMED=1
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

if [[ -z "$REMOTE" ]]; then
    echo "A remote target is required; use --remote or KALICO_SYNC_REMOTE." >&2
    exit 2
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync is required but not found in PATH" >&2
    exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
    echo "ssh is required but not found in PATH" >&2
    exit 1
fi

mkdir -p "$LOCAL_PATH"

# By default this deletes local files missing on the remote, including this
# checkout's uncommitted work, so ask first. --yes, --dry-run and --no-delete
# skip the question.
if [[ "$DELETE_MODE" -eq 1 && "$DRY_RUN" -eq 0 && "$CONFIRMED" -eq 0 ]]; then
    echo "This will run rsync with --delete against:"
    echo "  ${LOCAL_PATH}"
    echo "Any local file or directory not present on ${REMOTE}:${REMOTE_PATH}"
    echo "(uncommitted changes, local-only scratch files, build output) will be"
    echo "permanently deleted."
    read -r -p "Type 'yes' to continue: " confirm_reply
    if [[ "$confirm_reply" != "yes" ]]; then
        echo "Aborted." >&2
        exit 1
    fi
fi

RSYNC_ARGS=(
    -az
    --human-readable
    --itemize-changes
    --info=stats2,progress2
    --partial
)

if [[ "$DELETE_MODE" -eq 1 ]]; then
    # Not --delete-excluded, which would delete excluded paths such as .git/.
    RSYNC_ARGS+=(--delete)
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    RSYNC_ARGS+=(--dry-run)
fi

for pattern in "${EXCLUDES[@]}"; do
    RSYNC_ARGS+=(--exclude "$pattern")
done

REMOTE_SOURCE="${REMOTE}:${REMOTE_PATH%/}/"

echo "Syncing Kalico from ${REMOTE_SOURCE} to ${LOCAL_PATH}"
rsync "${RSYNC_ARGS[@]}" -e "ssh -p ${PORT}" "$REMOTE_SOURCE" "${LOCAL_PATH%/}/"
echo "Sync complete."
