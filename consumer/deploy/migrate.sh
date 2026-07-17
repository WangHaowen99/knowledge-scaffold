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
BACKEND_REPORT_FILE="${STATE_DIR}/backend-migration-report.json"

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

ensure_runtime_layout
ensure_env_files
require_file "${BUNDLE_ROOT}/backend/.env"
command -v docker >/dev/null 2>&1 || die "Docker is required for registered backend migrations"

BACKEND_IMAGE="$(manifest_get backend_image)"
[ -n "${BACKEND_IMAGE}" ] || die "backend_image is missing in manifest.json"
docker image inspect "${BACKEND_IMAGE}" >/dev/null 2>&1 || die \
    "Backend image ${BACKEND_IMAGE} is not loaded; run deploy.sh or docker load the release image first"

# Ensure the one-shot migration runtime exists before the auth migration can apply changes.
python3 "${SCRIPT_DIR}/migrate_auth_workspace.py" "${ARGS[@]}"

BACKEND_MIGRATION_ARGS=(
    --runtime-root /app/.onto-runtime
    --report /migration-state/backend-migration-report.json
    --from-version "${FROM_VERSION}"
    --to-version "${TO_VERSION}"
)
if [ "${DRY_RUN}" = true ]; then
    BACKEND_MIGRATION_ARGS+=(--dry-run)
else
    BACKEND_MIGRATION_ARGS+=(--confirm-writes-stopped)
fi

docker run --rm \
    --env-file "${BUNDLE_ROOT}/backend/.env" \
    -e PROJECT_ROOT=/app/.onto-runtime \
    -v "${BUNDLE_ROOT}/.onto-runtime:/app/.onto-runtime" \
    -v "${STATE_DIR}:/migration-state" \
    "${BACKEND_IMAGE}" \
    python -m migrations.runner "${BACKEND_MIGRATION_ARGS[@]}"

require_file "${BACKEND_REPORT_FILE}"
