#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_state

TARGET_VERSION="${1:-$(manifest_get app_version)}"
FROM_VERSION="${2:-${CURRENT_VERSION:-none}}"
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

backup_path_if_present "${BUNDLE_ROOT}/backend/.env" "backend-env.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/backend/config" "backend-config.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/backend/logs" "backend-logs.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/frontend/logs" "frontend-logs.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/.onto-runtime" "onto-runtime.tar.gz"
backup_path_if_present "${BUNDLE_ROOT}/.onto-auth" "onto-auth.tar.gz"

{
    printf '%s\n' \
        '.onto-runtime/users.json' \
        '.onto-runtime/workspaces/' \
        '.onto-runtime/config/' \
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

{
    printf 'BACKUP_CREATED_AT=%q\n' "${TIMESTAMP}"
    printf 'FROM_VERSION=%q\n' "${FROM_VERSION}"
    printf 'TARGET_VERSION=%q\n' "${TARGET_VERSION}"
    printf 'CURRENT_BACKEND_IMAGE=%q\n' "${CURRENT_BACKEND_IMAGE:-}"
    printf 'CURRENT_FRONTEND_IMAGE=%q\n' "${CURRENT_FRONTEND_IMAGE:-}"
    printf 'CURRENT_AUTH_IMAGE=%q\n' "${CURRENT_AUTH_IMAGE:-}"
} > "${BACKUP_DIR}/metadata.env"

if command -v sha256sum >/dev/null 2>&1; then
    (cd "${BACKUP_DIR}" && sha256sum ./*.tar ./*.tar.gz 2>/dev/null || true) > "${BACKUP_DIR}/checksums.sha256"
elif command -v shasum >/dev/null 2>&1; then
    (cd "${BACKUP_DIR}" && shasum -a 256 ./*.tar ./*.tar.gz 2>/dev/null || true) > "${BACKUP_DIR}/checksums.sha256"
fi

printf '%s\n' "${BACKUP_DIR}"
