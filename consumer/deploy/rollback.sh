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
FAILED_AUTH_IMAGE="${CURRENT_AUTH_IMAGE:-}"

log "Stopping failed release writes before failsafe backup and restore"
compose_down "${FAILED_BACKEND_IMAGE}" "${FAILED_FRONTEND_IMAGE}" "${FAILED_AUTH_IMAGE}"

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
restore_archive_if_present "${LAST_BACKUP_DIR}/backend-logs.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/frontend-logs.tar.gz"

if [ -f "${LAST_BACKUP_DIR}/onto-runtime.tar.gz" ]; then
    rm -rf "${BUNDLE_ROOT}/.onto-runtime"
fi
if [ -f "${LAST_BACKUP_DIR}/onto-auth.tar.gz" ]; then
    rm -rf "${BUNDLE_ROOT}/.onto-auth"
fi
restore_archive_if_present "${LAST_BACKUP_DIR}/onto-runtime.tar.gz"
restore_archive_if_present "${LAST_BACKUP_DIR}/onto-auth.tar.gz"

ROLLBACK_BACKEND_IMAGE=""
ROLLBACK_FRONTEND_IMAGE=""
ROLLBACK_AUTH_IMAGE=""

if [ -f "${LAST_BACKUP_DIR}/backend-image.tar" ]; then
    docker load -i "${LAST_BACKUP_DIR}/backend-image.tar" >/dev/null
fi

if [ -f "${LAST_BACKUP_DIR}/frontend-image.tar" ]; then
    docker load -i "${LAST_BACKUP_DIR}/frontend-image.tar" >/dev/null
fi

if [ -f "${LAST_BACKUP_DIR}/auth-server-image.tar" ]; then
    docker load -i "${LAST_BACKUP_DIR}/auth-server-image.tar" >/dev/null
fi

if [ -f "${LAST_BACKUP_DIR}/metadata.env" ]; then
    # 旧发布包的 metadata.env 没有 CURRENT_AUTH_IMAGE，先清空避免误用 state 中的失败镜像
    CURRENT_AUTH_IMAGE=""
    # shellcheck disable=SC1090
    source "${LAST_BACKUP_DIR}/metadata.env"
    ROLLBACK_BACKEND_IMAGE="${CURRENT_BACKEND_IMAGE:-${PREVIOUS_BACKEND_IMAGE:-}}"
    ROLLBACK_FRONTEND_IMAGE="${CURRENT_FRONTEND_IMAGE:-${PREVIOUS_FRONTEND_IMAGE:-}}"
    ROLLBACK_AUTH_IMAGE="${CURRENT_AUTH_IMAGE:-${PREVIOUS_AUTH_IMAGE:-}}"
fi

[ -n "${ROLLBACK_BACKEND_IMAGE}" ] || ROLLBACK_BACKEND_IMAGE="${PREVIOUS_BACKEND_IMAGE:-}"
[ -n "${ROLLBACK_FRONTEND_IMAGE}" ] || ROLLBACK_FRONTEND_IMAGE="${PREVIOUS_FRONTEND_IMAGE:-}"
[ -n "${ROLLBACK_AUTH_IMAGE}" ] || ROLLBACK_AUTH_IMAGE="${PREVIOUS_AUTH_IMAGE:-}"
[ -n "${ROLLBACK_BACKEND_IMAGE}" ] || die "Could not determine backend image for rollback"
[ -n "${ROLLBACK_FRONTEND_IMAGE}" ] || die "Could not determine frontend image for rollback"
# auth-server 现为必备服务（backend depends_on service_healthy）：镜像无法确定时
# compose 会回退到从未构建过的 :latest，导致回滚卡死在健康等待。优先复用本机已有的
# 带版本镜像；一个都没有则快速失败并给指引，避免半残态。
if [ -z "${ROLLBACK_AUTH_IMAGE}" ]; then
    ROLLBACK_AUTH_IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' onto-agent-auth-server 2>/dev/null | head -n 1 || true)"
    if [ -n "${ROLLBACK_AUTH_IMAGE}" ]; then
        warn "Rollback metadata lacks auth-server image; reusing local ${ROLLBACK_AUTH_IMAGE}"
    else
        die "Could not determine auth-server image for rollback and no local onto-agent-auth-server image exists; load one (docker load < auth-server-image.tar) then retry"
    fi
fi

log "Rolling back to ${PREVIOUS_VERSION}"
compose_up "${ROLLBACK_BACKEND_IMAGE}" "${ROLLBACK_FRONTEND_IMAGE}" "${ROLLBACK_AUTH_IMAGE}"

wait_for_container "onto-agent-backend" 180 || die "Backend container did not become healthy after rollback"
wait_for_container "onto-agent-mcp" 180 || die "MCP container did not become healthy after rollback"
wait_for_container "onto-agent-frontend" 180 || die "Frontend container did not become healthy after rollback"
wait_for_container "onto-agent-auth-server" 180 || die "Auth server container did not become healthy after rollback"

write_state \
    "${PREVIOUS_VERSION}" \
    "${CURRENT_FAILED_VERSION}" \
    "${FAILSAFE_BACKUP}" \
    "${ROLLBACK_BACKEND_IMAGE}" \
    "${ROLLBACK_FRONTEND_IMAGE}" \
    "${FAILED_BACKEND_IMAGE}" \
    "${FAILED_FRONTEND_IMAGE}" \
    "${ROLLBACK_AUTH_IMAGE}" \
    "${FAILED_AUTH_IMAGE}"

log "Rollback finished successfully"
