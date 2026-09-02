#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
chmod +x "${SCRIPT_DIR}/"*.sh

usage() {
    cat <<EOF
Usage: $(basename "$0") --to bind|volume [--dry-run]

Migrate runtime and auth data between bind mounts and Docker named volumes.
The source is mounted read-only, the target is staged separately, and the
active storage pointer changes only after canonical manifest verification.
EOF
}

TARGET_MODE=""
DRY_RUN=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --to)
            [ "$#" -ge 2 ] || die "--to requires bind or volume"
            TARGET_MODE="$2"
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            ;;
        *)
            die "Unknown storage migration option: $1"
            ;;
    esac
    shift
done
[ -n "${TARGET_MODE}" ] || {
    usage >&2
    exit 1
}
validate_storage_mode_value "${TARGET_MODE}"

load_state
[ -n "${CURRENT_VERSION:-}" ] || die "Storage migration requires an installed release"
SOURCE_MODE="${RESOLVED_STORAGE_MODE}"
SOURCE_RUNTIME_VOLUME="${ACTIVE_RUNTIME_VOLUME}"
SOURCE_AUTH_VOLUME="${ACTIVE_AUTH_VOLUME}"

TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}"
TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}"
ROLLBACK_RUNTIME_PATH=""
ROLLBACK_AUTH_PATH=""
ROLLBACK_RUNTIME_VOLUME=""
ROLLBACK_AUTH_VOLUME=""
BACKUP_DIR=""
DIGEST=""

persist_migration_phase() {
    local staged_runtime_name=""
    local staged_auth_name=""
    local rollback_runtime_name=""
    local rollback_auth_name=""

    if [ "${TARGET_MODE}" = "bind" ]; then
        staged_runtime_name="${STAGED_RUNTIME_PATH:+${STAGED_RUNTIME_PATH##*/}}"
        staged_auth_name="${STAGED_AUTH_PATH:+${STAGED_AUTH_PATH##*/}}"
        rollback_runtime_name="${ROLLBACK_RUNTIME_PATH:+${ROLLBACK_RUNTIME_PATH##*/}}"
        rollback_auth_name="${ROLLBACK_AUTH_PATH:+${ROLLBACK_AUTH_PATH##*/}}"
    fi
    persist_storage_operation_state \
        "$1" migration "${TARGET_MODE}" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        "${DIGEST}" "${BACKUP_DIR}" "${SOURCE_MODE}" \
        "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}" \
        "${staged_runtime_name}" "${staged_auth_name}" \
        "${rollback_runtime_name}" "${rollback_auth_name}"
}

prepare_migration_rollback_material() {
    if [ "${SOURCE_MODE}" = "volume" ]; then
        ROLLBACK_RUNTIME_VOLUME="${SOURCE_RUNTIME_VOLUME}"
        ROLLBACK_AUTH_VOLUME="${SOURCE_AUTH_VOLUME}"
    else
        ROLLBACK_RUNTIME_PATH="${BUNDLE_ROOT}/.onto-runtime"
        ROLLBACK_AUTH_PATH="${BUNDLE_ROOT}/.onto-auth"
    fi
}

recover_failed_migration() {
    warn "Completing interrupted storage migration rollback"
    persist_migration_phase rollback-switching
    stop_release_writers >/dev/null 2>&1 || true
    if [ "${TARGET_MODE}" = "bind" ] \
        && { [ -n "${ROLLBACK_RUNTIME_PATH}" ] || [ -n "${ROLLBACK_AUTH_PATH}" ]; }; then
        restore_previous_bind_paths
    fi
    persist_storage_configuration \
        "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}" \
        migration "${DIGEST}" "${TARGET_MODE}" "" "" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" "${BACKUP_DIR}"
    persist_current_storage_assignment \
        "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"
    persist_migration_phase rollback-pointer-switched
    start_release_and_wait >/dev/null 2>&1 \
        || warn "Source release did not recover automatically; data pointer was reverted"
    persist_migration_phase failed
    die "Migrated target failed health verification; source storage remains active"
}

persist_migrated_target_pointer() {
    prepare_migration_rollback_material
    persist_storage_configuration \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        migration "${DIGEST}" "${SOURCE_MODE}" \
        "${ROLLBACK_RUNTIME_PATH}" "${ROLLBACK_AUTH_PATH}" \
        "${ROLLBACK_RUNTIME_VOLUME}" "${ROLLBACK_AUTH_VOLUME}" "${BACKUP_DIR}"
    persist_current_storage_assignment \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"
}

finish_migrated_target_startup() {
    persist_migration_phase starting
    if ! start_release_and_wait; then
        recover_failed_migration
    fi
    persist_migration_phase healthy
    {
        printf 'FROM_STORAGE_MODE=%s\n' "${SOURCE_MODE}"
        printf 'TO_STORAGE_MODE=%s\n' "${TARGET_MODE}"
        printf 'STORAGE_TREE_SHA256=%s\n' "${DIGEST}"
        printf 'SOURCE_RUNTIME_VOLUME=%s\n' "${SOURCE_RUNTIME_VOLUME}"
        printf 'SOURCE_AUTH_VOLUME=%s\n' "${SOURCE_AUTH_VOLUME}"
        printf 'TARGET_RUNTIME_VOLUME=%s\n' "${TARGET_RUNTIME_VOLUME}"
        printf 'TARGET_AUTH_VOLUME=%s\n' "${TARGET_AUTH_VOLUME}"
    } > "${BACKUP_DIR}/migration-metadata.env"
    persist_migration_phase complete
    log "Storage migration completed and verified: ${SOURCE_MODE} -> ${TARGET_MODE} (${DIGEST})"
}

complete_migrated_target() {
    if ! target_storage_matches_backup \
        "${BACKUP_DIR}" "${TARGET_MODE}" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
        recover_failed_migration
    fi
    persist_migrated_target_pointer
    persist_migration_phase pointer-switched
    finish_migrated_target_startup
}

resume_started_migration() {
    if ! target_storage_exists \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
        recover_failed_migration
    fi
    if ! marker_storage_assignment_matches \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
        recover_failed_migration
    fi
    if ! current_storage_assignment_matches \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        && ! current_storage_assignment_matches \
            "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"; then
        recover_failed_migration
    fi
    persist_migrated_target_pointer
    finish_migrated_target_startup
}

resume_migration_staging() {
    log "Resuming interrupted migration target staging from ${JOURNAL_STORAGE_OPERATION_STATUS}"
    if marker_storage_assignment_matches \
        "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}" \
        && current_storage_assignment_matches \
            "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"; then
        :
    elif marker_storage_assignment_matches \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        && { current_storage_assignment_matches \
                "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}" \
            || current_storage_assignment_matches \
                "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; }; then
        complete_migrated_target
        return
    else
        recover_failed_migration
    fi
    stop_release_writers
    if ! stage_storage_backup \
        "${BACKUP_DIR}" "${TARGET_MODE}" migration "${DIGEST}" true; then
        recover_failed_migration
    fi
    persist_migration_phase verified
    if [ "${TARGET_MODE}" = "bind" ]; then
        switch_staged_bind_paths migration "${DIGEST}" persist_migration_phase \
            || die "Could not resume the verified bind storage switch"
    fi
    complete_migrated_target
}

load_storage_operation_state
if [ "${STORAGE_OPERATION_PRESENT}" = true ] \
    && [ "${JOURNAL_STORAGE_OPERATION}" = "migration" ] \
    && [ "${JOURNAL_TARGET_STORAGE_MODE}" = "${TARGET_MODE}" ]; then
    case "${JOURNAL_STORAGE_OPERATION_STATUS}" in
        staging|verified|bind-move-old-runtime|bind-move-old-auth|bind-promote-new-runtime|bind-promote-new-auth|bind-switched|pointer-switched|starting|healthy|rollback-switching|rollback-pointer-switched)
            BACKUP_DIR="${JOURNAL_BACKUP_DIR}"
            verify_storage_backup "${BACKUP_DIR}"
            DIGEST="$(storage_backup_digest "${BACKUP_DIR}")"
            [ "${DIGEST}" = "${JOURNAL_STORAGE_TREE_SHA256}" ] \
                || die "Storage migration journal digest does not match its backup"
            SOURCE_MODE="${JOURNAL_SOURCE_STORAGE_MODE}"
            SOURCE_RUNTIME_VOLUME="${JOURNAL_SOURCE_RUNTIME_VOLUME}"
            SOURCE_AUTH_VOLUME="${JOURNAL_SOURCE_AUTH_VOLUME}"
            TARGET_RUNTIME_VOLUME="${JOURNAL_TARGET_RUNTIME_VOLUME}"
            TARGET_AUTH_VOLUME="${JOURNAL_TARGET_AUTH_VOLUME}"
            STAGED_RUNTIME_PATH="${JOURNAL_STAGED_RUNTIME_PATH}"
            STAGED_AUTH_PATH="${JOURNAL_STAGED_AUTH_PATH}"
            ROLLBACK_RUNTIME_PATH="${JOURNAL_ROLLBACK_RUNTIME_PATH}"
            ROLLBACK_AUTH_PATH="${JOURNAL_ROLLBACK_AUTH_PATH}"
            case "${JOURNAL_STORAGE_OPERATION_STATUS}" in
                rollback-switching|rollback-pointer-switched)
                    prepare_migration_rollback_material
                    recover_failed_migration
                    ;;
                bind-move-old-runtime|bind-move-old-auth|bind-promote-new-runtime|bind-promote-new-auth|bind-switched)
                    log "Resuming interrupted bind migration switch from ${JOURNAL_STORAGE_OPERATION_STATUS}"
                    switch_staged_bind_paths migration "${DIGEST}" persist_migration_phase \
                        || die "Could not resume the verified bind storage switch"
                    python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
                        --runtime "${BUNDLE_ROOT}/.onto-runtime" \
                        --auth "${BUNDLE_ROOT}/.onto-auth" \
                        --manifest "${BACKUP_DIR}/storage-manifest.json" >/dev/null \
                        || die "Resumed bind storage switch does not match the verified backup"
                    complete_migrated_target
                    exit 0
                    ;;
                staging|verified)
                    resume_migration_staging
                    exit 0
                    ;;
                pointer-switched|starting|healthy)
                    log "Resuming interrupted storage migration from ${JOURNAL_STORAGE_OPERATION_STATUS}"
                    case "${JOURNAL_STORAGE_OPERATION_STATUS}" in
                        pointer-switched)
                            complete_migrated_target
                            ;;
                        starting|healthy)
                            resume_started_migration
                            ;;
                    esac
                    exit 0
                    ;;
            esac
            ;;
    esac
fi
if storage_operation_is_unfinished; then
    die "An unfinished storage operation must be recovered before starting a different migration"
fi

if [ "${TARGET_MODE}" = "${RESOLVED_STORAGE_MODE}" ]; then
    if [ "${PREFER_STORAGE_MARKER:-false}" = "true" ]; then
        persist_current_storage_assignment \
            "${RESOLVED_STORAGE_MODE}" "${ACTIVE_RUNTIME_VOLUME}" "${ACTIVE_AUTH_VOLUME}"
    fi
    log "Active storage already uses ${TARGET_MODE}; nothing to migrate"
    exit 0
fi

resolve_storage_utility_image
case "${SOURCE_MODE}" in
    bind)
        require_dir "${BUNDLE_ROOT}/.onto-runtime"
        require_dir "${BUNDLE_ROOT}/.onto-auth"
        SOURCE_RUNTIME_MOUNT="${BUNDLE_ROOT}/.onto-runtime"
        SOURCE_AUTH_MOUNT="${BUNDLE_ROOT}/.onto-auth"
        ;;
    volume)
        docker volume inspect "${SOURCE_RUNTIME_VOLUME}" >/dev/null 2>&1 \
            || die "Runtime source volume does not exist: ${SOURCE_RUNTIME_VOLUME}"
        docker volume inspect "${SOURCE_AUTH_VOLUME}" >/dev/null 2>&1 \
            || die "Auth source volume does not exist: ${SOURCE_AUTH_VOLUME}"
        SOURCE_RUNTIME_MOUNT="${SOURCE_RUNTIME_VOLUME}"
        SOURCE_AUTH_MOUNT="${SOURCE_AUTH_VOLUME}"
        ;;
esac

if [ "${DRY_RUN}" = "true" ]; then
    log "Storage migration preflight passed: ${SOURCE_MODE} -> ${TARGET_MODE}"
    exit 0
fi

log "Stopping storage writers before migration"
stop_release_writers

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/storage-migration-${TIMESTAMP}-${SOURCE_MODE}-to-${TARGET_MODE}"
mkdir -p "${BACKUP_DIR}"
if ! run_storage_utility \
    -v "${SOURCE_RUNTIME_MOUNT}:/source/runtime:ro" \
    -v "${SOURCE_AUTH_MOUNT}:/source/auth:ro" \
    -v "${BACKUP_DIR}:/backup:rw" \
    -v "${SCRIPT_DIR}:/tool:ro" \
    -- \
    python /tool/storage_integrity.py create-archive \
    --runtime /source/runtime \
    --auth /source/auth \
    --archive /backup/storage-data.tar.gz \
    --manifest /backup/storage-manifest.json \
    --storage-mode "${SOURCE_MODE}" >/dev/null; then
    start_release_and_wait >/dev/null 2>&1 \
        || warn "Could not restart source release after failed migration backup"
    die "Could not create and validate the read-only source archive"
fi
if ! run_storage_utility \
    -v "${BACKUP_DIR}:/backup:ro" \
    -v "${SCRIPT_DIR}:/tool:ro" \
    -- \
    python /tool/storage_integrity.py verify-backup --backup-dir /backup >/dev/null; then
    start_release_and_wait >/dev/null 2>&1 \
        || warn "Could not restart source release after failed migration backup verification"
    die "Source archive verification failed; active storage was not switched"
fi

DIGEST="$(storage_backup_digest "${BACKUP_DIR}")"
if [ "${TARGET_MODE}" = "volume" ]; then
    TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}.migration-${DIGEST:0:12}"
    TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}.migration-${DIGEST:0:12}"
fi
persist_migration_phase staging

if ! stage_storage_backup "${BACKUP_DIR}" "${TARGET_MODE}" migration "${DIGEST}"; then
    persist_storage_operation_state \
        failed migration "${TARGET_MODE}" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        "${DIGEST}" "${BACKUP_DIR}" "${SOURCE_MODE}" \
        "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"
    start_release_and_wait >/dev/null 2>&1 \
        || warn "Could not restart source release after failed migration staging"
    die "Staging verification failed; active storage was not switched"
fi

persist_migration_phase verified

if [ "${TARGET_MODE}" = "bind" ]; then
    switch_staged_bind_paths migration "${DIGEST}" persist_migration_phase \
        || die "Could not atomically switch verified bind staging directories"
    python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
        --runtime "${BUNDLE_ROOT}/.onto-runtime" \
        --auth "${BUNDLE_ROOT}/.onto-auth" \
        --manifest "${BACKUP_DIR}/storage-manifest.json" >/dev/null \
        || die "Switched bind storage does not match the verified backup"
fi

complete_migrated_target
