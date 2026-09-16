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

env_get() {
    local file="$1"
    local key="$2"
    local value
    value="$([ -f "${file}" ] && grep -E "^${key}=" "${file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

load_env_if_unset() {
    local file="$1"
    local key="$2"
    local value
    [ -z "${!key:-}" ] || return 0
    value="$(env_get "${file}" "${key}")"
    [ -z "${value}" ] || { printf -v "${key}" '%s' "${value}"; export "${key}"; }
}

ensure_modeling_service_token() {
    local env_file="$1"
    local secret="${MODELING_SERVICE_TOKEN:-$(env_get "${env_file}" MODELING_SERVICE_TOKEN)}"
    local temp_file
    [ -f "${env_file}" ] || die "Modeling service token env file not found: ${env_file}"
    if [ -z "${secret}" ]; then
        if command -v openssl >/dev/null 2>&1; then
            secret="$(openssl rand -hex 32)"
        elif command -v python3 >/dev/null 2>&1; then
            secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        else
            die "Cannot generate MODELING_SERVICE_TOKEN: openssl or Python 3 is required"
        fi
    fi
    [ "${#secret}" -ge 32 ] || die "MODELING_SERVICE_TOKEN must contain at least 32 characters"
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "Cannot update ${env_file}"
    chmod 600 "${temp_file}"
    if ! awk -v secret="${secret}" '
        BEGIN { updated = 0 }
        /^MODELING_SERVICE_TOKEN=/ {
            if (!updated) { print "MODELING_SERVICE_TOKEN=" secret; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print "MODELING_SERVICE_TOKEN=" secret }
    ' "${env_file}" > "${temp_file}"; then
        command rm -f -- "${temp_file}"
        die "Cannot update ${env_file}"
    fi
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
    MODELING_SERVICE_TOKEN="${secret}"
    export MODELING_SERVICE_TOKEN
}

load_modeling_env() {
    local file
    local key
    for file in "$@"; do
        for key in MODELING_LLM_BASE_URL MODELING_LLM_MODEL MODELING_LLM_API_KEY \
                   MODELING_LLM_MAX_TOKENS MODELING_LLM_CONTEXT_WINDOW \
                   MODELING_AGENT_LLM_MAX_TOKENS MODELING_AGENT_MAX_ITERATIONS \
                   MODELING_AGENT_PIP_INDEX_URL MODELING_AGENT_PIP_TIMEOUT \
                   MODELING_AGENT_PIP_RETRIES MODELING_SERVICE_TOKEN \
                   MODELING_ACCEPTANCE_MODE \
                   MODELING_CONTEXT_WINDOW_MESSAGES MODELING_CONTEXT_MAX_MESSAGES \
                   MODELING_CONTEXT_WINDOW_ROUNDS MODELING_CONTEXT_PROCESSOR_PRESET \
                   MODELING_CONTEXT_WINDOW_TOKENS MODELING_CONTEXT_PROTECTED_TOOLS; do
            load_env_if_unset "${file}" "${key}"
        done
    done
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

consumer_container() {
    if docker inspect onto-agent-backend >/dev/null 2>&1; then
        printf 'onto-agent-backend\n'
        return 0
    fi
    docker ps --format '{{.Names}}' | grep -E '^onto-agent-backend($|-)|^backend$' | head -n 1 || true
}

verify_consumer_service_token() {
    local container configured
    container="$(consumer_container)"
    [ -n "${container}" ] || return 0
    configured="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${container}" 2>/dev/null \
        | sed -n 's/^MODELING_SERVICE_TOKEN=//p' | tail -n 1)"
    if [ -z "${configured}" ] || [ "${configured}" != "${MODELING_SERVICE_TOKEN}" ]; then
        die "Running consumer does not use the configured MODELING_SERVICE_TOKEN; redeploy or restart consumer before deploying Modeling"
    fi
}

detect_consumer_network() {
    local container=""
    local network=""

    if [ -n "${CONSUMER_NETWORK:-}" ]; then
        printf '%s\n' "${CONSUMER_NETWORK}"
        return 0
    fi

    container="$(consumer_container)"

    if [ -n "${container}" ]; then
        network="$(container_network "${container}")"
        if [ -n "${network}" ]; then
            printf '%s\n' "${network}"
            return 0
        fi
    fi

    docker network ls --format '{{.Name}}' | grep -E '(^|_|-)onto-agent-network$|^onto-agent_default$' | head -n 1 || true
}

detect_jiuwenbox_socket_volume() {
    local container
    local volume
    container="$(consumer_container)"
    if [ -n "${container}" ]; then
        volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/run/jiuwenbox"}}{{println .Name}}{{end}}{{end}}' \
            "${container}" 2>/dev/null | head -n 1 || true)"
        [ -z "${volume}" ] || { printf '%s\n' "${volume}"; return 0; }
    fi
    docker volume ls --format '{{.Name}}' | grep -E '(^|_)onto-jiuwenbox-sock$' | head -n 1 || true
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

wait_for_http() {
    local url="$1"
    local timeout_seconds="${2:-60}"
    local elapsed=0
    while [ "${elapsed}" -lt "${timeout_seconds}" ]; do
        if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
            return 0
        fi
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
    local agent_image
    local agent_image_tar
    local network
    local jiuwenbox_volume

    release_arch="$(manifest_get "${manifest_file}" release_arch)"
    host_arch="$(detect_host_arch)"
    [ -n "${release_arch}" ] || die "release_arch is missing in manifest.json"
    [ "${host_arch}" = "${release_arch}" ] || die "Release arch '${release_arch}' does not match host arch '${host_arch}'"

    image="$(manifest_get "${manifest_file}" modeling_image)"
    image_tar="$(manifest_get "${manifest_file}" modeling_image_tar)"
    agent_image="$(manifest_get "${manifest_file}" modeling_agent_image)"
    agent_image_tar="$(manifest_get "${manifest_file}" modeling_agent_image_tar)"
    [ -n "${image}" ] || die "modeling_image is missing in manifest.json"
    [ -n "${image_tar}" ] || die "modeling_image_tar is missing in manifest.json"
    [ -n "${agent_image}" ] || die "modeling_agent_image is missing in manifest.json"
    [ -n "${agent_image_tar}" ] || die "modeling_agent_image_tar is missing in manifest.json"
    [ -f "${bundle_root}/${image_tar}" ] || die "Modeling image tar not found: ${bundle_root}/${image_tar}"
    [ -f "${bundle_root}/${agent_image_tar}" ] || die "Agent image tar not found: ${bundle_root}/${agent_image_tar}"

    ensure_modeling_service_token "${bundle_root}/backend/.env"
    load_modeling_env "${bundle_root}/backend/.env" "${bundle_root}/.env"
    verify_consumer_service_token

    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die "Could not detect consumer Docker network; deploy consumer first or set CONSUMER_NETWORK"
    jiuwenbox_volume="${JIUWENBOX_SOCKET_VOLUME:-$(detect_jiuwenbox_socket_volume)}"
    [ -n "${jiuwenbox_volume}" ] || die "Could not detect JiuwenBox socket volume; deploy consumer sandbox first or set JIUWENBOX_SOCKET_VOLUME"

    log "Loading modeling image ${image}"
    docker load -i "${bundle_root}/${image_tar}" >/dev/null
    log "Loading modeling Agent image ${agent_image}"
    docker load -i "${bundle_root}/${agent_image_tar}" >/dev/null

    log "Starting modeling on consumer network ${network}"
    MODELING_IMAGE="${image}" MODELING_AGENT_IMAGE="${agent_image}" CONSUMER_NETWORK="${network}" \
        JIUWENBOX_SOCKET_VOLUME="${jiuwenbox_volume}" compose \
        -p mw-modeling \
        -f "${compose_file}" \
        --project-directory "${bundle_root}" \
        up -d --remove-orphans

    wait_for_container "mw-modeling-agent" 180 || die "Modeling Agent container did not become healthy"
    wait_for_container "mw-modeling" 180 || die "Modeling container did not become healthy"
    wait_for_http "http://127.0.0.1:${MODELING_PORT:-5100}/workbench.html" 60 \
        || die "Modeling Workbench page did not become reachable"
    log "Modeling Workbench deployment finished: http://localhost:${MODELING_PORT:-5100}/workbench.html#/create"
}

deploy_source() {
    local repo_root
    local compose_file
    local network
    local jiuwenbox_volume

    repo_root="$(cd "${SCRIPT_DIR}/../.." && pwd)"
    compose_file="${repo_root}/deploy/docker-compose.modeling.yml"
    [ -f "${compose_file}" ] || die "Source compose file not found: ${compose_file}"

    ensure_modeling_service_token "${repo_root}/consumer/backend/.env"
    load_modeling_env "${repo_root}/consumer/backend/.env" "${repo_root}/deploy/.env"
    verify_consumer_service_token

    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die "Could not detect consumer Docker network; deploy consumer first or set CONSUMER_NETWORK"
    jiuwenbox_volume="${JIUWENBOX_SOCKET_VOLUME:-$(detect_jiuwenbox_socket_volume)}"
    [ -n "${jiuwenbox_volume}" ] || die "Could not detect JiuwenBox socket volume; deploy consumer sandbox first or set JIUWENBOX_SOCKET_VOLUME"

    log "Starting modeling from source on consumer network ${network}"
    CONSUMER_NETWORK="${network}" JIUWENBOX_SOCKET_VOLUME="${jiuwenbox_volume}" compose \
        -p mw-modeling \
        -f "${compose_file}" \
        --project-directory "${repo_root}/deploy" \
        up -d --build --remove-orphans

    wait_for_container "mw-modeling-agent" 180 || die "Modeling Agent container did not become healthy"
    wait_for_container "mw-modeling" 180 || die "Modeling container did not become healthy"
    wait_for_http "http://127.0.0.1:${MODELING_PORT:-5100}/workbench.html" 60 \
        || die "Modeling Workbench page did not become reachable"
    log "Modeling Workbench deployment finished: http://localhost:${MODELING_PORT:-5100}/workbench.html#/create"
}

if [ "$#" -gt 0 ]; then
    die "modeling/deploy/deploy.sh does not accept arguments"
fi

command -v docker >/dev/null 2>&1 || die "Docker is required"
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"
command -v curl >/dev/null 2>&1 || die "curl is required for Workbench health checks"

if [ -f "${SCRIPT_DIR}/manifest.json" ]; then
    deploy_release "${SCRIPT_DIR}/manifest.json" "$(cd "${SCRIPT_DIR}/.." && pwd)" "${SCRIPT_DIR}/docker-compose.release.yml"
elif [ -f "${SCRIPT_DIR}/../manifest.json" ]; then
    deploy_release "${SCRIPT_DIR}/../manifest.json" "$(cd "${SCRIPT_DIR}/../.." && pwd)" "${SCRIPT_DIR}/docker-compose.release.yml"
else
    warn "No release manifest found; using source deployment mode"
    deploy_source
fi
