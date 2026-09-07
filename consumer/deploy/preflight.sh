#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

PREFLIGHT_ARGS=()
if [ "${1:-}" = "--pre-migration" ]; then
    PREFLIGHT_ARGS+=(--pre-migration)
    shift
fi
[ "$#" -eq 0 ] || die "Usage: preflight.sh [--pre-migration]"

require_file "${MANIFEST_FILE}"
require_file "${COMPOSE_FILE}"
require_file "${SCRIPT_DIR}/jiuwenbox-policy.yaml"

command -v docker >/dev/null 2>&1 || die "Docker is required but not installed"
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"
compose_probe_output=""
if ! compose_probe_output="$(docker_compose_cmd 2>&1 >/dev/null)"; then
    if [ -n "${compose_probe_output}" ]; then
        printf '%s\n' "${compose_probe_output}" >&2
    fi
    die "Docker Compose is required but not available (tried 'docker compose' and 'docker-compose')"
fi
[ "$(uname -s)" = "Linux" ] || die "JiuwenBox sandbox deployment requires Linux"

ensure_runtime_layout
ensure_env_files
ensure_mcp_token_secret
load_state

PREFLIGHT_STORAGE_VIEW=""
PREFLIGHT_ENV_FILE="${BUNDLE_ROOT}/backend/.env"
PREFLIGHT_RUNTIME_ROOT="${BUNDLE_ROOT}/.onto-runtime"

cleanup_preflight_storage_view() {
    if [ -n "${PREFLIGHT_STORAGE_VIEW}" ]; then
        case "${PREFLIGHT_STORAGE_VIEW}" in
            "${STATE_DIR}"/preflight-storage.*)
                rm -rf "${PREFLIGHT_STORAGE_VIEW}"
                ;;
            *)
                warn "Refusing to remove unexpected preflight storage view path"
                ;;
        esac
    fi
}
trap cleanup_preflight_storage_view EXIT

prepare_preflight_storage_view() {
    [ "${RESOLVED_STORAGE_MODE}" = "volume" ] || return 0
    if ! docker volume inspect "${ACTIVE_RUNTIME_VOLUME}" >/dev/null 2>&1 \
        || ! docker volume inspect "${ACTIVE_AUTH_VOLUME}" >/dev/null 2>&1; then
        [ -z "${CURRENT_VERSION:-}" ] \
            || die "Installed volume storage is missing; refusing to preflight host shadow directories"
        return 0
    fi
    if [ -z "${STORAGE_UTILITY_IMAGE:-}" ] && [ -n "${CURRENT_BACKEND_IMAGE:-}" ]; then
        STORAGE_UTILITY_IMAGE="${CURRENT_BACKEND_IMAGE}"
        export STORAGE_UTILITY_IMAGE
    fi
    PREFLIGHT_STORAGE_VIEW="$(mktemp -d "${STATE_DIR}/preflight-storage.XXXXXX")" \
        || die "Cannot create restricted preflight storage view"
    chmod 700 "${PREFLIGHT_STORAGE_VIEW}"
    run_storage_utility \
        -v "${ACTIVE_RUNTIME_VOLUME}:/source/runtime:ro" \
        -v "${ACTIVE_AUTH_VOLUME}:/source/auth:ro" \
        -v "${PREFLIGHT_STORAGE_VIEW}:/view:rw" \
        -v "${SCRIPT_DIR}:/tool:ro" \
        -- \
        python /tool/storage_integrity.py export-preflight-view \
        --runtime /source/runtime \
        --auth /source/auth \
        --output /view >/dev/null \
        || die "Could not create the read-only volume preflight view"
    mkdir -p "${PREFLIGHT_STORAGE_VIEW}/backend"
    cp "${BUNDLE_ROOT}/backend/.env" "${PREFLIGHT_STORAGE_VIEW}/backend/.env"
    chmod 600 "${PREFLIGHT_STORAGE_VIEW}/backend/.env"
    PREFLIGHT_ENV_FILE="${PREFLIGHT_STORAGE_VIEW}/backend/.env"
    PREFLIGHT_RUNTIME_ROOT="${PREFLIGHT_STORAGE_VIEW}/.onto-runtime"
}

prepare_preflight_storage_view

command -v python3 >/dev/null 2>&1 || die "Python 3 is required for deployment validation"
build_compose_env_args
PREFLIGHT_COMPOSE_ENV_ARGS=()
for compose_env_file in "${COMPOSE_ENV_FILES[@]}"; do
    if [ "${compose_env_file}" = "${BACKEND_COMPOSE_ENV_FILE}" ]; then
        compose_env_file="${PREFLIGHT_ENV_FILE}"
    fi
    PREFLIGHT_COMPOSE_ENV_ARGS+=(--compose-env-file "${compose_env_file}")
done
python3 "${SCRIPT_DIR}/preflight_config.py" \
    --env-file "${PREFLIGHT_ENV_FILE}" \
    "${PREFLIGHT_COMPOSE_ENV_ARGS[@]}" \
    --runtime-root "${PREFLIGHT_RUNTIME_ROOT}" \
    --compose-file "${COMPOSE_FILE}" \
    --report "${STATE_DIR}/preflight-config.json" \
    "${PREFLIGHT_ARGS[@]}" \
    || die "Auth, MCP, user-limit, or Graph preflight validation failed; inspect ${STATE_DIR}/preflight-config.json"

resolved_host_ports="$(python3 -c '
import json
import sys

ports = json.load(open(sys.argv[1], encoding="utf-8")).get("host_ports", {})
values = [ports.get(name) for name in ("backend", "mcp", "frontend")]
if any(type(value) is not int or value < 1 or value > 65535 for value in values):
    raise SystemExit(1)
print(*values)
' "${STATE_DIR}/preflight-config.json")" \
    || die "Could not resolve release host ports from Compose config"
read -r BACKEND_HOST_PORT MCP_HOST_PORT FRONTEND_HOST_PORT <<< "${resolved_host_ports}"

host_arch="$(detect_host_arch)"
release_arch="$(manifest_get release_arch)"
[ -n "${release_arch}" ] || die "release_arch is missing in manifest.json"

if [ "${host_arch}" != "${release_arch}" ]; then
    die "Release arch '${release_arch}' does not match host arch '${host_arch}'"
fi

backend_tar="$(manifest_get backend_image_tar)"
frontend_tar="$(manifest_get frontend_image_tar)"
jiuwenbox_tar="$(manifest_get jiuwenbox_image_tar)"

[ -n "${backend_tar}" ] || die "backend_image_tar is missing in manifest.json"
[ -n "${frontend_tar}" ] || die "frontend_image_tar is missing in manifest.json"
[ -n "${jiuwenbox_tar}" ] || die "jiuwenbox_image_tar is missing in manifest.json"

require_file "${BUNDLE_ROOT}/${backend_tar}"
require_file "${BUNDLE_ROOT}/${frontend_tar}"
require_file "${BUNDLE_ROOT}/${jiuwenbox_tar}"

check_port_available "${BACKEND_HOST_PORT}"
check_port_available "${MCP_HOST_PORT}"
check_port_available "${FRONTEND_HOST_PORT}"

# auth-server 附加检查（必备组件，不发布宿主端口，无需查端口占用）
auth_tar="$(manifest_get auth_server_image_tar)"
[ -n "${auth_tar}" ] || die "auth_server_image_tar is missing in manifest.json"
require_file "${BUNDLE_ROOT}/${auth_tar}"

auth_health="$(docker inspect --format '{{.State.Health.Status}}' onto-agent-auth-server 2>/dev/null || true)"
case "${auth_health}" in
    "" | "<no value>")
        log "auth-server 容器尚未创建（首次部署属正常，部署后会自动拉起）"
        ;;
    healthy)
        log "auth-server 容器健康状态: healthy"
        ;;
    *)
        warn "auth-server 容器健康状态: ${auth_health}（部署会重建该容器）"
        ;;
esac

mcp_health="$(docker inspect --format '{{.State.Health.Status}}' onto-agent-mcp 2>/dev/null || true)"
case "${mcp_health}" in
    "" | "<no value>")
        log "MCP 容器尚未创建（首次部署属正常，部署后监听 7488）"
        ;;
    healthy)
        curl -fsS --max-time 3 "http://127.0.0.1:${MCP_HOST_PORT}/health" >/dev/null \
            || die "MCP container is healthy but host health endpoint is unreachable"
        log "MCP ${MCP_HOST_PORT} health verified"
        ;;
    *)
        warn "MCP 容器健康状态: ${mcp_health}（部署会重建该容器）"
        ;;
esac

log "Preflight checks passed"
