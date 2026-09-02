#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_state

WRITES_STOPPED=false
POSITIONAL=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --confirm-writes-stopped)
            WRITES_STOPPED=true
            ;;
        --*)
            die "Unknown backup option: $1"
            ;;
        *)
            POSITIONAL+=("$1")
            ;;
    esac
    shift
done
[ "${#POSITIONAL[@]}" -le 2 ] \
    || die "Usage: backup.sh --confirm-writes-stopped [target-version] [from-version]"
[ "${WRITES_STOPPED}" = true ] \
    || die "Backup requires --confirm-writes-stopped before reading runtime/auth facts"

TARGET_VERSION="${POSITIONAL[0]:-$(manifest_get app_version)}"
FROM_VERSION="${POSITIONAL[1]:-${CURRENT_VERSION:-none}}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}_${FROM_VERSION}_to_${TARGET_VERSION}"

mkdir -p "${BACKUP_DIR}"

backup_path_if_present() {
    local source_path="$1"
    local archive_name="$2"

    if [ -e "${source_path}" ]; then
        tar -C "${BUNDLE_ROOT}" -czf "${BACKUP_DIR}/${archive_name}" "${source_path#${BUNDLE_ROOT}/}"
    fi
}

backup_legacy_container_storage_if_present() {
    local container_name="onto-agent-backend"
    local staging_root="${BACKUP_DIR}/legacy-backend-data"
    local resolved_paths=""
    local chat_source=""
    local execution_source=""
    local exported=0

    docker container inspect "${container_name}" >/dev/null 2>&1 || return 0

    resolved_paths="$(python3 "${SCRIPT_DIR}/migrate_persistent_storage.py" \
        --root "${BUNDLE_ROOT}" \
        --print-legacy-export-paths)"
    chat_source="$(printf '%s\n' "${resolved_paths}" | sed -n 's/^CHAT_STORAGE_DIR=//p' | head -n 1)"
    execution_source="$(printf '%s\n' "${resolved_paths}" | sed -n 's/^EXECUTION_STORAGE_DIR=//p' | head -n 1)"

    rm -rf "${staging_root}"
    mkdir -p "${staging_root}/chats" "${staging_root}/executions"

    if [ -n "${chat_source}" ] && docker cp "${container_name}:${chat_source}/." "${staging_root}/chats" 2>/dev/null; then
        if find "${staging_root}/chats" -mindepth 1 -print -quit | grep -q .; then
            exported=1
        else
            rm -rf "${staging_root}/chats"
        fi
    else
        rm -rf "${staging_root}/chats"
    fi

    if [ -n "${execution_source}" ] && docker cp "${container_name}:${execution_source}/." "${staging_root}/executions" 2>/dev/null; then
        if find "${staging_root}/executions" -mindepth 1 -print -quit | grep -q .; then
            exported=1
        else
            rm -rf "${staging_root}/executions"
        fi
    else
        rm -rf "${staging_root}/executions"
    fi

    if [ "${exported}" -eq 1 ]; then
        tar -C "${BACKUP_DIR}" -czf "${BACKUP_DIR}/legacy-backend-data.tar.gz" "legacy-backend-data"
    fi

    rm -rf "${staging_root}"
}

backup_canonical_storage() {
    local integrity_tool="${SCRIPT_DIR}/storage_integrity.py"
    local archive="${BACKUP_DIR}/storage-data.tar.gz"
    local manifest="${BACKUP_DIR}/storage-manifest.json"

    require_file "${integrity_tool}"
    case "${RESOLVED_STORAGE_MODE}" in
        bind)
            require_dir "${BUNDLE_ROOT}/.onto-runtime"
            require_dir "${BUNDLE_ROOT}/.onto-auth"
            python3 "${integrity_tool}" create-archive \
                --runtime "${BUNDLE_ROOT}/.onto-runtime" \
                --auth "${BUNDLE_ROOT}/.onto-auth" \
                --archive "${archive}" \
                --manifest "${manifest}" \
                --storage-mode bind >/dev/null
            python3 "${integrity_tool}" verify-backup \
                --backup-dir "${BACKUP_DIR}" >/dev/null
            ;;
        volume)
            # Validate the project release image pin before touching source storage.
            resolve_storage_utility_image
            docker volume inspect "${ACTIVE_RUNTIME_VOLUME}" >/dev/null 2>&1 \
                || die "Runtime volume does not exist: ${ACTIVE_RUNTIME_VOLUME}"
            docker volume inspect "${ACTIVE_AUTH_VOLUME}" >/dev/null 2>&1 \
                || die "Auth volume does not exist: ${ACTIVE_AUTH_VOLUME}"
            run_storage_utility \
                -v "${ACTIVE_RUNTIME_VOLUME}:/source/runtime:ro" \
                -v "${ACTIVE_AUTH_VOLUME}:/source/auth:ro" \
                -v "${BACKUP_DIR}:/backup:rw" \
                -v "${SCRIPT_DIR}:/tool:ro" \
                -- \
                python /tool/storage_integrity.py create-archive \
                --runtime /source/runtime \
                --auth /source/auth \
                --archive /backup/storage-data.tar.gz \
                --manifest /backup/storage-manifest.json \
                --storage-mode volume >/dev/null
            run_storage_utility \
                -v "${BACKUP_DIR}:/backup:ro" \
                -v "${SCRIPT_DIR}:/tool:ro" \
                -- \
                python /tool/storage_integrity.py verify-backup \
                --backup-dir /backup >/dev/null
            ;;
    esac
}

backup_path_if_present "${BUNDLE_ROOT}/backend/.env" "backend-env.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/backend/logs" "backend-logs.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/frontend/logs" "frontend-logs.tar.gz"
if [ "${RESOLVED_STORAGE_MODE}" = "bind" ]; then
    # Keep the legacy per-directory archives for backward-compatible manual recovery.
    backup_path_if_present "${BUNDLE_ROOT}/.onto-runtime" "onto-runtime.tar.gz"
    backup_path_if_present "${BUNDLE_ROOT}/.onto-auth" "onto-auth.tar.gz"
fi
backup_canonical_storage
backup_legacy_container_storage_if_present

{
    printf '%s\n' \
        '.onto-runtime/users.json' \
        '.onto-runtime/workspaces/' \
        '.onto-runtime/config/' \
        '.onto-runtime/data/chats/' \
        '.onto-runtime/data/executions/' \
        '.onto-runtime/workspaces/*/runtime/users/' \
        'legacy-backend-data/chats/' \
        'legacy-backend-data/executions/' \
        '.onto-runtime/mcp-tokens.json' \
        '.onto-runtime/**/graph*migration*' \
        '.onto-auth/users.json' \
        '.onto-auth/keys/'
} > "${BACKUP_DIR}/data-inventory.txt"

if [ -n "${CURRENT_BACKEND_IMAGE:-}" ] && docker image inspect "${CURRENT_BACKEND_IMAGE}" >/dev/null 2>&1; then
    docker save -o "${BACKUP_DIR}/backend-image.tar" "${CURRENT_BACKEND_IMAGE}"
fi

if [ -n "${CURRENT_FRONTEND_IMAGE:-}" ] && docker image inspect "${CURRENT_FRONTEND_IMAGE}" >/dev/null 2>&1; then
    docker save -o "${BACKUP_DIR}/frontend-image.tar" "${CURRENT_FRONTEND_IMAGE}"
fi

if [ -n "${CURRENT_AUTH_IMAGE:-}" ] && docker image inspect "${CURRENT_AUTH_IMAGE}" >/dev/null 2>&1; then
    docker save -o "${BACKUP_DIR}/auth-server-image.tar" "${CURRENT_AUTH_IMAGE}"
fi

if [ -n "${CURRENT_JIUWENBOX_IMAGE:-}" ] && docker image inspect "${CURRENT_JIUWENBOX_IMAGE}" >/dev/null 2>&1; then
    docker save -o "${BACKUP_DIR}/jiuwenbox-image.tar" "${CURRENT_JIUWENBOX_IMAGE}"
fi

{
    printf 'BACKUP_CREATED_AT=%q\n' "${TIMESTAMP}"
    printf 'FROM_VERSION=%q\n' "${FROM_VERSION}"
    printf 'TARGET_VERSION=%q\n' "${TARGET_VERSION}"
    printf 'CURRENT_BACKEND_IMAGE=%q\n' "${CURRENT_BACKEND_IMAGE:-}"
    printf 'CURRENT_FRONTEND_IMAGE=%q\n' "${CURRENT_FRONTEND_IMAGE:-}"
    printf 'CURRENT_AUTH_IMAGE=%q\n' "${CURRENT_AUTH_IMAGE:-}"
    printf 'CURRENT_JIUWENBOX_IMAGE=%q\n' "${CURRENT_JIUWENBOX_IMAGE:-}"
    printf 'STORAGE_MODE=%q\n' "${RESOLVED_STORAGE_MODE}"
    printf 'RUNTIME_VOLUME_NAME=%q\n' "${ACTIVE_RUNTIME_VOLUME}"
    printf 'AUTH_VOLUME_NAME=%q\n' "${ACTIVE_AUTH_VOLUME}"
    printf 'STORAGE_UTILITY_IMAGE=%q\n' "${RESOLVED_STORAGE_UTILITY_IMAGE:-}"
} > "${BACKUP_DIR}/metadata.env"

if command -v sha256sum >/dev/null 2>&1; then
    (cd "${BACKUP_DIR}" && sha256sum ./*.tar ./*.tar.gz 2>/dev/null || true) > "${BACKUP_DIR}/checksums.sha256"
elif command -v shasum >/dev/null 2>&1; then
    (cd "${BACKUP_DIR}" && shasum -a 256 ./*.tar ./*.tar.gz 2>/dev/null || true) > "${BACKUP_DIR}/checksums.sha256"
fi

printf '%s\n' "${BACKUP_DIR}"
