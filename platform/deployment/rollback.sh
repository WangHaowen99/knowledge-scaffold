#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "${SCRIPT_DIR}/"*.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

load_state

[ -n "${PREVIOUS_VERSION:-}" ] || die "No previous version recorded for rollback"
[ -n "${LAST_BACKUP_DIR:-}" ] || die "No backup directory recorded for rollback"
[ -d "${LAST_BACKUP_DIR}" ] || die "Recorded backup directory does not exist: ${LAST_BACKUP_DIR}"

CURRENT_FAILED_VERSION="${CURRENT_VERSION:-unknown}"
FAILED_BACKEND_IMAGE="${CURRENT_BACKEND_IMAGE:-}"
FAILED_FRONTEND_IMAGE="${CURRENT_FRONTEND_IMAGE:-}"
FAILSAFE_BACKUP="$("${SCRIPT_DIR}/backup.sh" "rollback-${PREVIOUS_VERSION}" "${CURRENT_FAILED_VERSION}")"
log "Captured current state before rollback at ${FAILSAFE_BACKUP}"

restore_archive_if_present() {
    local archive_path="$1"
    if [ -f "${archive_path}" ]; then
        tar -C "${BUNDLE_ROOT}" -xzf "${archive_path}"
    fi
}

restore_archive_if_present "${LAST_BACKUP_DIR}/backend-env.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/frontend-env.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/backend-data.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/backend-logs.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/frontend-logs.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/onto-runtime.tar.gz"

ROLLBACK_BACKEND_IMAGE=""
ROLLBACK_FRONTEND_IMAGE=""

if [ -f "${LAST_BACKUP_DIR}/backend-image.tar" ]; then
    docker load -i "${LAST_BACKUP_DIR}/backend-image.tar" >/dev/null
fi

if [ -f "${LAST_BACKUP_DIR}/frontend-image.tar" ]; then
    docker load -i "${LAST_BACKUP_DIR}/frontend-image.tar" >/dev/null
fi

if [ -f "${LAST_BACKUP_DIR}/metadata.env" ]; then
    # shellcheck disable=SC1090
    source "${LAST_BACKUP_DIR}/metadata.env"
    ROLLBACK_BACKEND_IMAGE="${CURRENT_BACKEND_IMAGE:-${PREVIOUS_BACKEND_IMAGE:-}}"
    ROLLBACK_FRONTEND_IMAGE="${CURRENT_FRONTEND_IMAGE:-${PREVIOUS_FRONTEND_IMAGE:-}}"
fi

[ -n "${ROLLBACK_BACKEND_IMAGE}" ] || ROLLBACK_BACKEND_IMAGE="${PREVIOUS_BACKEND_IMAGE:-}"
[ -n "${ROLLBACK_FRONTEND_IMAGE}" ] || ROLLBACK_FRONTEND_IMAGE="${PREVIOUS_FRONTEND_IMAGE:-}"
[ -n "${ROLLBACK_BACKEND_IMAGE}" ] || die "Could not determine backend image for rollback"
[ -n "${ROLLBACK_FRONTEND_IMAGE}" ] || die "Could not determine frontend image for rollback"

log "Rolling back to ${PREVIOUS_VERSION}"
compose_up "${ROLLBACK_BACKEND_IMAGE}" "${ROLLBACK_FRONTEND_IMAGE}"

wait_for_container "onto-agent-backend" 180 || die "Backend container did not become healthy after rollback"
wait_for_container "onto-agent-frontend" 180 || die "Frontend container did not become healthy after rollback"

write_state \
    "${PREVIOUS_VERSION}" \
    "${CURRENT_FAILED_VERSION}" \
    "${FAILSAFE_BACKUP}" \
    "${ROLLBACK_BACKEND_IMAGE}" \
    "${ROLLBACK_FRONTEND_IMAGE}" \
    "${FAILED_BACKEND_IMAGE}" \
    "${FAILED_FRONTEND_IMAGE}"

log "Rollback finished successfully"
