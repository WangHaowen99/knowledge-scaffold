#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

FROM_VERSION=""
TO_VERSION="$(manifest_get app_version 2>/dev/null || true)"
DRY_RUN=false
WRITES_STOPPED=false
REPORT_FILE="${STATE_DIR}/migration-report.json"

POSITIONAL=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            ;;
        --confirm-writes-stopped)
            WRITES_STOPPED=true
            ;;
        --report)
            [ "$#" -ge 2 ] || die "--report requires a path"
            REPORT_FILE="$2"
            shift
            ;;
        --*)
            die "Unknown migration option: $1"
            ;;
        *)
            POSITIONAL+=("$1")
            ;;
    esac
    shift
done

if [ "${#POSITIONAL[@]}" -gt 0 ]; then
    FROM_VERSION="${POSITIONAL[0]}"
fi
if [ "${#POSITIONAL[@]}" -gt 1 ]; then
    TO_VERSION="${POSITIONAL[1]}"
fi
if [ "${#POSITIONAL[@]}" -gt 2 ]; then
    die "Usage: migrate.sh [from-version] [to-version] [--dry-run|--confirm-writes-stopped]"
fi

ARGS=(
    --root "${BUNDLE_ROOT}"
    --backup-root "${BACKUP_ROOT}"
    --report "${REPORT_FILE}"
    --from-version "${FROM_VERSION}"
    --to-version "${TO_VERSION}"
)

if [ "${DRY_RUN}" = true ]; then
    ARGS+=(--dry-run)
fi
if [ "${WRITES_STOPPED}" = true ]; then
    ARGS+=(--confirm-writes-stopped)
fi

python3 "${SCRIPT_DIR}/migrate_auth_workspace.py" "${ARGS[@]}"
