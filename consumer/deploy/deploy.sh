#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "${SCRIPT_DIR}/"*.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

TARGET_VERSION="$(manifest_get app_version)"
BACKEND_IMAGE="$(manifest_get backend_image)"
FRONTEND_IMAGE="$(manifest_get frontend_image)"
AUTH_IMAGE="$(manifest_get auth_server_image)"
JIUWENBOX_IMAGE="$(manifest_get jiuwenbox_image)"
BACKEND_TAR="${BUNDLE_ROOT}/$(manifest_get backend_image_tar)"
FRONTEND_TAR="${BUNDLE_ROOT}/$(manifest_get frontend_image_tar)"
AUTH_TAR_REL="$(manifest_get auth_server_image_tar)"
JIUWENBOX_TAR_REL="$(manifest_get jiuwenbox_image_tar)"

[ -n "${TARGET_VERSION}" ] || die "app_version is missing in manifest.json"
[ -n "${BACKEND_IMAGE}" ] || die "backend_image is missing in manifest.json"
[ -n "${FRONTEND_IMAGE}" ] || die "frontend_image is missing in manifest.json"
[ -n "${AUTH_IMAGE}" ] || die "auth_server_image is missing in manifest.json"
[ -n "${JIUWENBOX_IMAGE}" ] || die "jiuwenbox_image is missing in manifest.json"

"${SCRIPT_DIR}/preflight.sh" --pre-migration
load_state

MODE="install"
if [ -n "${CURRENT_VERSION:-}" ]; then
    MODE="upgrade"
fi

log "Starting ${MODE} for version ${TARGET_VERSION}"

ensure_runtime_layout
ensure_env_files

log "Loading release images"
docker_load_image "${BACKEND_TAR}"
docker_load_image "${FRONTEND_TAR}"
# auth-server 为必备组件，镜像必须随包分发并导入
[ -n "${AUTH_TAR_REL}" ] || die "auth_server_image_tar is missing in manifest.json"
docker_load_image "${BUNDLE_ROOT}/${AUTH_TAR_REL}"
[ -n "${JIUWENBOX_TAR_REL}" ] || die "jiuwenbox_image_tar is missing in manifest.json"
docker_load_image "${BUNDLE_ROOT}/${JIUWENBOX_TAR_REL}"

log "Validating migration plan without changing data"
"${SCRIPT_DIR}/migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}" --dry-run

if [ "${MODE}" = "upgrade" ]; then
    log "Stopping previous release writes before backup and migration"
    compose_stop "${CURRENT_BACKEND_IMAGE}" "${CURRENT_FRONTEND_IMAGE}" "${CURRENT_AUTH_IMAGE:-}" "${CURRENT_JIUWENBOX_IMAGE:-${JIUWENBOX_IMAGE}}"
fi

BACKUP_DIR=""
if [ "${MODE}" = "upgrade" ]; then
    BACKUP_DIR="$("${SCRIPT_DIR}/backup.sh" "${TARGET_VERSION}" "${CURRENT_VERSION}")"
    log "Backup created after writes stopped: ${BACKUP_DIR}"
fi

log "Applying migration after writes stopped"
"${SCRIPT_DIR}/migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}" --confirm-writes-stopped \
    ${BACKUP_DIR:+"--legacy-backup-dir"} ${BACKUP_DIR:+"${BACKUP_DIR}"}

log "Validating canonical configuration after migration"
"${SCRIPT_DIR}/preflight.sh"

if [ "${MODE}" = "upgrade" ]; then
    log "Removing stopped release containers before restart"
    compose_down "${CURRENT_BACKEND_IMAGE}" "${CURRENT_FRONTEND_IMAGE}" "${CURRENT_AUTH_IMAGE:-}" "${CURRENT_JIUWENBOX_IMAGE:-${JIUWENBOX_IMAGE}}"
fi

log "Starting release containers"
compose_up "${BACKEND_IMAGE}" "${FRONTEND_IMAGE}" "${AUTH_IMAGE}" "${JIUWENBOX_IMAGE}"

wait_for_container "onto-agent-backend" 180 || die "Backend container did not become healthy"
wait_for_container "onto-agent-mcp" 180 || die "MCP container did not become healthy"
wait_for_container "onto-agent-frontend" 180 || die "Frontend container did not become healthy"
wait_for_container "onto-agent-auth-server" 180 || die "Auth server container did not become healthy"
wait_for_container "onto-agent-jiuwenbox" 180 || die "JiuwenBox container did not become healthy"

write_state \
    "${TARGET_VERSION}" \
    "${CURRENT_VERSION:-}" \
    "${BACKUP_DIR}" \
    "${BACKEND_IMAGE}" \
    "${FRONTEND_IMAGE}" \
    "${CURRENT_BACKEND_IMAGE:-}" \
    "${CURRENT_FRONTEND_IMAGE:-}" \
    "${AUTH_IMAGE}" \
    "${CURRENT_AUTH_IMAGE:-}" \
    "${JIUWENBOX_IMAGE}" \
    "${CURRENT_JIUWENBOX_IMAGE:-}"

log "Deployment finished successfully"
