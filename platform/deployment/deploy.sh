#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "${SCRIPT_DIR}/"*.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

TARGET_VERSION="$(manifest_get app_version)"
BACKEND_IMAGE="$(manifest_get backend_image)"
FRONTEND_IMAGE="$(manifest_get frontend_image)"
BACKEND_TAR="${BUNDLE_ROOT}/$(manifest_get backend_image_tar)"
FRONTEND_TAR="${BUNDLE_ROOT}/$(manifest_get frontend_image_tar)"

[ -n "${TARGET_VERSION}" ] || die "app_version is missing in manifest.json"
[ -n "${BACKEND_IMAGE}" ] || die "backend_image is missing in manifest.json"
[ -n "${FRONTEND_IMAGE}" ] || die "frontend_image is missing in manifest.json"

"${SCRIPT_DIR}/preflight.sh"
load_state

MODE="install"
if [ -n "${CURRENT_VERSION:-}" ]; then
    MODE="upgrade"
fi

log "Starting ${MODE} for version ${TARGET_VERSION}"

ensure_runtime_layout
ensure_env_files

BACKUP_DIR=""
if [ "${MODE}" = "upgrade" ]; then
    BACKUP_DIR="$("${SCRIPT_DIR}/backup.sh" "${TARGET_VERSION}" "${CURRENT_VERSION}")"
    log "Backup created at ${BACKUP_DIR}"
fi

log "Loading release images"
docker_load_image "${BACKEND_TAR}"
docker_load_image "${FRONTEND_TAR}"

log "Applying migration step"
"${SCRIPT_DIR}/migrate.sh" "${CURRENT_VERSION:-}" "${TARGET_VERSION}"

if [ "${MODE}" = "upgrade" ]; then
    log "Stopping previous release containers"
    compose_down "${CURRENT_BACKEND_IMAGE}" "${CURRENT_FRONTEND_IMAGE}"
fi

log "Starting release containers"
compose_up "${BACKEND_IMAGE}" "${FRONTEND_IMAGE}"

wait_for_container "onto-agent-backend" 180 || die "Backend container did not become healthy"
wait_for_container "onto-agent-frontend" 180 || die "Frontend container did not become healthy"

write_state \
    "${TARGET_VERSION}" \
    "${CURRENT_VERSION:-}" \
    "${BACKUP_DIR}" \
    "${BACKEND_IMAGE}" \
    "${FRONTEND_IMAGE}" \
    "${CURRENT_BACKEND_IMAGE:-}" \
    "${CURRENT_FRONTEND_IMAGE:-}"

log "Deployment finished successfully"
