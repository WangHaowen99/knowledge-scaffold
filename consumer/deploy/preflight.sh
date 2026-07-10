#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

require_file "${MANIFEST_FILE}"
require_file "${COMPOSE_FILE}"

command -v docker >/dev/null 2>&1 || die "Docker is required but not installed"
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"
docker_compose_cmd >/dev/null 2>&1 || die "Docker Compose is required but not available"

ensure_runtime_layout
ensure_env_files

host_arch="$(detect_host_arch)"
release_arch="$(manifest_get release_arch)"
[ -n "${release_arch}" ] || die "release_arch is missing in manifest.json"

if [ "${host_arch}" != "${release_arch}" ]; then
    die "Release arch '${release_arch}' does not match host arch '${host_arch}'"
fi

backend_tar="$(manifest_get backend_image_tar)"
frontend_tar="$(manifest_get frontend_image_tar)"

[ -n "${backend_tar}" ] || die "backend_image_tar is missing in manifest.json"
[ -n "${frontend_tar}" ] || die "frontend_image_tar is missing in manifest.json"

require_file "${BUNDLE_ROOT}/${backend_tar}"
require_file "${BUNDLE_ROOT}/${frontend_tar}"

check_port_available "${BACKEND_PORT:-5055}"
check_port_available "${MCP_SERVER_PORT:-7488}"
check_port_available "${FRONTEND_PORT:-8888}"

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

log "Preflight checks passed"
