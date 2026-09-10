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
STORAGE_REPORT_FILE="${REPORT_FILE%.json}-persistent-storage.json"
LEGACY_BACKUP_DIR=""

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
        --legacy-backup-dir)
            [ "$#" -ge 2 ] || die "--legacy-backup-dir requires a path"
            LEGACY_BACKUP_DIR="$2"
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

ensure_runtime_layout
ensure_env_files
require_file "${BUNDLE_ROOT}/backend/.env"
command -v docker >/dev/null 2>&1 || die "Docker is required for registered backend migrations"

BACKEND_IMAGE="$(manifest_get backend_image)"
[ -n "${BACKEND_IMAGE}" ] || die "backend_image is missing in manifest.json"
docker image inspect "${BACKEND_IMAGE}" >/dev/null 2>&1 || die \
    "Backend image ${BACKEND_IMAGE} is not loaded; run deploy.sh or docker load the release image first"

if [ "${DRY_RUN}" != true ] && [ "${WRITES_STOPPED}" != true ]; then
    die "Apply migration requires --confirm-writes-stopped"
fi

REPORT_HOST_DIR="$(cd "$(dirname "${REPORT_FILE}")" && pwd)"
REPORT_FILE="${REPORT_HOST_DIR}/$(basename "${REPORT_FILE}")"
BACKEND_REPORT_FILE="${REPORT_HOST_DIR}/backend-migration-report.json"
STORAGE_REPORT_FILE="${REPORT_HOST_DIR}/$(basename "${REPORT_FILE%.json}-persistent-storage.json")"
SYSTEM_CONFIG_REPORT="${REPORT_HOST_DIR}/$(basename "${REPORT_FILE%.json}-system-config.json")"
DATA_ACCESS=rw
if [ "${DRY_RUN}" = true ]; then
    DATA_ACCESS=ro
fi

case "${RESOLVED_STORAGE_MODE}" in
    bind)
        RUNTIME_STORAGE_SOURCE="${BUNDLE_ROOT}/.onto-runtime"
        AUTH_STORAGE_SOURCE="${BUNDLE_ROOT}/.onto-auth"
        ;;
    volume)
        docker volume inspect "${ACTIVE_RUNTIME_VOLUME}" >/dev/null 2>&1 \
            || die "Runtime volume does not exist: ${ACTIVE_RUNTIME_VOLUME}"
        docker volume inspect "${ACTIVE_AUTH_VOLUME}" >/dev/null 2>&1 \
            || die "Auth volume does not exist: ${ACTIVE_AUTH_VOLUME}"
        RUNTIME_STORAGE_SOURCE="${ACTIVE_RUNTIME_VOLUME}"
        AUTH_STORAGE_SOURCE="${ACTIVE_AUTH_VOLUME}"
        ;;
esac

MIGRATION_DOCKER_ARGS=(
    --rm
    --network=none
    --read-only
    --cap-drop ALL
    --security-opt no-new-privileges
    --user 0:0
    --tmpfs /tmp:rw,noexec,nosuid,size=256m
    -e "LOG_DIR=/tmp/logs"
    -v "${RUNTIME_STORAGE_SOURCE}:/release/.onto-runtime:${DATA_ACCESS}"
    -v "${AUTH_STORAGE_SOURCE}:/release/.onto-auth:${DATA_ACCESS}"
    -v "${BUNDLE_ROOT}/backend:/release/backend:ro"
    -v "${SCRIPT_DIR}:/deploy-tools:ro"
    -v "${REPORT_HOST_DIR}:/migration-reports:rw"
    -v "${BACKUP_ROOT}:/migration-backups:rw"
)
# The persistent-storage and system-config utilities use same-directory
# temporary files for atomic replacements in backend/. A single-file bind
# mount cannot replace its mount point, so give only those two invocations a
# writable backend directory during apply. Dry-run and the other migrations
# keep the release source read-only.
STORAGE_MIGRATION_DOCKER_ARGS=("${MIGRATION_DOCKER_ARGS[@]}")
STORAGE_BACKEND_MOUNT_REPLACED=false
for index in "${!STORAGE_MIGRATION_DOCKER_ARGS[@]}"; do
    if [ "${STORAGE_MIGRATION_DOCKER_ARGS[$index]}" = "${BUNDLE_ROOT}/backend:/release/backend:ro" ]; then
        STORAGE_MIGRATION_DOCKER_ARGS[$index]="${BUNDLE_ROOT}/backend:/release/backend:${DATA_ACCESS}"
        STORAGE_BACKEND_MOUNT_REPLACED=true
        break
    fi
done
if [ "${STORAGE_BACKEND_MOUNT_REPLACED}" != true ]; then
    die "Persistent-storage migration backend mount was not found"
fi
SYSTEM_CONFIG_DOCKER_ARGS=("${MIGRATION_DOCKER_ARGS[@]}")
SYSTEM_CONFIG_BACKEND_MOUNT_REPLACED=false
for index in "${!SYSTEM_CONFIG_DOCKER_ARGS[@]}"; do
    if [ "${SYSTEM_CONFIG_DOCKER_ARGS[$index]}" = "${BUNDLE_ROOT}/backend:/release/backend:ro" ]; then
        SYSTEM_CONFIG_DOCKER_ARGS[$index]="${BUNDLE_ROOT}/backend:/release/backend:${DATA_ACCESS}"
        SYSTEM_CONFIG_BACKEND_MOUNT_REPLACED=true
        break
    fi
done
if [ "${SYSTEM_CONFIG_BACKEND_MOUNT_REPLACED}" != true ]; then
    die "System-config migration backend mount was not found"
fi
if [ -n "${LEGACY_BACKUP_DIR}" ]; then
    require_dir "${LEGACY_BACKUP_DIR}"
    MIGRATION_DOCKER_ARGS+=(-v "${LEGACY_BACKUP_DIR}:/legacy-backup:ro")
    STORAGE_MIGRATION_DOCKER_ARGS+=(-v "${LEGACY_BACKUP_DIR}:/legacy-backup:ro")
    SYSTEM_CONFIG_DOCKER_ARGS+=(-v "${LEGACY_BACKUP_DIR}:/legacy-backup:ro")
fi

AUTH_ARGS=(
    --root /release
    --backup-root /migration-backups
    --report "/migration-reports/$(basename "${REPORT_FILE}")"
    --from-version "${FROM_VERSION}"
    --to-version "${TO_VERSION}"
)
STORAGE_ARGS=(
    --root /release
    --report "/migration-reports/$(basename "${STORAGE_REPORT_FILE}")"
    --storage-mode "${RESOLVED_STORAGE_MODE}"
)
SYSTEM_CONFIG_ARGS=(
    --root /release
    --report "/migration-reports/$(basename "${SYSTEM_CONFIG_REPORT}")"
)
BACKEND_MIGRATION_ARGS=(
    --runtime-root /release/.onto-runtime
    --report "/migration-reports/$(basename "${BACKEND_REPORT_FILE}")"
    --from-version "${FROM_VERSION}"
    --to-version "${TO_VERSION}"
)
if [ "${DRY_RUN}" = true ]; then
    AUTH_ARGS+=(--dry-run)
    STORAGE_ARGS+=(--dry-run)
    SYSTEM_CONFIG_ARGS+=(--dry-run)
    BACKEND_MIGRATION_ARGS+=(--dry-run)
else
    AUTH_ARGS+=(--confirm-writes-stopped)
    BACKEND_MIGRATION_ARGS+=(--confirm-writes-stopped)
fi
if [ -n "${LEGACY_BACKUP_DIR}" ]; then
    STORAGE_ARGS+=(--legacy-backup-dir /legacy-backup)
fi

# Every registered migration sees the same active bind/volume data roots.
docker run "${MIGRATION_DOCKER_ARGS[@]}" "${BACKEND_IMAGE}" \
    python /deploy-tools/migrate_auth_workspace.py "${AUTH_ARGS[@]}"
docker run "${STORAGE_MIGRATION_DOCKER_ARGS[@]}" "${BACKEND_IMAGE}" \
    python /deploy-tools/migrate_persistent_storage.py "${STORAGE_ARGS[@]}"
docker run "${SYSTEM_CONFIG_DOCKER_ARGS[@]}" "${BACKEND_IMAGE}" \
    python /deploy-tools/migrate_system_config.py "${SYSTEM_CONFIG_ARGS[@]}"
docker run "${MIGRATION_DOCKER_ARGS[@]}" \
    --env-file "${BUNDLE_ROOT}/backend/.env" \
    -e PROJECT_ROOT=/release/.onto-runtime \
    "${BACKEND_IMAGE}" \
    python -m migrations.runner "${BACKEND_MIGRATION_ARGS[@]}"

require_file "${REPORT_FILE}"
require_file "${STORAGE_REPORT_FILE}"
require_file "${SYSTEM_CONFIG_REPORT}"
require_file "${BACKEND_REPORT_FILE}"
