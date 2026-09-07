#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
chmod +x "${SCRIPT_DIR}/"*.sh

usage() {
    cat <<EOF
Usage: $(basename "$0") <backup-dir> [--target-mode bind|volume]

Restore runtime and auth data from a canonical storage backup. The active
data is never overwritten in place; writers are stopped, a temporary target
is populated and verified, then the storage pointer is switched.
EOF
}

[ "$#" -ge 1 ] || {
    usage >&2
    exit 1
}

BACKUP_DIR="$1"
shift
TARGET_MODE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-mode)
            [ "$#" -ge 2 ] || die "--target-mode requires bind or volume"
            TARGET_MODE="$2"
            shift
            ;;
        *)
            die "Unknown restore option: $1"
            ;;
    esac
    shift
done

require_dir "${BACKUP_DIR}"
BACKUP_DIR="$(cd "${BACKUP_DIR}" && pwd)"
load_state
[ -n "${CURRENT_VERSION:-}" ] || die "Restore requires an installed release"
SOURCE_MODE="${RESOLVED_STORAGE_MODE}"
SOURCE_RUNTIME_VOLUME="${ACTIVE_RUNTIME_VOLUME}"
SOURCE_AUTH_VOLUME="${ACTIVE_AUTH_VOLUME}"
TARGET_MODE="${TARGET_MODE:-${SOURCE_MODE}}"
validate_storage_mode_value "${TARGET_MODE}"

# The backup is fully validated while active services are still untouched.
verify_storage_backup "${BACKUP_DIR}"
DIGEST="$(storage_backup_digest "${BACKUP_DIR}")"

TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}"
TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}"
if [ "${TARGET_MODE}" = "volume" ]; then
    TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}.restore-${DIGEST:0:12}"
    TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}.restore-${DIGEST:0:12}"
fi

ROLLBACK_RUNTIME_PATH=""
ROLLBACK_AUTH_PATH=""
ROLLBACK_RUNTIME_VOLUME=""
ROLLBACK_AUTH_VOLUME=""

persist_restore_phase() {
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
        "$1" restore "${TARGET_MODE}" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        "${DIGEST}" "${BACKUP_DIR}" "${SOURCE_MODE}" \
        "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}" \
        "${staged_runtime_name}" "${staged_auth_name}" \
        "${rollback_runtime_name}" "${rollback_auth_name}"
}

prepare_restore_rollback_material() {
    if [ "${SOURCE_MODE}" = "volume" ]; then
        ROLLBACK_RUNTIME_VOLUME="${SOURCE_RUNTIME_VOLUME}"
        ROLLBACK_AUTH_VOLUME="${SOURCE_AUTH_VOLUME}"
    elif [ "${TARGET_MODE}" = "volume" ]; then
        ROLLBACK_RUNTIME_PATH="${BUNDLE_ROOT}/.onto-runtime"
        ROLLBACK_AUTH_PATH="${BUNDLE_ROOT}/.onto-auth"
    fi
}

recover_failed_restore() {
    warn "Completing interrupted restore rollback to source storage"
    persist_restore_phase rollback-switching
    stop_release_writers >/dev/null 2>&1 || true
    if [ "${TARGET_MODE}" = "bind" ] \
        && { [ -n "${ROLLBACK_RUNTIME_PATH}" ] || [ -n "${ROLLBACK_AUTH_PATH}" ]; }; then
        restore_previous_bind_paths
    fi
    persist_storage_configuration \
        "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}" \
        restore "${DIGEST}" "${TARGET_MODE}" "" "" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" "${BACKUP_DIR}"
    persist_current_storage_assignment \
        "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"
    persist_restore_phase rollback-pointer-switched
    start_release_and_wait >/dev/null 2>&1 \
        || warn "Source release did not recover automatically; data pointer was reverted"
    persist_restore_phase failed
    die "Restore target failed health verification; source storage remains active"
}

persist_restored_target_pointer() {
    prepare_restore_rollback_material
    persist_storage_configuration \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        restore "${DIGEST}" "${SOURCE_MODE}" \
        "${ROLLBACK_RUNTIME_PATH}" "${ROLLBACK_AUTH_PATH}" \
        "${ROLLBACK_RUNTIME_VOLUME}" "${ROLLBACK_AUTH_VOLUME}" "${BACKUP_DIR}"
    persist_current_storage_assignment \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"
}

finish_restored_target_startup() {
    persist_restore_phase starting
    if ! start_release_and_wait; then
        recover_failed_restore
    fi
    persist_restore_phase healthy
    persist_restore_phase complete
    log "Storage restore completed and verified: ${DIGEST}"
}

complete_restored_target() {
    if ! target_storage_matches_backup \
        "${BACKUP_DIR}" "${TARGET_MODE}" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
        recover_failed_restore
    fi
    persist_restored_target_pointer
    persist_restore_phase pointer-switched
    finish_restored_target_startup
}

resume_started_restore() {
    if ! target_storage_exists \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
        recover_failed_restore
    fi
    if ! marker_storage_assignment_matches \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
        recover_failed_restore
    fi
    if ! current_storage_assignment_matches \
        "${TARGET_MODE}" "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        && ! current_storage_assignment_matches \
            "${SOURCE_MODE}" "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"; then
        recover_failed_restore
    fi
    persist_restored_target_pointer
    finish_restored_target_startup
}

resume_restore_staging() {
    log "Resuming interrupted restore target staging from ${JOURNAL_STORAGE_OPERATION_STATUS}"
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
        complete_restored_target
        return
    else
        recover_failed_restore
    fi
    stop_release_writers
    if ! stage_storage_backup \
        "${BACKUP_DIR}" "${TARGET_MODE}" restore "${DIGEST}" true; then
        recover_failed_restore
    fi
    persist_restore_phase verified
    if [ "${TARGET_MODE}" = "bind" ]; then
        switch_staged_bind_paths restore "${DIGEST}" persist_restore_phase \
            || die "Could not resume the verified bind storage switch"
    fi
    complete_restored_target
}

load_storage_operation_state
if storage_operation_matches restore "${DIGEST}" "${TARGET_MODE}"; then
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
            prepare_restore_rollback_material
            recover_failed_restore
            ;;
        staging|verified)
            resume_restore_staging
            exit 0
            ;;
        pointer-switched|starting|healthy)
            log "Resuming interrupted restore ${DIGEST} from ${JOURNAL_STORAGE_OPERATION_STATUS}"
            case "${JOURNAL_STORAGE_OPERATION_STATUS}" in
                pointer-switched)
                    complete_restored_target
                    ;;
                starting|healthy)
                    resume_started_restore
                    ;;
            esac
            exit 0
            ;;
        bind-move-old-runtime|bind-move-old-auth|bind-promote-new-runtime|bind-promote-new-auth|bind-switched)
            log "Resuming interrupted bind restore switch from ${JOURNAL_STORAGE_OPERATION_STATUS}"
            switch_staged_bind_paths restore "${DIGEST}" persist_restore_phase \
                || die "Could not resume the verified bind storage switch"
            python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
                --runtime "${BUNDLE_ROOT}/.onto-runtime" \
                --auth "${BUNDLE_ROOT}/.onto-auth" \
                --manifest "${BACKUP_DIR}/storage-manifest.json" >/dev/null \
                || die "Resumed bind storage switch does not match the verified backup"
            complete_restored_target
            exit 0
            ;;
    esac
fi
if storage_operation_is_unfinished; then
    die "An unfinished storage operation must be recovered before starting a different restore"
fi

if [ "${TARGET_MODE}" = "${RESOLVED_STORAGE_MODE}" ] \
    && active_storage_matches_backup "${BACKUP_DIR}"; then
    log "Active storage already matches backup ${DIGEST}; nothing to restore"
    exit 0
fi

if [ "${SOURCE_MODE}" = "volume" ] || [ "${TARGET_MODE}" = "volume" ]; then
    resolve_storage_utility_image
fi

persist_restore_phase staging

log "Stopping storage writers before restore"
stop_release_writers

if ! stage_storage_backup "${BACKUP_DIR}" "${TARGET_MODE}" restore "${DIGEST}"; then
    persist_storage_operation_state \
        failed restore "${TARGET_MODE}" \
        "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}" \
        "${DIGEST}" "${BACKUP_DIR}" "${SOURCE_MODE}" \
        "${SOURCE_RUNTIME_VOLUME}" "${SOURCE_AUTH_VOLUME}"
    start_release_and_wait >/dev/null 2>&1 || warn "Could not restart source release after failed restore staging"
    die "Staging verification failed; active storage was not switched"
fi

persist_restore_phase verified

if [ "${TARGET_MODE}" = "bind" ]; then
    switch_staged_bind_paths restore "${DIGEST}" persist_restore_phase \
        || die "Could not atomically switch verified bind staging directories"
    python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
        --runtime "${BUNDLE_ROOT}/.onto-runtime" \
        --auth "${BUNDLE_ROOT}/.onto-auth" \
        --manifest "${BACKUP_DIR}/storage-manifest.json" >/dev/null \
        || die "Switched bind storage does not match the verified backup"
fi
complete_restored_target
