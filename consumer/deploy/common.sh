#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEPLOY_TRANSACTION_LOCK_FILE="${BUNDLE_ROOT}/.consumer-deploy.lock"
STATE_DIR="${SCRIPT_DIR}/state"
STATE_FILE="${STATE_DIR}/current.env"
STORAGE_MODE_STATE_FILE="${STATE_DIR}/storage-mode.env"
STORAGE_OPERATION_STATE_FILE="${STATE_DIR}/storage-operation.env"
BACKUP_ROOT="${SCRIPT_DIR}/backups"
MANIFEST_FILE="${SCRIPT_DIR}/manifest.json"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.release.yml"
STORAGE_VOLUME_COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.storage-volume.yml"
ACTIVE_STORAGE_VOLUME_COMPOSE_FILE="${STATE_DIR}/active-storage-volume.yml"
DEFAULT_RUNTIME_VOLUME_NAME="consumer-runtime-data"
DEFAULT_AUTH_VOLUME_NAME="consumer-auth-data"
BACKEND_COMPOSE_ENV_FILE="${BUNDLE_ROOT}/backend/.env"
LEGACY_COMPOSE_ENV_FILE="${BUNDLE_ROOT}/.env"
LEGACY_COMPOSE_HOST_ENV_FILE="${STATE_DIR}/legacy-compose-host.env"

log() {
    printf '[deploy] %s\n' "$*"
}

warn() {
    printf '[deploy][warn] %s\n' "$*" >&2
}

die() {
    printf '[deploy][error] %s\n' "$*" >&2
    exit 1
}

validate_storage_mode_value() {
    local storage_mode="$1"
    case "${storage_mode}" in
        bind|volume)
            return 0
            ;;
        *)
            die "STORAGE_MODE must be 'bind' or 'volume' (got '${storage_mode}')"
            ;;
    esac
}

validate_volume_name() {
    local volume_name="$1"
    local label="$2"

    case "${volume_name}" in
        ''|*[!a-zA-Z0-9_.-]*|[.-]*)
            die "${label} has an invalid Docker volume name: '${volume_name}'"
            ;;
    esac
}

validate_storage_state_name() {
    local name="$1"
    local label="$2"
    local prefix="$3"

    [ -z "${name}" ] && return 0
    case "${name}" in
        .|..|*[!a-zA-Z0-9_.-]*|*/*)
            die "${label} has an invalid persisted path name"
            ;;
    esac
    case "${name}" in
        "${prefix}"*)
            ;;
        *)
            die "${label} does not use the required '${prefix}' prefix"
            ;;
    esac
}

validate_pinned_utility_image() {
    local image="$1"

    case "${image}" in
        ''|latest|*:latest|*':')
            die "STORAGE_UTILITY_IMAGE must be pinned to a release tag or sha256 digest"
            ;;
        *@sha256:*)
            local digest="${image##*@sha256:}"
            case "${digest}" in
                *[!0-9a-f]*|'')
                    die "STORAGE_UTILITY_IMAGE has an invalid sha256 digest"
                    ;;
            esac
            [ "${#digest}" -eq 64 ] \
                || die "STORAGE_UTILITY_IMAGE has an invalid sha256 digest"
            ;;
        *:*)
            ;;
        *)
            die "STORAGE_UTILITY_IMAGE must include a non-latest tag or sha256 digest"
            ;;
    esac
}

read_state_value() {
    local file="$1"
    local key="$2"
    local count value

    count="$(grep -c "^${key}=" "${file}" 2>/dev/null || true)"
    case "${count}" in
        0)
            return 1
            ;;
        1)
            value="$(sed -n "s/^${key}=//p" "${file}")"
            value="${value%$'\r'}"
            printf '%s' "${value}"
            ;;
        *)
            die "State file ${file} contains duplicate ${key} entries"
            ;;
    esac
}

validate_inherited_flock_deployment_lock() {
    local lock_file="$1"
    local lock_fd="${DEPLOY_TRANSACTION_LOCK_FD:-}"
    local lock_identity fd_identity confirmed_lock_identity

    case "${lock_fd}" in
        ''|*[!0-9]*)
            die "Inherited deployment transaction lock has an invalid file descriptor"
            ;;
    esac
    if [ "${lock_fd}" -lt 3 ]; then
        die "Inherited deployment transaction lock cannot use a standard I/O file descriptor"
    fi
    [ -d /proc/self/fd ] \
        || die "Linux /proc/self/fd is required to validate an inherited deployment transaction lock"
    [ -e "/proc/self/fd/${lock_fd}" ] \
        || die "Inherited deployment transaction lock file descriptor is closed"
    lock_identity="$(stat -Lc '%d:%i' "${lock_file}" 2>/dev/null)" \
        || die "Cannot identify the stable deployment transaction lock file"
    fd_identity="$(stat -Lc '%d:%i' "/proc/self/fd/${lock_fd}" 2>/dev/null)" \
        || die "Cannot identify the inherited deployment transaction lock file descriptor"
    [ "${fd_identity}" = "${lock_identity}" ] \
        || die "Inherited deployment transaction lock file descriptor does not reference the stable lock file"
    flock -n "${lock_fd}" \
        || die "Inherited deployment transaction lock is not held by this process"
    confirmed_lock_identity="$(stat -Lc '%d:%i' "${lock_file}" 2>/dev/null)" \
        || die "Cannot revalidate the stable deployment transaction lock file"
    [ "${confirmed_lock_identity}" = "${fd_identity}" ] \
        || die "Stable deployment transaction lock file changed during inherited lock validation"
}

acquire_flock_deployment_lock() {
    local lock_fd

    command -v flock >/dev/null 2>&1 \
        || die "util-linux flock is required for deployment transaction locking"
    if [ "${DEPLOY_TRANSACTION_LOCK_BACKEND:-}" = "flock" ] \
        && [ -n "${DEPLOY_TRANSACTION_LOCK_TOKEN:-}" ]; then
        validate_inherited_flock_deployment_lock "${DEPLOY_TRANSACTION_LOCK_FILE}"
        DEPLOY_TRANSACTION_LOCK_HELD=true
        export DEPLOY_TRANSACTION_LOCK_HELD
        return 0
    fi

    exec {lock_fd}> "${DEPLOY_TRANSACTION_LOCK_FILE}"
    if ! flock -n "${lock_fd}"; then
        eval "exec ${lock_fd}>&-"
        die "Another deployment transaction is already running"
    fi
    DEPLOY_TRANSACTION_LOCK_BACKEND=flock
    DEPLOY_TRANSACTION_LOCK_FD="${lock_fd}"
    DEPLOY_TRANSACTION_LOCK_TOKEN="$$-${RANDOM}-${RANDOM}"
    DEPLOY_TRANSACTION_LOCK_HELD=true
    export DEPLOY_TRANSACTION_LOCK_BACKEND DEPLOY_TRANSACTION_LOCK_FD
    export DEPLOY_TRANSACTION_LOCK_TOKEN DEPLOY_TRANSACTION_LOCK_HELD
}

acquire_deployment_transaction_lock() {
    acquire_flock_deployment_lock
}

assert_deployment_transaction_lock() {
    [ "${DEPLOY_TRANSACTION_LOCK_HELD:-}" = "true" ] \
        || die "Deployment transaction lock is not held"
}

resolve_storage_mode() {
    local requested_mode="${STORAGE_MODE:-}"
    local persisted_mode="${PERSISTED_STORAGE_MODE:-}"
    local persisted_status="${PERSISTED_STORAGE_STATUS:-installed}"

    if [ -n "${requested_mode}" ]; then
        validate_storage_mode_value "${requested_mode}"
    fi
    if [ -n "${persisted_mode}" ]; then
        validate_storage_mode_value "${persisted_mode}"
    fi
    if [ -n "${requested_mode}" ] && [ -n "${persisted_mode}" ] \
        && [ "${requested_mode}" != "${persisted_mode}" ]; then
        die "Requested STORAGE_MODE '${requested_mode}' does not match ${persisted_status} storage mode '${persisted_mode}'; migrate storage explicitly before switching modes"
    fi

    RESOLVED_STORAGE_MODE="${requested_mode:-${persisted_mode:-bind}}"
}

initialize_storage_mode() {
    local installed_mode=""
    local marker_mode=""
    local marker_status=""
    local operation_status=""
    local operation_target_mode=""
    local operation_source_mode=""
    local operation_marker_mode=""

    PERSISTED_STORAGE_MODE=""
    PERSISTED_STORAGE_STATUS=""
    PENDING_STORAGE_MODE=""
    IGNORE_PENDING_STORAGE_MARKER=false
    PREFER_STORAGE_MARKER=false

    if [ -f "${STATE_FILE}" ]; then
        if ! installed_mode="$(read_state_value "${STATE_FILE}" CURRENT_STORAGE_MODE)"; then
            # Releases predating named-volume support only installed bind mounts.
            installed_mode="bind"
        fi
        validate_storage_mode_value "${installed_mode}"
    fi

    if [ -f "${STORAGE_MODE_STATE_FILE}" ]; then
        marker_status="$(read_state_value "${STORAGE_MODE_STATE_FILE}" STORAGE_MODE_STATUS)" \
            || die "Storage mode state is missing STORAGE_MODE_STATUS"
        marker_mode="$(read_state_value "${STORAGE_MODE_STATE_FILE}" STORAGE_MODE)" \
            || die "Storage mode state is missing STORAGE_MODE"
        case "${marker_status}" in
            pending|installed)
                ;;
            *)
                die "Storage mode state has invalid status '${marker_status}'"
                ;;
        esac
        validate_storage_mode_value "${marker_mode}"
    fi
    if [ -f "${STORAGE_OPERATION_STATE_FILE}" ]; then
        operation_status="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" STORAGE_OPERATION_STATUS 2>/dev/null || true)"
        operation_target_mode="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" TARGET_STORAGE_MODE 2>/dev/null || true)"
        operation_source_mode="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" SOURCE_STORAGE_MODE 2>/dev/null || true)"
        case "${operation_status}" in
            verified|bind-switched|pointer-switched|starting|healthy)
                operation_marker_mode="${operation_target_mode}"
                ;;
            rollback-switching|rollback-pointer-switched)
                operation_marker_mode="${operation_source_mode}"
                ;;
        esac
    fi
    if [ -n "${installed_mode}" ] \
        && [ "${marker_status}" = "installed" ] \
        && [ -n "${operation_marker_mode}" ] \
        && [ "${operation_marker_mode}" = "${marker_mode}" ]; then
        PREFER_STORAGE_MARKER=true
    fi

    if [ -n "${installed_mode}" ] && [ "${marker_status}" = "pending" ]; then
        PENDING_STORAGE_MODE="${marker_mode}"
        IGNORE_PENDING_STORAGE_MARKER=true
    elif [ -n "${installed_mode}" ] && [ -n "${marker_mode}" ] \
        && [ "${installed_mode}" != "${marker_mode}" ]; then
        if [ "${marker_status}" = "installed" ] \
            && [ -n "${operation_marker_mode}" ] \
            && [ "${operation_marker_mode}" = "${marker_mode}" ]; then
            # The verified target pointer was durably switched before the legacy
            # current.env mirror. Prefer it and let an idempotent retry finalize.
            PREFER_STORAGE_MARKER=true
        else
            die "Installed storage mode '${installed_mode}' conflicts with persisted ${marker_status} mode '${marker_mode}'"
        fi
    fi
    if [ -z "${installed_mode}" ] && [ "${marker_status}" = "installed" ]; then
        die "Installed storage mode marker exists without ${STATE_FILE}"
    fi

    if [ "${PREFER_STORAGE_MARKER}" = "true" ]; then
        PERSISTED_STORAGE_MODE="${marker_mode}"
        PERSISTED_STORAGE_STATUS="installed"
    elif [ -n "${installed_mode}" ]; then
        PERSISTED_STORAGE_MODE="${installed_mode}"
        PERSISTED_STORAGE_STATUS="installed"
    elif [ -n "${marker_mode}" ]; then
        PERSISTED_STORAGE_MODE="${marker_mode}"
        PERSISTED_STORAGE_STATUS="${marker_status}"
    fi
    CURRENT_STORAGE_MODE="${installed_mode}"
    resolve_storage_mode
}

initialize_storage_volume_names() {
    local current_runtime_volume=""
    local current_auth_volume=""
    local marker_runtime_volume=""
    local marker_auth_volume=""

    if [ -f "${STATE_FILE}" ]; then
        current_runtime_volume="$(read_state_value "${STATE_FILE}" CURRENT_RUNTIME_VOLUME 2>/dev/null || true)"
        current_auth_volume="$(read_state_value "${STATE_FILE}" CURRENT_AUTH_VOLUME 2>/dev/null || true)"
    fi
    if [ -f "${STORAGE_MODE_STATE_FILE}" ]; then
        marker_runtime_volume="$(read_state_value "${STORAGE_MODE_STATE_FILE}" RUNTIME_VOLUME_NAME 2>/dev/null || true)"
        marker_auth_volume="$(read_state_value "${STORAGE_MODE_STATE_FILE}" AUTH_VOLUME_NAME 2>/dev/null || true)"
    fi
    if [ "${IGNORE_PENDING_STORAGE_MARKER:-false}" = "true" ]; then
        marker_runtime_volume=""
        marker_auth_volume=""
    fi
    if [ "${PREFER_STORAGE_MARKER:-false}" = "true" ]; then
        current_runtime_volume=""
        current_auth_volume=""
    fi
    if [ -n "${current_runtime_volume}" ] && [ -n "${marker_runtime_volume}" ] \
        && [ "${current_runtime_volume}" != "${marker_runtime_volume}" ]; then
        die "Deployment and storage state disagree about the active runtime volume"
    fi
    if [ -n "${current_auth_volume}" ] && [ -n "${marker_auth_volume}" ] \
        && [ "${current_auth_volume}" != "${marker_auth_volume}" ]; then
        die "Deployment and storage state disagree about the active auth volume"
    fi
    ACTIVE_RUNTIME_VOLUME="${marker_runtime_volume:-${current_runtime_volume:-${DEFAULT_RUNTIME_VOLUME_NAME}}}"
    ACTIVE_AUTH_VOLUME="${marker_auth_volume:-${current_auth_volume:-${DEFAULT_AUTH_VOLUME_NAME}}}"
    validate_volume_name "${ACTIVE_RUNTIME_VOLUME}" "Runtime volume"
    validate_volume_name "${ACTIVE_AUTH_VOLUME}" "Auth volume"
}

run_deployment_state_io() {
    local operation="$1"
    shift

    if [ -n "${DEPLOY_STATE_IO_HELPER:-}" ]; then
        "${DEPLOY_STATE_IO_HELPER}" "${operation}" "$@"
        return
    fi
    command -v python3 >/dev/null 2>&1 \
        || die "Python 3 is required for crash-durable deployment state"
    python3 - "${operation}" "$@" <<'PY'
import os
import sys

operation = sys.argv[1]
paths = sys.argv[2:]

if operation == "fsync-file" and len(paths) == 1:
    descriptor = os.open(paths[0], os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
elif operation == "replace" and len(paths) == 2:
    os.replace(paths[0], paths[1])
elif operation == "fsync-dir" and len(paths) == 1:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(paths[0], flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
else:
    raise SystemExit(f"unsupported deployment state operation: {operation}")
PY
}

durably_replace_deployment_state() {
    local temp_file="$1"
    local target_file="$2"
    local parent_dir

    parent_dir="$(dirname "${target_file}")"
    if ! run_deployment_state_io fsync-file "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot durably persist deployment state: temporary file fsync failed"
    fi
    if ! run_deployment_state_io replace "${temp_file}" "${target_file}"; then
        rm -f "${temp_file}"
        die "Cannot durably persist deployment state: atomic replace failed"
    fi
    if ! run_deployment_state_io fsync-dir "${parent_dir}"; then
        rm -f "${temp_file}"
        die "Cannot durably persist deployment state: parent directory fsync failed"
    fi
}

persist_storage_mode_state() {
    local status="$1"
    local storage_mode="$2"
    local temp_file

    case "${status}" in
        pending|installed)
            ;;
        *)
            die "Cannot persist invalid storage mode status '${status}'"
            ;;
    esac
    validate_storage_mode_value "${storage_mode}"
    temp_file="$(mktemp "${STORAGE_MODE_STATE_FILE}.tmp.XXXXXX")" \
        || die "Cannot create temporary storage mode state"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary storage mode state"
    }
    if ! {
        printf 'STORAGE_MODE_STATUS=%s\n' "${status}"
        printf 'STORAGE_MODE=%s\n' "${storage_mode}"
    } > "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot write temporary storage mode state"
    fi
    durably_replace_deployment_state "${temp_file}" "${STORAGE_MODE_STATE_FILE}"

    PERSISTED_STORAGE_MODE="${storage_mode}"
    PERSISTED_STORAGE_STATUS="${status}"
    RESOLVED_STORAGE_MODE="${storage_mode}"
}

persist_storage_pointer_state() {
    local storage_mode="$1"
    local runtime_volume="$2"
    local auth_volume="$3"
    local temp_file

    validate_storage_mode_value "${storage_mode}"
    validate_volume_name "${runtime_volume}" "Runtime volume"
    validate_volume_name "${auth_volume}" "Auth volume"
    temp_file="$(mktemp "${STORAGE_MODE_STATE_FILE}.tmp.XXXXXX")" \
        || die "Cannot create temporary storage pointer state"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary storage pointer state"
    }
    {
        printf 'STORAGE_MODE_STATUS=installed\n'
        printf 'STORAGE_MODE=%s\n' "${storage_mode}"
        if [ -f "${STORAGE_MODE_STATE_FILE}" ]; then
            awk '
                !/^STORAGE_MODE_STATUS=/ && !/^STORAGE_MODE=/ &&
                !/^RUNTIME_VOLUME_NAME=/ && !/^AUTH_VOLUME_NAME=/ { print }
            ' "${STORAGE_MODE_STATE_FILE}"
        fi
        if [ "${runtime_volume}" != "${DEFAULT_RUNTIME_VOLUME_NAME}" ] \
            || [ "${auth_volume}" != "${DEFAULT_AUTH_VOLUME_NAME}" ]; then
            printf 'RUNTIME_VOLUME_NAME=%s\n' "${runtime_volume}"
            printf 'AUTH_VOLUME_NAME=%s\n' "${auth_volume}"
        fi
    } > "${temp_file}"
    durably_replace_deployment_state "${temp_file}" "${STORAGE_MODE_STATE_FILE}"
    PERSISTED_STORAGE_MODE="${storage_mode}"
    PERSISTED_STORAGE_STATUS="installed"
    RESOLVED_STORAGE_MODE="${storage_mode}"
}

persist_storage_operation_state() {
    local status="$1"
    local operation="$2"
    local target_mode="$3"
    local target_runtime_volume="$4"
    local target_auth_volume="$5"
    local digest="$6"
    local backup_dir="$7"
    local source_mode="$8"
    local source_runtime_volume="$9"
    local source_auth_volume="${10}"
    local staged_runtime_name="${11:-}"
    local staged_auth_name="${12:-}"
    local rollback_runtime_name="${13:-}"
    local rollback_auth_name="${14:-}"
    local temp_file
    local backup_dir_hex

    case "${status}" in
        staging|verified|bind-move-old-runtime|bind-move-old-auth|bind-promote-new-runtime|bind-promote-new-auth|bind-switched|pointer-switched|starting|healthy|rollback-switching|rollback-pointer-switched|complete|failed)
            ;;
        *)
            die "Invalid storage operation status: ${status}"
            ;;
    esac
    case "${operation}" in
        restore|migration)
            ;;
        *)
            die "Invalid storage operation type: ${operation}"
            ;;
    esac
    validate_storage_mode_value "${target_mode}"
    validate_storage_mode_value "${source_mode}"
    validate_volume_name "${target_runtime_volume}" "Target runtime volume"
    validate_volume_name "${target_auth_volume}" "Target auth volume"
    validate_volume_name "${source_runtime_volume}" "Source runtime volume"
    validate_volume_name "${source_auth_volume}" "Source auth volume"
    validate_storage_state_name "${staged_runtime_name}" "Staged runtime path" ".onto-runtime.${operation}-"
    validate_storage_state_name "${staged_auth_name}" "Staged auth path" ".onto-auth.${operation}-"
    validate_storage_state_name "${rollback_runtime_name}" "Rollback runtime path" ".onto-runtime.rollback-"
    validate_storage_state_name "${rollback_auth_name}" "Rollback auth path" ".onto-auth.rollback-"
    backup_dir_hex="$(python3 - "${backup_dir}" <<'PY'
import sys

value = sys.argv[1]
if "\x00" in value or "\n" in value or "\r" in value:
    raise SystemExit("storage backup path contains an unsupported control character")
print(value.encode("utf-8").hex())
PY
)" || die "Cannot encode storage backup path"
    temp_file="$(mktemp "${STORAGE_OPERATION_STATE_FILE}.tmp.XXXXXX")" \
        || die "Cannot create temporary storage operation state"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary storage operation state"
    }
    if ! {
        printf 'STORAGE_OPERATION_STATUS=%s\n' "${status}"
        printf 'STORAGE_OPERATION=%s\n' "${operation}"
        printf 'TARGET_STORAGE_MODE=%s\n' "${target_mode}"
        printf 'TARGET_RUNTIME_VOLUME=%s\n' "${target_runtime_volume}"
        printf 'TARGET_AUTH_VOLUME=%s\n' "${target_auth_volume}"
        printf 'SOURCE_STORAGE_MODE=%s\n' "${source_mode}"
        printf 'SOURCE_RUNTIME_VOLUME=%s\n' "${source_runtime_volume}"
        printf 'SOURCE_AUTH_VOLUME=%s\n' "${source_auth_volume}"
        printf 'STORAGE_TREE_SHA256=%s\n' "${digest}"
        printf 'BACKUP_DIR_HEX=%s\n' "${backup_dir_hex}"
        printf 'STAGED_RUNTIME_NAME=%s\n' "${staged_runtime_name}"
        printf 'STAGED_AUTH_NAME=%s\n' "${staged_auth_name}"
        printf 'ROLLBACK_RUNTIME_NAME=%s\n' "${rollback_runtime_name}"
        printf 'ROLLBACK_AUTH_NAME=%s\n' "${rollback_auth_name}"
    } > "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot write temporary storage operation state"
    fi
    durably_replace_deployment_state "${temp_file}" "${STORAGE_OPERATION_STATE_FILE}"
}

load_storage_operation_state() {
    local backup_dir_hex=""

    STORAGE_OPERATION_PRESENT=false
    JOURNAL_STORAGE_OPERATION_STATUS=""
    JOURNAL_STORAGE_OPERATION=""
    JOURNAL_TARGET_STORAGE_MODE=""
    JOURNAL_TARGET_RUNTIME_VOLUME=""
    JOURNAL_TARGET_AUTH_VOLUME=""
    JOURNAL_SOURCE_STORAGE_MODE=""
    JOURNAL_SOURCE_RUNTIME_VOLUME=""
    JOURNAL_SOURCE_AUTH_VOLUME=""
    JOURNAL_STORAGE_TREE_SHA256=""
    JOURNAL_BACKUP_DIR=""
    JOURNAL_STAGED_RUNTIME_PATH=""
    JOURNAL_STAGED_AUTH_PATH=""
    JOURNAL_ROLLBACK_RUNTIME_PATH=""
    JOURNAL_ROLLBACK_AUTH_PATH=""
    [ -f "${STORAGE_OPERATION_STATE_FILE}" ] || return 0

    JOURNAL_STORAGE_OPERATION_STATUS="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" STORAGE_OPERATION_STATUS)" \
        || die "Storage operation journal is missing status"
    JOURNAL_STORAGE_OPERATION="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" STORAGE_OPERATION)" \
        || die "Storage operation journal is missing operation"
    JOURNAL_TARGET_STORAGE_MODE="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" TARGET_STORAGE_MODE)" \
        || die "Storage operation journal is missing target mode"
    JOURNAL_TARGET_RUNTIME_VOLUME="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" TARGET_RUNTIME_VOLUME)" \
        || die "Storage operation journal is missing target runtime volume"
    JOURNAL_TARGET_AUTH_VOLUME="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" TARGET_AUTH_VOLUME)" \
        || die "Storage operation journal is missing target auth volume"
    JOURNAL_SOURCE_STORAGE_MODE="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" SOURCE_STORAGE_MODE)" \
        || die "Storage operation journal is missing source mode"
    JOURNAL_SOURCE_RUNTIME_VOLUME="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" SOURCE_RUNTIME_VOLUME)" \
        || die "Storage operation journal is missing source runtime volume"
    JOURNAL_SOURCE_AUTH_VOLUME="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" SOURCE_AUTH_VOLUME)" \
        || die "Storage operation journal is missing source auth volume"
    JOURNAL_STORAGE_TREE_SHA256="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" STORAGE_TREE_SHA256)" \
        || die "Storage operation journal is missing tree digest"
    backup_dir_hex="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" BACKUP_DIR_HEX)" \
        || die "Storage operation journal is missing encoded backup path"

    case "${JOURNAL_STORAGE_OPERATION_STATUS}" in
        staging|verified|bind-move-old-runtime|bind-move-old-auth|bind-promote-new-runtime|bind-promote-new-auth|bind-switched|pointer-switched|starting|healthy|rollback-switching|rollback-pointer-switched|complete|failed)
            ;;
        *)
            die "Storage operation journal has invalid status '${JOURNAL_STORAGE_OPERATION_STATUS}'"
            ;;
    esac
    case "${JOURNAL_STORAGE_OPERATION}" in
        restore|migration)
            ;;
        *)
            die "Storage operation journal has invalid operation '${JOURNAL_STORAGE_OPERATION}'"
            ;;
    esac
    validate_storage_mode_value "${JOURNAL_TARGET_STORAGE_MODE}"
    validate_storage_mode_value "${JOURNAL_SOURCE_STORAGE_MODE}"
    validate_volume_name "${JOURNAL_TARGET_RUNTIME_VOLUME}" "Journal target runtime volume"
    validate_volume_name "${JOURNAL_TARGET_AUTH_VOLUME}" "Journal target auth volume"
    validate_volume_name "${JOURNAL_SOURCE_RUNTIME_VOLUME}" "Journal source runtime volume"
    validate_volume_name "${JOURNAL_SOURCE_AUTH_VOLUME}" "Journal source auth volume"
    case "${JOURNAL_STORAGE_TREE_SHA256}" in
        *[!0-9a-f]*|'')
            die "Storage operation journal has an invalid tree digest"
            ;;
    esac
    [ "${#JOURNAL_STORAGE_TREE_SHA256}" -eq 64 ] \
        || die "Storage operation journal has an invalid tree digest"
    case "${backup_dir_hex}" in
        *[!0-9a-f]*|'')
            die "Storage operation journal has an invalid encoded backup path"
            ;;
    esac
    JOURNAL_BACKUP_DIR="$(python3 - "${backup_dir_hex}" <<'PY'
import re
import sys

try:
    value = bytes.fromhex(sys.argv[1]).decode("utf-8")
except (ValueError, UnicodeDecodeError) as exc:
    raise SystemExit(f"invalid encoded storage backup path: {exc}")
is_absolute = value.startswith("/") or re.match(r"^[A-Za-z]:/", value) is not None
if not is_absolute or "\x00" in value or "\n" in value or "\r" in value:
    raise SystemExit("storage backup path must be an absolute path without control characters")
print(value)
PY
)" || die "Cannot decode storage operation backup path"
    local staged_runtime_name staged_auth_name rollback_runtime_name rollback_auth_name
    staged_runtime_name="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" STAGED_RUNTIME_NAME 2>/dev/null || true)"
    staged_auth_name="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" STAGED_AUTH_NAME 2>/dev/null || true)"
    rollback_runtime_name="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" ROLLBACK_RUNTIME_NAME 2>/dev/null || true)"
    rollback_auth_name="$(read_state_value "${STORAGE_OPERATION_STATE_FILE}" ROLLBACK_AUTH_NAME 2>/dev/null || true)"
    validate_storage_state_name "${staged_runtime_name}" "Journal staged runtime path" ".onto-runtime.${JOURNAL_STORAGE_OPERATION}-"
    validate_storage_state_name "${staged_auth_name}" "Journal staged auth path" ".onto-auth.${JOURNAL_STORAGE_OPERATION}-"
    validate_storage_state_name "${rollback_runtime_name}" "Journal rollback runtime path" ".onto-runtime.rollback-"
    validate_storage_state_name "${rollback_auth_name}" "Journal rollback auth path" ".onto-auth.rollback-"
    [ -z "${staged_runtime_name}" ] \
        || JOURNAL_STAGED_RUNTIME_PATH="${BUNDLE_ROOT}/${staged_runtime_name}"
    [ -z "${staged_auth_name}" ] \
        || JOURNAL_STAGED_AUTH_PATH="${BUNDLE_ROOT}/${staged_auth_name}"
    [ -z "${rollback_runtime_name}" ] \
        || JOURNAL_ROLLBACK_RUNTIME_PATH="${BUNDLE_ROOT}/${rollback_runtime_name}"
    [ -z "${rollback_auth_name}" ] \
        || JOURNAL_ROLLBACK_AUTH_PATH="${BUNDLE_ROOT}/${rollback_auth_name}"
    STORAGE_OPERATION_PRESENT=true
}

storage_operation_matches() {
    local operation="$1"
    local digest="$2"
    local target_mode="$3"

    [ "${STORAGE_OPERATION_PRESENT:-false}" = true ] \
        && [ "${JOURNAL_STORAGE_OPERATION}" = "${operation}" ] \
        && [ "${JOURNAL_STORAGE_TREE_SHA256}" = "${digest}" ] \
        && [ "${JOURNAL_TARGET_STORAGE_MODE}" = "${target_mode}" ]
}

storage_operation_is_unfinished() {
    [ "${STORAGE_OPERATION_PRESENT:-false}" = true ] || return 1
    case "${JOURNAL_STORAGE_OPERATION_STATUS}" in
        complete|failed)
            return 1
            ;;
        *)
            return 0
            ;;
    esac
}

storage_assignment_file_matches() {
    local file="$1"
    local mode_key="$2"
    local runtime_key="$3"
    local auth_key="$4"
    local expected_mode="$5"
    local expected_runtime="$6"
    local expected_auth="$7"
    local actual_mode actual_runtime actual_auth

    [ -f "${file}" ] || return 1
    actual_mode="$(read_state_value "${file}" "${mode_key}" 2>/dev/null || true)"
    actual_runtime="$(read_state_value "${file}" "${runtime_key}" 2>/dev/null || true)"
    actual_auth="$(read_state_value "${file}" "${auth_key}" 2>/dev/null || true)"
    [ -n "${actual_mode}" ] || actual_mode="bind"
    actual_runtime="${actual_runtime:-${DEFAULT_RUNTIME_VOLUME_NAME}}"
    actual_auth="${actual_auth:-${DEFAULT_AUTH_VOLUME_NAME}}"
    [ "${actual_mode}" = "${expected_mode}" ] \
        && [ "${actual_runtime}" = "${expected_runtime}" ] \
        && [ "${actual_auth}" = "${expected_auth}" ]
}

current_storage_assignment_matches() {
    storage_assignment_file_matches \
        "${STATE_FILE}" CURRENT_STORAGE_MODE \
        CURRENT_RUNTIME_VOLUME CURRENT_AUTH_VOLUME "$@"
}

marker_storage_assignment_matches() {
    [ "$(read_state_value "${STORAGE_MODE_STATE_FILE}" STORAGE_MODE_STATUS 2>/dev/null || true)" = "installed" ] \
        && storage_assignment_file_matches \
            "${STORAGE_MODE_STATE_FILE}" STORAGE_MODE \
            RUNTIME_VOLUME_NAME AUTH_VOLUME_NAME "$@"
}

persist_storage_configuration() {
    local storage_mode="$1"
    local runtime_volume="$2"
    local auth_volume="$3"
    local operation="$4"
    local digest="$5"
    local rollback_mode="${6:-}"
    local rollback_runtime_path="${7:-}"
    local rollback_auth_path="${8:-}"
    local rollback_runtime_volume="${9:-}"
    local rollback_auth_volume="${10:-}"
    local backup_dir="${11:-}"
    local temp_file

    validate_storage_mode_value "${storage_mode}"
    validate_volume_name "${runtime_volume}" "Runtime volume"
    validate_volume_name "${auth_volume}" "Auth volume"
    if [ -n "${rollback_mode}" ]; then
        validate_storage_mode_value "${rollback_mode}"
    fi
    temp_file="$(mktemp "${STORAGE_MODE_STATE_FILE}.tmp.XXXXXX")" \
        || die "Cannot create temporary storage configuration"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary storage configuration"
    }
    if ! {
        printf 'STORAGE_MODE_STATUS=installed\n'
        printf 'STORAGE_MODE=%s\n' "${storage_mode}"
        if [ "${runtime_volume}" != "${DEFAULT_RUNTIME_VOLUME_NAME}" ] \
            || [ "${auth_volume}" != "${DEFAULT_AUTH_VOLUME_NAME}" ]; then
            printf 'RUNTIME_VOLUME_NAME=%s\n' "${runtime_volume}"
            printf 'AUTH_VOLUME_NAME=%s\n' "${auth_volume}"
        fi
        printf 'LAST_STORAGE_OPERATION=%s\n' "${operation}"
        printf 'LAST_STORAGE_OPERATION_DIGEST=%s\n' "${digest}"
        [ -z "${backup_dir}" ] || printf 'LAST_STORAGE_BACKUP_DIR=%q\n' "${backup_dir}"
        [ -z "${rollback_mode}" ] || printf 'ROLLBACK_STORAGE_MODE=%s\n' "${rollback_mode}"
        [ -z "${rollback_runtime_path}" ] || printf 'ROLLBACK_RUNTIME_PATH=%q\n' "${rollback_runtime_path}"
        [ -z "${rollback_auth_path}" ] || printf 'ROLLBACK_AUTH_PATH=%q\n' "${rollback_auth_path}"
        [ -z "${rollback_runtime_volume}" ] || printf 'ROLLBACK_RUNTIME_VOLUME=%s\n' "${rollback_runtime_volume}"
        [ -z "${rollback_auth_volume}" ] || printf 'ROLLBACK_AUTH_VOLUME=%s\n' "${rollback_auth_volume}"
    } > "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot write temporary storage configuration"
    fi
    durably_replace_deployment_state "${temp_file}" "${STORAGE_MODE_STATE_FILE}"
    PERSISTED_STORAGE_MODE="${storage_mode}"
    PERSISTED_STORAGE_STATUS="installed"
    RESOLVED_STORAGE_MODE="${storage_mode}"
    ACTIVE_RUNTIME_VOLUME="${runtime_volume}"
    ACTIVE_AUTH_VOLUME="${auth_volume}"
}

persist_current_storage_assignment() {
    local storage_mode="$1"
    local runtime_volume="$2"
    local auth_volume="$3"
    local temp_file

    require_file "${STATE_FILE}"
    validate_storage_mode_value "${storage_mode}"
    validate_volume_name "${runtime_volume}" "Runtime volume"
    validate_volume_name "${auth_volume}" "Auth volume"
    temp_file="$(mktemp "${STATE_FILE}.tmp.XXXXXX")" \
        || die "Cannot create temporary deployment storage state"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary deployment storage state"
    }
    if ! awk '
        !/^CURRENT_STORAGE_MODE=/ && !/^CURRENT_RUNTIME_VOLUME=/ && !/^CURRENT_AUTH_VOLUME=/ { print }
    ' "${STATE_FILE}" > "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot preserve deployment state during storage switch"
    fi
    {
        printf 'CURRENT_STORAGE_MODE=%s\n' "${storage_mode}"
        printf 'CURRENT_RUNTIME_VOLUME=%s\n' "${runtime_volume}"
        printf 'CURRENT_AUTH_VOLUME=%s\n' "${auth_volume}"
    } >> "${temp_file}"
    durably_replace_deployment_state "${temp_file}" "${STATE_FILE}"
    CURRENT_STORAGE_MODE="${storage_mode}"
}

persist_pending_storage_mode() {
    resolve_storage_mode
    if [ -z "${PERSISTED_STORAGE_MODE:-}" ]; then
        persist_storage_mode_state pending "${RESOLVED_STORAGE_MODE}"
    fi
}

# All lifecycle scripts source common.sh before reading state or invoking Docker.
acquire_deployment_transaction_lock
mkdir -p "${STATE_DIR}" "${BACKUP_ROOT}"
initialize_storage_mode
initialize_storage_volume_names

require_file() {
    local path="$1"
    [ -f "${path}" ] || die "Required file not found: ${path}"
}

require_dir() {
    local path="$1"
    [ -d "${path}" ] || die "Required directory not found: ${path}"
}

manifest_get() {
    local key="$1"
    require_file "${MANIFEST_FILE}"
    sed -n 's/^[[:space:]]*"'${key}'":[[:space:]]*"\([^"]*\)".*$/\1/p' "${MANIFEST_FILE}" | head -n 1
}

resolve_storage_utility_image() {
    local configured_image="${STORAGE_UTILITY_IMAGE:-}"
    local manifest_image=""

    if [ -z "${configured_image}" ] && [ -f "${MANIFEST_FILE}" ]; then
        manifest_image="$(manifest_get storage_utility_image)"
        [ -n "${manifest_image}" ] || manifest_image="$(manifest_get backend_image)"
    fi
    RESOLVED_STORAGE_UTILITY_IMAGE="${configured_image:-${manifest_image:-${CURRENT_BACKEND_IMAGE:-}}}"
    validate_pinned_utility_image "${RESOLVED_STORAGE_UTILITY_IMAGE}"
    docker image inspect "${RESOLVED_STORAGE_UTILITY_IMAGE}" >/dev/null 2>&1 \
        || die "Pinned storage utility image is not loaded: ${RESOLVED_STORAGE_UTILITY_IMAGE}"
}

run_storage_utility() {
    local phase="docker"
    local docker_args=()
    local command_args=()
    local argument

    resolve_storage_utility_image
    for argument in "$@"; do
        if [ "${argument}" = "--" ] && [ "${phase}" = "docker" ]; then
            phase="command"
            continue
        fi
        if [ "${phase}" = "docker" ]; then
            docker_args+=("${argument}")
        else
            command_args+=("${argument}")
        fi
    done
    [ "${#command_args[@]}" -gt 0 ] || die "Storage utility command is missing"
    docker run --rm \
        --network=none \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges \
        --user 0:0 \
        --tmpfs /tmp:rw,noexec,nosuid,size=256m \
        "${docker_args[@]}" \
        "${RESOLVED_STORAGE_UTILITY_IMAGE}" \
        "${command_args[@]}"
}

storage_backup_digest() {
    local backup_dir="$1"
    python3 - "${backup_dir}/storage-manifest.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
digest = payload.get("tree_sha256")
if not isinstance(digest, str) or len(digest) != 64:
    raise SystemExit("invalid storage tree digest")
print(digest)
PY
}

verify_storage_backup() {
    local backup_dir="$1"
    require_dir "${backup_dir}"
    require_file "${SCRIPT_DIR}/storage_integrity.py"
    python3 "${SCRIPT_DIR}/storage_integrity.py" verify-backup \
        --backup-dir "${backup_dir}" >/dev/null
}

active_storage_matches_backup() {
    local backup_dir="$1"

    case "${RESOLVED_STORAGE_MODE}" in
        bind)
            [ -d "${BUNDLE_ROOT}/.onto-runtime" ] \
                && [ -d "${BUNDLE_ROOT}/.onto-auth" ] \
                && python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
                    --runtime "${BUNDLE_ROOT}/.onto-runtime" \
                    --auth "${BUNDLE_ROOT}/.onto-auth" \
                    --manifest "${backup_dir}/storage-manifest.json" >/dev/null 2>&1
            ;;
        volume)
            docker volume inspect "${ACTIVE_RUNTIME_VOLUME}" >/dev/null 2>&1 \
                && docker volume inspect "${ACTIVE_AUTH_VOLUME}" >/dev/null 2>&1 \
                && run_storage_utility \
                    -v "${ACTIVE_RUNTIME_VOLUME}:/source/runtime:ro" \
                    -v "${ACTIVE_AUTH_VOLUME}:/source/auth:ro" \
                    -v "${backup_dir}:/backup:ro" \
                    -v "${SCRIPT_DIR}:/tool:ro" \
                    -- \
                    python /tool/storage_integrity.py verify-tree \
                    --runtime /source/runtime \
                    --auth /source/auth \
                    --manifest /backup/storage-manifest.json >/dev/null 2>&1
            ;;
    esac
}

target_storage_matches_backup() {
    local backup_dir="$1"
    local target_mode="$2"
    local target_runtime_volume="$3"
    local target_auth_volume="$4"

    validate_storage_mode_value "${target_mode}"
    case "${target_mode}" in
        bind)
            [ -d "${BUNDLE_ROOT}/.onto-runtime" ] \
                && [ -d "${BUNDLE_ROOT}/.onto-auth" ] \
                && python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
                    --runtime "${BUNDLE_ROOT}/.onto-runtime" \
                    --auth "${BUNDLE_ROOT}/.onto-auth" \
                    --manifest "${backup_dir}/storage-manifest.json" >/dev/null 2>&1
            ;;
        volume)
            validate_volume_name "${target_runtime_volume}" "Target runtime volume"
            validate_volume_name "${target_auth_volume}" "Target auth volume"
            docker volume inspect "${target_runtime_volume}" >/dev/null 2>&1 \
                && docker volume inspect "${target_auth_volume}" >/dev/null 2>&1 \
                && run_storage_utility \
                    -v "${target_runtime_volume}:/target/runtime:ro" \
                    -v "${target_auth_volume}:/target/auth:ro" \
                    -v "${backup_dir}:/backup:ro" \
                    -v "${SCRIPT_DIR}:/tool:ro" \
                    -- \
                    python /tool/storage_integrity.py verify-tree \
                    --runtime /target/runtime \
                    --auth /target/auth \
                    --manifest /backup/storage-manifest.json >/dev/null 2>&1
            ;;
    esac
}

target_storage_exists() {
    local target_mode="$1"
    local target_runtime_volume="$2"
    local target_auth_volume="$3"

    validate_storage_mode_value "${target_mode}"
    case "${target_mode}" in
        bind)
            [ -d "${BUNDLE_ROOT}/.onto-runtime" ] \
                && [ ! -L "${BUNDLE_ROOT}/.onto-runtime" ] \
                && [ -d "${BUNDLE_ROOT}/.onto-auth" ] \
                && [ ! -L "${BUNDLE_ROOT}/.onto-auth" ]
            ;;
        volume)
            validate_volume_name "${target_runtime_volume}" "Target runtime volume"
            validate_volume_name "${target_auth_volume}" "Target auth volume"
            docker volume inspect "${target_runtime_volume}" >/dev/null 2>&1 \
                && docker volume inspect "${target_auth_volume}" >/dev/null 2>&1
            ;;
    esac
}

stage_storage_backup() {
    local backup_dir="$1"
    local target_mode="$2"
    local operation="$3"
    local digest="$4"
    local allow_alternate_target="${5:-false}"
    local suffix="${digest:0:12}"
    local alternate_suffix=""
    local runtime_volume_exists=false
    local auth_volume_exists=false

    validate_storage_mode_value "${target_mode}"
    case "${allow_alternate_target}" in
        true|false)
            ;;
        *)
            die "Invalid staging rebuild policy"
            ;;
    esac
    STAGED_RUNTIME_PATH=""
    STAGED_AUTH_PATH=""
    TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}"
    TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}"

    case "${target_mode}" in
        bind)
            STAGED_RUNTIME_PATH="${BUNDLE_ROOT}/.onto-runtime.${operation}-${suffix}"
            STAGED_AUTH_PATH="${BUNDLE_ROOT}/.onto-auth.${operation}-${suffix}"
            python3 "${SCRIPT_DIR}/storage_integrity.py" extract-archive \
                --backup-dir "${backup_dir}" \
                --runtime "${STAGED_RUNTIME_PATH}" \
                --auth "${STAGED_AUTH_PATH}" >/dev/null \
                || return 1
            python3 "${SCRIPT_DIR}/storage_integrity.py" verify-tree \
                --runtime "${STAGED_RUNTIME_PATH}" \
                --auth "${STAGED_AUTH_PATH}" \
                --manifest "${backup_dir}/storage-manifest.json" >/dev/null \
                || return 1
            ;;
        volume)
            TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}.${operation}-${suffix}"
            TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}.${operation}-${suffix}"
            validate_volume_name "${TARGET_RUNTIME_VOLUME}" "Staging runtime volume"
            validate_volume_name "${TARGET_AUTH_VOLUME}" "Staging auth volume"
            if docker volume inspect "${TARGET_RUNTIME_VOLUME}" >/dev/null 2>&1; then
                runtime_volume_exists=true
            fi
            if docker volume inspect "${TARGET_AUTH_VOLUME}" >/dev/null 2>&1; then
                auth_volume_exists=true
            fi
            if [ "${runtime_volume_exists}" = true ] \
                && [ "${auth_volume_exists}" = true ]; then
                if target_storage_matches_backup \
                    "${backup_dir}" volume \
                    "${TARGET_RUNTIME_VOLUME}" "${TARGET_AUTH_VOLUME}"; then
                    log "Reusing verified storage target volumes for ${operation} ${digest}"
                    return 0
                fi
                if [ "${allow_alternate_target}" != true ]; then
                    warn "Existing storage target volumes do not match the verified backup; refusing to overwrite them"
                    return 1
                fi
            fi
            if [ "${runtime_volume_exists}" = true ] \
                || [ "${auth_volume_exists}" = true ]; then
                if [ "${allow_alternate_target}" != true ]; then
                    warn "Storage target volume pair is incomplete; refusing to overwrite it"
                    return 1
                fi
                alternate_suffix="$(date +%Y%m%d%H%M%S)-$$-${RANDOM}"
                TARGET_RUNTIME_VOLUME="${DEFAULT_RUNTIME_VOLUME_NAME}.${operation}-${suffix}-retry-${alternate_suffix}"
                TARGET_AUTH_VOLUME="${DEFAULT_AUTH_VOLUME_NAME}.${operation}-${suffix}-retry-${alternate_suffix}"
                validate_volume_name "${TARGET_RUNTIME_VOLUME}" "Alternate staging runtime volume"
                validate_volume_name "${TARGET_AUTH_VOLUME}" "Alternate staging auth volume"
                if docker volume inspect "${TARGET_RUNTIME_VOLUME}" >/dev/null 2>&1 \
                    || docker volume inspect "${TARGET_AUTH_VOLUME}" >/dev/null 2>&1; then
                    warn "Could not allocate a unique alternate storage target volume pair"
                    return 1
                fi
                log "Preserving the existing storage target and staging ${operation} into a unique alternate volume pair"
            fi
            docker volume create "${TARGET_RUNTIME_VOLUME}" >/dev/null \
                || return 1
            docker volume create "${TARGET_AUTH_VOLUME}" >/dev/null \
                || return 1
            run_storage_utility \
                -v "${backup_dir}:/backup:ro" \
                -v "${TARGET_RUNTIME_VOLUME}:/target/runtime:rw" \
                -v "${TARGET_AUTH_VOLUME}:/target/auth:rw" \
                -v "${SCRIPT_DIR}:/tool:ro" \
                -- \
                python /tool/storage_integrity.py extract-archive \
                --backup-dir /backup \
                --runtime /target/runtime \
                --auth /target/auth >/dev/null \
                || return 1
            run_storage_utility \
                -v "${backup_dir}:/backup:ro" \
                -v "${TARGET_RUNTIME_VOLUME}:/target/runtime:ro" \
                -v "${TARGET_AUTH_VOLUME}:/target/auth:ro" \
                -v "${SCRIPT_DIR}:/tool:ro" \
                -- \
                python /tool/storage_integrity.py verify-tree \
                --runtime /target/runtime \
                --auth /target/auth \
                --manifest /backup/storage-manifest.json >/dev/null \
                || return 1
            ;;
    esac
}

switch_staged_bind_paths() {
    local operation="$1"
    local digest="$2"
    local phase_callback="$3"
    local rollback_suffix
    local resuming=false

    case "${JOURNAL_STORAGE_OPERATION_STATUS:-}" in
        bind-move-old-runtime|bind-move-old-auth|bind-promote-new-runtime|bind-promote-new-auth|bind-switched)
            resuming=true
            ;;
    esac
    [ -n "${STAGED_RUNTIME_PATH:-}" ] && [ -n "${STAGED_AUTH_PATH:-}" ] \
        || die "Bind storage switch is missing deterministic staging paths"
    for path in \
        "${BUNDLE_ROOT}/.onto-runtime" "${BUNDLE_ROOT}/.onto-auth" \
        "${STAGED_RUNTIME_PATH}" "${STAGED_AUTH_PATH}" \
        "${ROLLBACK_RUNTIME_PATH:-}" "${ROLLBACK_AUTH_PATH:-}"; do
        [ -z "${path}" ] || [ ! -L "${path}" ] \
            || die "Bind storage switch refuses symbolic-link path: ${path}"
    done

    if [ "${resuming}" = false ]; then
        rollback_suffix="${operation}-${digest:0:12}-$(date +%Y%m%d-%H%M%S)-$$"
        if [ -e "${BUNDLE_ROOT}/.onto-runtime" ]; then
            ROLLBACK_RUNTIME_PATH="${BUNDLE_ROOT}/.onto-runtime.rollback-${rollback_suffix}"
            [ ! -e "${ROLLBACK_RUNTIME_PATH}" ] \
                || die "Runtime rollback path already exists"
        else
            ROLLBACK_RUNTIME_PATH=""
        fi
        if [ -e "${BUNDLE_ROOT}/.onto-auth" ]; then
            ROLLBACK_AUTH_PATH="${BUNDLE_ROOT}/.onto-auth.rollback-${rollback_suffix}"
            [ ! -e "${ROLLBACK_AUTH_PATH}" ] \
                || die "Auth rollback path already exists"
        else
            ROLLBACK_AUTH_PATH=""
        fi
    fi

    "${phase_callback}" bind-move-old-runtime
    if [ -n "${ROLLBACK_RUNTIME_PATH}" ] && [ ! -e "${ROLLBACK_RUNTIME_PATH}" ]; then
        [ -e "${BUNDLE_ROOT}/.onto-runtime" ] || return 1
        mv "${BUNDLE_ROOT}/.onto-runtime" "${ROLLBACK_RUNTIME_PATH}" || return 1
    fi

    "${phase_callback}" bind-move-old-auth
    if [ -n "${ROLLBACK_AUTH_PATH}" ] && [ ! -e "${ROLLBACK_AUTH_PATH}" ]; then
        [ -e "${BUNDLE_ROOT}/.onto-auth" ] || return 1
        mv "${BUNDLE_ROOT}/.onto-auth" "${ROLLBACK_AUTH_PATH}" || return 1
    fi

    "${phase_callback}" bind-promote-new-runtime
    if [ -e "${STAGED_RUNTIME_PATH}" ]; then
        [ ! -e "${BUNDLE_ROOT}/.onto-runtime" ] || return 1
        mv "${STAGED_RUNTIME_PATH}" "${BUNDLE_ROOT}/.onto-runtime" || return 1
    else
        [ -e "${BUNDLE_ROOT}/.onto-runtime" ] || return 1
    fi

    "${phase_callback}" bind-promote-new-auth
    if [ -e "${STAGED_AUTH_PATH}" ]; then
        [ ! -e "${BUNDLE_ROOT}/.onto-auth" ] || return 1
        mv "${STAGED_AUTH_PATH}" "${BUNDLE_ROOT}/.onto-auth" || return 1
    else
        [ -e "${BUNDLE_ROOT}/.onto-auth" ] || return 1
    fi
    "${phase_callback}" bind-switched
}

restore_previous_bind_paths() {
    local failed_suffix="failed-$(date +%Y%m%d-%H%M%S)-$$"
    local active_runtime="${BUNDLE_ROOT}/.onto-runtime"
    local active_auth="${BUNDLE_ROOT}/.onto-auth"

    if [ -n "${ROLLBACK_RUNTIME_PATH:-}" ]; then
        if [ -e "${ROLLBACK_RUNTIME_PATH}" ]; then
            [ ! -e "${active_runtime}" ] \
                || mv "${active_runtime}" "${BUNDLE_ROOT}/.onto-runtime.${failed_suffix}"
            mv "${ROLLBACK_RUNTIME_PATH}" "${active_runtime}"
        else
            [ -e "${active_runtime}" ] \
                || die "Runtime source is missing during bind rollback recovery"
        fi
    fi
    if [ -n "${ROLLBACK_AUTH_PATH:-}" ]; then
        if [ -e "${ROLLBACK_AUTH_PATH}" ]; then
            [ ! -e "${active_auth}" ] \
                || mv "${active_auth}" "${BUNDLE_ROOT}/.onto-auth.${failed_suffix}"
            mv "${ROLLBACK_AUTH_PATH}" "${active_auth}"
        else
            [ -e "${active_auth}" ] \
                || die "Auth source is missing during bind rollback recovery"
        fi
    fi
}

stop_release_writers() {
    compose_stop \
        "${CURRENT_BACKEND_IMAGE:-}" \
        "${CURRENT_FRONTEND_IMAGE:-}" \
        "${CURRENT_AUTH_IMAGE:-}" \
        "${CURRENT_JIUWENBOX_IMAGE:-onto-agent-jiuwenbox:latest}"
}

start_release_and_wait() {
    compose_up \
        "${CURRENT_BACKEND_IMAGE:-}" \
        "${CURRENT_FRONTEND_IMAGE:-}" \
        "${CURRENT_AUTH_IMAGE:-}" \
        "${CURRENT_JIUWENBOX_IMAGE:-onto-agent-jiuwenbox:latest}"
    wait_for_container "onto-agent-backend" 180 \
        && wait_for_container "onto-agent-mcp" 180 \
        && wait_for_container "onto-agent-frontend" 180 \
        && wait_for_container "onto-agent-auth-server" 180 \
        && wait_for_container "onto-agent-jiuwenbox" 180
}

load_state() {
    if [ -f "${STATE_FILE}" ]; then
        # shellcheck disable=SC1090
        source "${STATE_FILE}"
        # Releases predating named-volume support only installed bind mounts.
        CURRENT_STORAGE_MODE="${CURRENT_STORAGE_MODE:-bind}"
    else
        CURRENT_VERSION=""
        PREVIOUS_VERSION=""
        LAST_BACKUP_DIR=""
        CURRENT_BACKEND_IMAGE=""
        CURRENT_FRONTEND_IMAGE=""
        PREVIOUS_BACKEND_IMAGE=""
        PREVIOUS_FRONTEND_IMAGE=""
        CURRENT_AUTH_IMAGE=""
        PREVIOUS_AUTH_IMAGE=""
        CURRENT_JIUWENBOX_IMAGE=""
        PREVIOUS_JIUWENBOX_IMAGE=""
        CURRENT_STORAGE_MODE=""
    fi

    # 旧版本发布包写出的 state 文件没有 auth 镜像字段，升级时补默认值
    CURRENT_AUTH_IMAGE="${CURRENT_AUTH_IMAGE:-}"
    PREVIOUS_AUTH_IMAGE="${PREVIOUS_AUTH_IMAGE:-}"
    CURRENT_JIUWENBOX_IMAGE="${CURRENT_JIUWENBOX_IMAGE:-}"
    PREVIOUS_JIUWENBOX_IMAGE="${PREVIOUS_JIUWENBOX_IMAGE:-}"
    CURRENT_STORAGE_MODE="${CURRENT_STORAGE_MODE:-}"
}

assert_no_existing_install_for_install() {
    if [ -n "${CURRENT_VERSION:-}" ]; then
        return 0
    fi

    local evidence=""
    for marker in \
        "${BUNDLE_ROOT}/backend/.env" \
        "${BUNDLE_ROOT}/.onto-runtime" \
        "${BUNDLE_ROOT}/.onto-auth"; do
        if [ "${marker}" = "${BUNDLE_ROOT}/backend/.env" ] && [ -e "${marker}" ]; then
            evidence="${marker}"
            break
        fi
        if [ -d "${marker}" ] && find "${marker}" -mindepth 1 \( -type f -o -type l \) -print -quit | grep -q .; then
            evidence="${marker}"
            break
        fi
    done

    if [ -n "${evidence}" ]; then
        die "Existing installation detected without deployment state (${evidence}); use upgrade.sh or restore the deployment state before installing"
    fi
}

write_state() {
    local current_version="$1"
    local previous_version="$2"
    local last_backup_dir="$3"
    local current_backend_image="$4"
    local current_frontend_image="$5"
    local previous_backend_image="$6"
    local previous_frontend_image="$7"
    local current_auth_image="${8:-}"
    local previous_auth_image="${9:-}"
    local current_jiuwenbox_image="${10:-}"
    local previous_jiuwenbox_image="${11:-}"
    local resolved_storage_mode
    local temp_file

    assert_deployment_transaction_lock
    resolve_storage_mode
    resolved_storage_mode="${RESOLVED_STORAGE_MODE}"

    mkdir -p "${STATE_DIR}"
    temp_file="$(mktemp "${STATE_FILE}.tmp.XXXXXX")" \
        || die "Cannot create temporary deployment state"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary deployment state"
    }
    if ! {
        printf 'CURRENT_VERSION=%q\n' "${current_version}"
        printf 'PREVIOUS_VERSION=%q\n' "${previous_version}"
        printf 'LAST_BACKUP_DIR=%q\n' "${last_backup_dir}"
        printf 'CURRENT_BACKEND_IMAGE=%q\n' "${current_backend_image}"
        printf 'CURRENT_FRONTEND_IMAGE=%q\n' "${current_frontend_image}"
        printf 'PREVIOUS_BACKEND_IMAGE=%q\n' "${previous_backend_image}"
        printf 'PREVIOUS_FRONTEND_IMAGE=%q\n' "${previous_frontend_image}"
        printf 'CURRENT_AUTH_IMAGE=%q\n' "${current_auth_image}"
        printf 'PREVIOUS_AUTH_IMAGE=%q\n' "${previous_auth_image}"
        printf 'CURRENT_JIUWENBOX_IMAGE=%q\n' "${current_jiuwenbox_image}"
        printf 'PREVIOUS_JIUWENBOX_IMAGE=%q\n' "${previous_jiuwenbox_image}"
        printf 'CURRENT_STORAGE_MODE=%q\n' "${resolved_storage_mode}"
        printf 'CURRENT_RUNTIME_VOLUME=%q\n' "${ACTIVE_RUNTIME_VOLUME}"
        printf 'CURRENT_AUTH_VOLUME=%q\n' "${ACTIVE_AUTH_VOLUME}"
    } > "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot write temporary deployment state"
    fi
    durably_replace_deployment_state "${temp_file}" "${STATE_FILE}"
    persist_storage_pointer_state \
        "${resolved_storage_mode}" "${ACTIVE_RUNTIME_VOLUME}" "${ACTIVE_AUTH_VOLUME}"
    CURRENT_STORAGE_MODE="${resolved_storage_mode}"
}

ensure_runtime_layout() {
    mkdir -p \
        "${BUNDLE_ROOT}/backend/logs" \
        "${BUNDLE_ROOT}/frontend/logs/nginx"

    if [ "${RESOLVED_STORAGE_MODE}" = "bind" ]; then
        # auth-server is mandatory; bind mode keeps both data roots on the host.
        mkdir -p \
            "${BUNDLE_ROOT}/.onto-runtime/config" \
            "${BUNDLE_ROOT}/.onto-runtime/data/chats" \
            "${BUNDLE_ROOT}/.onto-runtime/data/executions" \
            "${BUNDLE_ROOT}/.onto-auth"
    fi
}

ensure_env_files() {
    if [ ! -f "${BUNDLE_ROOT}/backend/.env" ] && [ -f "${BUNDLE_ROOT}/backend/.env.example" ]; then
        cp "${BUNDLE_ROOT}/backend/.env.example" "${BUNDLE_ROOT}/backend/.env"
    fi
}

ensure_mcp_token_secret() {
    local env_file="${BUNDLE_ROOT}/backend/.env"
    local secret generated_secret
    require_file "${env_file}"
    secret="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?MCP_TOKEN_SECRET[[:space:]]*=[[:space:]]*//p' "${env_file}" | tail -n 1)"
    secret="${secret%\"}"
    secret="${secret#\"}"
    secret="${secret%\'}"
    secret="${secret#\'}"
    if [ -z "${secret}" ]; then
        generated_secret="$(generate_mcp_token_secret)"
        write_mcp_token_secret "${env_file}" "${generated_secret}"
        log "MCP_TOKEN_SECRET was missing; generated and saved an independent secret in backend/.env"
        return 0
    fi
    [ "${#secret}" -ge 32 ] || die \
        "Set MCP_TOKEN_SECRET to an independent random value of at least 32 characters in ${env_file}"
}

generate_mcp_token_secret() {
    local generated_secret
    if command -v openssl >/dev/null 2>&1; then
        generated_secret="$(openssl rand -hex 32)" || die "Failed to generate MCP_TOKEN_SECRET"
    elif command -v python3 >/dev/null 2>&1; then
        generated_secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
            || die "Failed to generate MCP_TOKEN_SECRET"
    else
        die "Cannot generate MCP_TOKEN_SECRET: openssl or Python 3 is required"
    fi
    [ "${#generated_secret}" -eq 64 ] || die "Failed to generate MCP_TOKEN_SECRET"
    printf '%s' "${generated_secret}"
}

write_mcp_token_secret() {
    local env_file="$1"
    local secret="$2"
    local temp_file
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "Cannot update ${env_file}"
    chmod 600 "${temp_file}" || {
        rm -f "${temp_file}"
        die "Cannot secure temporary configuration file"
    }
    if ! awk -v secret="${secret}" '
        BEGIN { updated = 0 }
        /^[[:space:]]*(export[[:space:]]+)?MCP_TOKEN_SECRET[[:space:]]*=/ {
            if (!updated) {
                print "MCP_TOKEN_SECRET=" secret
                updated = 1
            }
            next
        }
        { print }
        END {
            if (!updated) {
                print "MCP_TOKEN_SECRET=" secret
            }
        }
    ' "${env_file}" > "${temp_file}"; then
        rm -f "${temp_file}"
        die "Cannot update ${env_file}"
    fi
    mv "${temp_file}" "${env_file}" || {
        rm -f "${temp_file}"
        die "Cannot update ${env_file}"
    }
    chmod 600 "${env_file}" || die "Cannot secure ${env_file}"
}

detect_host_arch() {
    local arch
    arch="$(uname -m)"
    case "${arch}" in
        x86_64|amd64)
            printf 'amd64\n'
            ;;
        aarch64|arm64)
            printf 'arm64\n'
            ;;
        *)
            printf '%s\n' "${arch}"
            ;;
    esac
}

check_port_available() {
    local port="$1"

    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "( sport = :${port} )" | grep -q ":${port}"; then
            warn "Port ${port} appears to be in use"
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -ltn 2>/dev/null | grep -q "[\.:]${port}[[:space:]]"; then
            warn "Port ${port} appears to be in use"
        fi
    fi
}

docker_compose_cmd() {
    if [ -n "${DOCKER_COMPOSE_CMD:-}" ]; then
        case "${DOCKER_COMPOSE_CMD}" in
            "docker compose"|"docker-compose")
                printf '%s' "${DOCKER_COMPOSE_CMD}"
                return 0
                ;;
            *)
                unset DOCKER_COMPOSE_CMD
                ;;
        esac
    fi

    if docker compose version >/dev/null 2>&1; then
        export DOCKER_COMPOSE_CMD='docker compose'
        printf '%s' "${DOCKER_COMPOSE_CMD}"
        return 0
    fi

    if command -v docker-compose >/dev/null 2>&1; then
        # The release compose file uses profiles; older v1 builds cannot parse it.
        local legacy_version
        legacy_version="$(docker-compose version --short 2>/dev/null || true)"
        case "${legacy_version}" in
            1.2[0-7].*|1.1?.*|1.?.*|0.*)
                die "docker-compose ${legacy_version:-<unknown>} is too old (<1.28) and cannot run this release compose file; upgrade Docker Compose"
                ;;
        esac
        export DOCKER_COMPOSE_CMD='docker-compose'
        printf '%s' "${DOCKER_COMPOSE_CMD}"
        return 0
    fi

    return 1
}

render_legacy_compose_host_env() {
    local rendered_tmp="${LEGACY_COMPOSE_HOST_ENV_FILE}.tmp"

    if [ ! -f "${LEGACY_COMPOSE_ENV_FILE}" ]; then
        rm -f "${LEGACY_COMPOSE_HOST_ENV_FILE}" "${rendered_tmp}"
        return 0
    fi

    if ! awk '
        function trim(value) {
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            return value
        }
        {
            line = $0
            sub(/\r$/, "", line)
            if (line !~ /^[[:space:]]*(export[[:space:]]+)?(BACKEND_PORT|FRONTEND_PORT|MCP_SERVER_PORT)[[:space:]]*=/) {
                next
            }
            key = line
            sub(/^[[:space:]]*(export[[:space:]]+)?/, "", key)
            sub(/[[:space:]]*=.*/, "", key)
            value = line
            sub(/^[^=]*=[[:space:]]*/, "", value)
            sub(/[[:space:]]+#.*$/, "", value)
            value = trim(value)
            if (length(value) >= 2 && ((substr(value, 1, 1) == "\"" && substr(value, length(value), 1) == "\"") || (substr(value, 1, 1) == "\047" && substr(value, length(value), 1) == "\047"))) {
                value = substr(value, 2, length(value) - 2)
            }
            values[key] = value
        }
        END {
            keys[1] = "BACKEND_PORT"
            keys[2] = "FRONTEND_PORT"
            keys[3] = "MCP_SERVER_PORT"
            invalid = 0
            for (slot = 1; slot <= 3; slot++) {
                key = keys[slot]
                if (!(key in values)) {
                    continue
                }
                value = values[key]
                if (value !~ /^[0-9]+$/ || value + 0 < 1 || value + 0 > 65535) {
                    printf "Invalid legacy host port %s=%s\n", key, value > "/dev/stderr"
                    invalid = 1
                    continue
                }
                printf "%s=%s\n", key, value
            }
            exit invalid
        }
    ' "${LEGACY_COMPOSE_ENV_FILE}" > "${rendered_tmp}"; then
        rm -f "${rendered_tmp}"
        die "Legacy root .env contains an invalid host port"
    fi
    chmod 600 "${rendered_tmp}"
    mv "${rendered_tmp}" "${LEGACY_COMPOSE_HOST_ENV_FILE}"
}

build_compose_env_args() {
    render_legacy_compose_host_env
    COMPOSE_ENV_FILES=("${BACKEND_COMPOSE_ENV_FILE}")
    if [ -s "${LEGACY_COMPOSE_HOST_ENV_FILE}" ]; then
        COMPOSE_ENV_FILES+=("${LEGACY_COMPOSE_HOST_ENV_FILE}")
    fi
    COMPOSE_ENV_ARGS=()
    for env_file in "${COMPOSE_ENV_FILES[@]}"; do
        COMPOSE_ENV_ARGS+=(--env-file "${env_file}")
    done
}

build_compose_file_args() {
    local active_overlay_tmp

    assert_deployment_transaction_lock
    persist_pending_storage_mode
    COMPOSE_FILE_ARGS=(-f "${COMPOSE_FILE}")
    if [ "${RESOLVED_STORAGE_MODE}" = "volume" ]; then
        require_file "${STORAGE_VOLUME_COMPOSE_FILE}"
        COMPOSE_FILE_ARGS+=(-f "${STORAGE_VOLUME_COMPOSE_FILE}")
        if [ "${ACTIVE_RUNTIME_VOLUME}" != "${DEFAULT_RUNTIME_VOLUME_NAME}" ] \
            || [ "${ACTIVE_AUTH_VOLUME}" != "${DEFAULT_AUTH_VOLUME_NAME}" ]; then
            active_overlay_tmp="${ACTIVE_STORAGE_VOLUME_COMPOSE_FILE}.tmp"
            {
                printf 'volumes:\n'
                printf '  consumer-runtime-data:\n'
                printf '    name: %s\n' "${ACTIVE_RUNTIME_VOLUME}"
                printf '  consumer-auth-data:\n'
                printf '    name: %s\n' "${ACTIVE_AUTH_VOLUME}"
            } > "${active_overlay_tmp}"
            mv "${active_overlay_tmp}" "${ACTIVE_STORAGE_VOLUME_COMPOSE_FILE}"
            COMPOSE_FILE_ARGS+=(-f "${ACTIVE_STORAGE_VOLUME_COMPOSE_FILE}")
        else
            rm -f "${ACTIVE_STORAGE_VOLUME_COMPOSE_FILE}"
        fi
    fi
}

compose_up() {
    local compose_cmd
    build_compose_file_args
    compose_cmd="$(docker_compose_cmd)" || die "Docker Compose is not available (tried 'docker compose' and 'docker-compose')"
    build_compose_env_args

    # shellcheck disable=SC2086
    BACKEND_IMAGE="$1" FRONTEND_IMAGE="$2" AUTH_SERVER_IMAGE="${3:-}" JIUWENBOX_IMAGE="${4:-onto-agent-jiuwenbox:latest}" ${compose_cmd} \
        "${COMPOSE_ENV_ARGS[@]}" \
        "${COMPOSE_FILE_ARGS[@]}" \
        --project-directory "${BUNDLE_ROOT}" \
        up -d
}

compose_down() {
    local compose_cmd
    build_compose_file_args
    compose_cmd="$(docker_compose_cmd)" || die "Docker Compose is not available (tried 'docker compose' and 'docker-compose')"
    build_compose_env_args

    # shellcheck disable=SC2086
    BACKEND_IMAGE="$1" FRONTEND_IMAGE="$2" AUTH_SERVER_IMAGE="${3:-}" JIUWENBOX_IMAGE="${4:-onto-agent-jiuwenbox:latest}" ${compose_cmd} \
        "${COMPOSE_ENV_ARGS[@]}" \
        "${COMPOSE_FILE_ARGS[@]}" \
        --project-directory "${BUNDLE_ROOT}" \
        down
}

compose_stop() {
    local compose_cmd
    build_compose_file_args
    compose_cmd="$(docker_compose_cmd)" || die "Docker Compose is not available (tried 'docker compose' and 'docker-compose')"
    build_compose_env_args

    # shellcheck disable=SC2086
    BACKEND_IMAGE="$1" FRONTEND_IMAGE="$2" AUTH_SERVER_IMAGE="${3:-}" JIUWENBOX_IMAGE="${4:-onto-agent-jiuwenbox:latest}" ${compose_cmd} \
        "${COMPOSE_ENV_ARGS[@]}" \
        "${COMPOSE_FILE_ARGS[@]}" \
        --project-directory "${BUNDLE_ROOT}" \
        stop
}

container_health() {
    local container_name="$1"
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_name}" 2>/dev/null || true
}

wait_for_container() {
    local container_name="$1"
    local timeout_seconds="${2:-120}"
    local elapsed=0
    local status=""

    while [ "${elapsed}" -lt "${timeout_seconds}" ]; do
        status="$(container_health "${container_name}")"
        case "${status}" in
            healthy|running)
                return 0
                ;;
            unhealthy|exited|dead)
                return 1
                ;;
        esac
        sleep 2
        elapsed=$((elapsed + 2))
    done

    return 1
}

docker_load_image() {
    local image_tar="$1"
    require_file "${image_tar}"
    docker load -i "${image_tar}" >/dev/null
}
