#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A standalone consumer release is often unpacked into a staging directory and
# invoked directly. Route it through the existing installation's upgrade flow
# before common.sh binds BUNDLE_ROOT to the staging directory.
INSTALL_DIR="${ONTO_AGENT_INSTALL_DIR:-/opt/onto-agent}"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [ -f "${SCRIPT_DIR}/manifest.json" ] && [ "${BUNDLE_ROOT}" != "${INSTALL_DIR}" ]; then
    EXISTING_INSTALL=false
    for marker in \
        "${INSTALL_DIR}/VERSION" \
        "${INSTALL_DIR}/deploy/state/current.env"; do
        if [ -e "${marker}" ]; then
            EXISTING_INSTALL=true
            break
        fi
    done
    if [ "${EXISTING_INSTALL}" = false ]; then
        for marker in "${INSTALL_DIR}/.onto-runtime" "${INSTALL_DIR}/.onto-auth"; do
            if [ -d "${marker}" ] && find "${marker}" -mindepth 1 \( -type f -o -type l \) -print -quit | grep -q .; then
                EXISTING_INSTALL=true
                break
            fi
        done
    fi
    if [ "${EXISTING_INSTALL}" = true ]; then
        [ -f "${SCRIPT_DIR}/upgrade.sh" ] || {
            printf '[deploy][error] Existing installation detected but upgrade.sh is missing\n' >&2
            exit 1
        }
        printf '[deploy] Existing installation detected at %s; dispatching upgrade flow\n' "${INSTALL_DIR}"
        exec bash "${SCRIPT_DIR}/upgrade.sh" "${INSTALL_DIR}"
    fi
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"
chmod +x "${SCRIPT_DIR}/"*.sh

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
assert_no_existing_install_for_install

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
    BACKUP_DIR="$("${SCRIPT_DIR}/backup.sh" --confirm-writes-stopped "${TARGET_VERSION}" "${CURRENT_VERSION}")"
    log "Backup created after writes stopped: ${BACKUP_DIR}"
fi

log "Applying migration after writes stopped"
MIGRATE_APPLY_ARGS=(
    "${CURRENT_VERSION:-}"
    "${TARGET_VERSION}"
    --confirm-writes-stopped
)
if [ -n "${BACKUP_DIR}" ]; then
    MIGRATE_APPLY_ARGS+=(--legacy-backup-dir "${BACKUP_DIR}")
fi
"${SCRIPT_DIR}/migrate.sh" "${MIGRATE_APPLY_ARGS[@]}"

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
