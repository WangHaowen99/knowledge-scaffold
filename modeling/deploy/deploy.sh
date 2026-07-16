#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
    printf '[modeling-deploy] %s\n' "$*"
}

warn() {
    printf '[modeling-deploy][warn] %s\n' "$*" >&2
}

die() {
    printf '[modeling-deploy][error] %s\n' "$*" >&2
    exit 1
}

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        die "Docker Compose is not available"
    fi
}

manifest_get() {
    local manifest="$1"
    local key="$2"
    [ -f "${manifest}" ] || die "Required manifest not found: ${manifest}"
    sed -n 's/^[[:space:]]*"'${key}'":[[:space:]]*"\([^"]*\)".*$/\1/p' "${manifest}" | head -n 1
}

detect_host_arch() {
    case "$(uname -m)" in
        x86_64|amd64)
            printf 'amd64\n'
            ;;
        aarch64|arm64)
            printf 'arm64\n'
            ;;
        *)
            uname -m
            ;;
    esac
}

container_network() {
    local container="$1"
    docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "${container}" 2>/dev/null | head -n 1
}

detect_consumer_network() {
    local container=""
    local network=""

    if [ -n "${CONSUMER_NETWORK:-}" ]; then
        printf '%s\n' "${CONSUMER_NETWORK}"
        return 0
    fi

    if docker inspect onto-agent-backend >/dev/null 2>&1; then
        container="onto-agent-backend"
    else
        container="$(docker ps --format '{{.Names}}' | grep -E '^onto-agent-backend($|-)' | head -n 1 || true)"
    fi

    if [ -n "${container}" ]; then
        network="$(container_network "${container}")"
        if [ -n "${network}" ]; then
            printf '%s\n' "${network}"
            return 0
        fi
    fi

    docker network ls --format '{{.Name}}' | grep -E '(^|_|-)onto-agent-network$|^onto-agent_default$' | head -n 1 || true
}

container_health() {
    local container_name="$1"
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_name}" 2>/dev/null || true
}

wait_for_container() {
    local container_name="$1"
    local timeout_seconds="${2:-120}"
    local elapsed=0
    local status=""

    while [ "${elapsed}" -lt "${timeout_seconds}" ]; do
        status="$(container_health "${container_name}")"
        case "${status}" in
            healthy|running)
                return 0
                ;;
            unhealthy|exited|dead)
                return 1
                ;;
        esac
        sleep 2
        elapsed=$((elapsed + 2))
    done

    return 1
}

deploy_release() {
    local manifest_file="$1"
    local bundle_root="$2"
    local compose_file="$3"
    local release_arch
    local host_arch
    local image
    local image_tar
    local network

    release_arch="$(manifest_get "${manifest_file}" release_arch)"
    host_arch="$(detect_host_arch)"
    [ -n "${release_arch}" ] || die "release_arch is missing in manifest.json"
    [ "${host_arch}" = "${release_arch}" ] || die "Release arch '${release_arch}' does not match host arch '${host_arch}'"

    image="$(manifest_get "${manifest_file}" modeling_image)"
    image_tar="$(manifest_get "${manifest_file}" modeling_image_tar)"
    [ -n "${image}" ] || die "modeling_image is missing in manifest.json"
    [ -n "${image_tar}" ] || die "modeling_image_tar is missing in manifest.json"
    [ -f "${bundle_root}/${image_tar}" ] || die "Modeling image tar not found: ${bundle_root}/${image_tar}"

    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die "Could not detect consumer Docker network; deploy consumer first or set CONSUMER_NETWORK"

    log "Loading modeling image ${image}"
    docker load -i "${bundle_root}/${image_tar}" >/dev/null

    log "Starting modeling on consumer network ${network}"
    MODELING_IMAGE="${image}" CONSUMER_NETWORK="${network}" compose \
        -p mw-modeling \
        -f "${compose_file}" \
        --project-directory "${bundle_root}" \
        up -d

    wait_for_container "mw-modeling" 180 || die "Modeling container did not become healthy"
    log "Modeling deployment finished successfully"
}

deploy_source() {
    local repo_root
    local compose_file
    local network

    repo_root="$(cd "${SCRIPT_DIR}/../.." && pwd)"
    compose_file="${repo_root}/deploy/docker-compose.modeling.yml"
    [ -f "${compose_file}" ] || die "Source compose file not found: ${compose_file}"

    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die "Could not detect consumer Docker network; deploy consumer first or set CONSUMER_NETWORK"

    log "Starting modeling from source on consumer network ${network}"
    CONSUMER_NETWORK="${network}" compose \
        -p mw-modeling \
        -f "${compose_file}" \
        --project-directory "${repo_root}/deploy" \
        up -d --build

    wait_for_container "mw-modeling" 180 || die "Modeling container did not become healthy"
    log "Modeling deployment finished successfully"
}

if [ "$#" -gt 0 ]; then
    die "modeling/deploy/deploy.sh does not accept arguments"
fi

command -v docker >/dev/null 2>&1 || die "Docker is required"
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"

if [ -f "${SCRIPT_DIR}/manifest.json" ]; then
    deploy_release "${SCRIPT_DIR}/manifest.json" "$(cd "${SCRIPT_DIR}/.." && pwd)" "${SCRIPT_DIR}/docker-compose.release.yml"
elif [ -f "${SCRIPT_DIR}/../manifest.json" ]; then
    deploy_release "${SCRIPT_DIR}/../manifest.json" "$(cd "${SCRIPT_DIR}/../.." && pwd)" "${SCRIPT_DIR}/docker-compose.release.yml"
else
    warn "No release manifest found; using source deployment mode"
    deploy_source
fi
