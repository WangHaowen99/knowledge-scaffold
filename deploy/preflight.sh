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
check_port_available "${FRONTEND_PORT:-8888}"

log "Preflight checks passed"
