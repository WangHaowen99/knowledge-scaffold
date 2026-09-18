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
    local compose_bin=""
    local bundled_compose=false
    local daemon_api_version

    if [ -f "${SCRIPT_DIR}/manifest.json" ]; then
        [ -x "${SCRIPT_DIR}/bin/docker-compose" ] \
            || die "Bundled Docker Compose is missing: ${SCRIPT_DIR}/bin/docker-compose"
        compose_bin="${SCRIPT_DIR}/bin/docker-compose"
        bundled_compose=true
    elif [ -x "${SCRIPT_DIR}/../bin/docker-compose" ]; then
        compose_bin="${SCRIPT_DIR}/../bin/docker-compose"
        bundled_compose=true
    elif docker compose version >/dev/null 2>&1; then
        compose_bin="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        compose_bin="docker-compose"
    else
        die "Docker Compose is not available"
    fi

    if [ "${bundled_compose}" = true ]; then
        daemon_api_version="$(docker version --format '{{.Server.APIVersion}}' 2>/dev/null || true)"
        case "${daemon_api_version}" in
            ''|*[!0-9.]*|.*|*.)
                die "Could not determine a valid Docker daemon API version for bundled Compose"
                ;;
        esac
        DOCKER_API_VERSION="${daemon_api_version}" "${compose_bin}" "$@"
    elif [ "${compose_bin}" = "docker compose" ]; then
        docker compose "$@"
    else
        docker-compose "$@"
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
    value="$(printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//")"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "${value}"
}

load_env_if_unset() {
    local file="$1"
    local key="$2"
    local value
    [ -z "${!key:-}" ] || return 0
    value="$(env_get "${file}" "${key}")"
    [ -z "${value}" ] || { printf -v "${key}" '%s' "${value}"; export "${key}"; }
}

generate_hex_secret() {
    local secret
    command -v od >/dev/null 2>&1 || die "od is required to generate Modeling secrets"
    secret="$(LC_ALL=C od -An -N 32 -tx1 /dev/urandom | tr -d '[:space:]')" \
        || die "Could not generate a Modeling secret"
    case "${secret}" in
        ''|*[!0-9a-f]*) die "Operating system random source returned an invalid secret" ;;
    esac
    [ "${#secret}" -eq 64 ] || die "Operating system random source returned an invalid secret"
    printf '%s' "${secret}"
}

write_env_secret() {
    local env_file="$1"
    local key="$2"
    local secret="$3"
    local temp_file
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "Cannot update ${env_file}"
    chmod 600 "${temp_file}"
    if ! awk -v key="${key}" -v secret="${secret}" '
        BEGIN { updated = 0 }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            if (!updated) { print key "=" secret; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print key "=" secret }
    ' "${env_file}" > "${temp_file}"; then
        command rm -f -- "${temp_file}"
        die "Cannot update ${env_file}"
    fi
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
}

require_modeling_service_token() {
    local env_file="$1"
    local secret="${MODELING_SERVICE_TOKEN:-$(env_get "${env_file}" MODELING_SERVICE_TOKEN)}"
    [ -f "${env_file}" ] || die "Modeling service token env file not found: ${env_file}"
    [ -n "${secret}" ] || die \
        "Set MODELING_SERVICE_TOKEN in ${env_file}; it must match the Consumer value"
    [ "${#secret}" -ge 32 ] || die "MODELING_SERVICE_TOKEN must contain at least 32 characters"
    chmod 600 "${env_file}"
    MODELING_SERVICE_TOKEN="${secret}"
    export MODELING_SERVICE_TOKEN
}

ensure_source_modeling_service_token() {
    local env_file="$1"
    local secret="${MODELING_SERVICE_TOKEN:-$(env_get "${env_file}" MODELING_SERVICE_TOKEN)}"
    [ -f "${env_file}" ] || die "Modeling service token env file not found: ${env_file}"
    if [ -z "${secret}" ]; then
        secret="$(generate_hex_secret)"
        write_env_secret "${env_file}" MODELING_SERVICE_TOKEN "${secret}"
        log "MODELING_SERVICE_TOKEN was generated for source deployment (value is not printed)"
    fi
    [ "${#secret}" -ge 32 ] || die "MODELING_SERVICE_TOKEN must contain at least 32 characters"
    MODELING_SERVICE_TOKEN="${secret}"
    export MODELING_SERVICE_TOKEN
}

ensure_modeling_mcp_token_secret() {
    local env_file="$1"
    local secret="${MODELING_MCP_TOKEN_SECRET:-$(env_get "${env_file}" MODELING_MCP_TOKEN_SECRET)}"
    if [ -z "${secret}" ]; then
        secret="$(generate_hex_secret)"
        write_env_secret "${env_file}" MODELING_MCP_TOKEN_SECRET "${secret}"
        log "MODELING_MCP_TOKEN_SECRET was generated and saved (value is not printed)"
    fi
    [ "${#secret}" -ge 32 ] || die "MODELING_MCP_TOKEN_SECRET must contain at least 32 characters"
    MODELING_MCP_TOKEN_SECRET="${secret}"
    export MODELING_MCP_TOKEN_SECRET
}

load_modeling_env() {
    local file
    local key
    for file in "$@"; do
        for key in MODELING_LLM_BASE_URL MODELING_LLM_MODEL MODELING_LLM_API_KEY \
                   MODELING_LLM_MAX_TOKENS MODELING_LLM_CONTEXT_WINDOW \
                   MODELING_AGENT_LLM_MAX_TOKENS MODELING_AGENT_MAX_ITERATIONS \
                   MODELING_LLM_THINKING_MODE MODELING_REASONING_HEADROOM_TOKENS \
                   MODELING_LEADER_OUTPUT_TOKENS \
                   MODELING_AGENT_PIP_INDEX_URL MODELING_AGENT_PIP_TIMEOUT \
                   MODELING_AGENT_PIP_RETRIES MODELING_SERVICE_TOKEN \
                   MODELING_MCP_TOKEN_SECRET MODELING_PORT MODELING_MCP_PORT \
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

version_ge() {
    local actual="$1"
    local required="$2"
    local actual_part
    local required_part
    local index=1
    actual="$(printf '%s' "${actual}" | sed 's/[^0-9.].*$//')"
    required="$(printf '%s' "${required}" | sed 's/[^0-9.].*$//')"
    while [ "${index}" -le 3 ]; do
        actual_part="${actual%%.*}"
        required_part="${required%%.*}"
        [ "${actual}" = "${actual_part}" ] && actual="" || actual="${actual#*.}"
        [ "${required}" = "${required_part}" ] && required="" || required="${required#*.}"
        actual_part="${actual_part:-0}"
        required_part="${required_part:-0}"
        case "${actual_part}:${required_part}" in *[!0-9:]*) return 1 ;; esac
        [ "${actual_part}" -gt "${required_part}" ] && return 0
        [ "${actual_part}" -lt "${required_part}" ] && return 1
        index=$((index + 1))
    done
    return 0
}

container_network() {
    local container="$1"
    docker container inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        "${container}" 2>/dev/null | head -n 1
}

consumer_container() {
    if docker container inspect onto-agent-backend >/dev/null 2>&1; then
        printf 'onto-agent-backend\n'
        return 0
    fi
    docker ps --format '{{.Names}}' | grep -E '^onto-agent-backend($|-)|^backend$' | head -n 1 || true
}

verify_consumer_service_token() {
    local container configured
    container="$(consumer_container)"
    [ -n "${container}" ] || return 0
    configured="$(docker container inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
        "${container}" 2>/dev/null \
        | sed -n 's/^MODELING_SERVICE_TOKEN=//p' | tail -n 1)"
    if [ -z "${configured}" ] || [ "${configured}" != "${MODELING_SERVICE_TOKEN}" ]; then
        die "Running Consumer does not use the configured MODELING_SERVICE_TOKEN; " \
            "configure the same value on both sides and restart Consumer"
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

    docker network ls --format '{{.Name}}' \
        | grep -E '(^|_|-)onto-agent-network$|^onto-agent_default$' \
        | head -n 1 || true
}

detect_jiuwenbox_socket_volume() {
    local container
    local volume
    container="$(consumer_container)"
    if [ -n "${container}" ]; then
        volume="$(docker container inspect \
            --format '{{range .Mounts}}{{if eq .Destination "/run/jiuwenbox"}}{{println .Name}}{{end}}{{end}}' \
            "${container}" 2>/dev/null | head -n 1 || true)"
        [ -z "${volume}" ] || { printf '%s\n' "${volume}"; return 0; }
    fi
    docker volume ls --format '{{.Name}}' | grep -E '(^|_)onto-jiuwenbox-sock$' | head -n 1 || true
}

container_health() {
    local container_name="$1"
    docker container inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "${container_name}" 2>/dev/null || true
}

remove_existing_modeling_containers() {
    local bundle_root="$1"
    local name
    local working_dir
    local existing=""
    for name in mw-modeling mw-modeling-agent; do
        if docker container inspect "${name}" >/dev/null 2>&1; then
            working_dir="$(docker container inspect "${name}" \
                --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
                2>/dev/null || true)"
            [ "${working_dir}" = "${bundle_root}" ] || die \
                "Existing container ${name} belongs to another installation directory (${working_dir:-unknown})"
            existing="${existing} ${name}"
        fi
    done
    [ -n "${existing}" ] || return 0
    log "Removing existing Modeling containers:${existing}"
    for name in ${existing}; do
        docker rm -f "${name}" >/dev/null || die "Could not remove existing container ${name}"
    done
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
    local config_file="$4"
    local release_arch
    local host_arch
    local image
    local image_tar
    local agent_image
    local agent_image_tar
    local network
    local jiuwenbox_volume
    local bash_required
    local docker_required
    local docker_actual

    release_arch="$(manifest_get "${manifest_file}" release_arch)"
    host_arch="$(detect_host_arch)"
    [ -n "${release_arch}" ] || die "release_arch is missing in manifest.json"
    [ "${host_arch}" = "${release_arch}" ] \
        || die "Release arch '${release_arch}' does not match host arch '${host_arch}'"
    bash_required="$(manifest_get "${manifest_file}" bash_min_version)"
    docker_required="$(manifest_get "${manifest_file}" docker_min_version)"
    bash_required="${bash_required:-3.2}"
    docker_required="${docker_required:-20.10}"
    version_ge "${BASH_VERSION}" "${bash_required}" \
        || die "Bash ${BASH_VERSION} does not satisfy minimum version ${bash_required}"
    docker_actual="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
    version_ge "${docker_actual}" "${docker_required}" \
        || die "Docker ${docker_actual:-unknown} does not satisfy minimum version ${docker_required}"

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

    require_modeling_service_token "${config_file}"
    ensure_modeling_mcp_token_secret "${config_file}"
    load_modeling_env "${config_file}"
    verify_consumer_service_token

    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die \
        "Could not detect Consumer Docker network; deploy Consumer first or set CONSUMER_NETWORK"
    jiuwenbox_volume="${JIUWENBOX_SOCKET_VOLUME:-$(detect_jiuwenbox_socket_volume)}"
    [ -n "${jiuwenbox_volume}" ] || die \
        "Could not detect JiuwenBox socket volume; deploy Consumer first or set JIUWENBOX_SOCKET_VOLUME"

    log "Loading modeling image ${image}"
    docker load -i "${bundle_root}/${image_tar}" >/dev/null
    log "Loading modeling Agent image ${agent_image}"
    docker load -i "${bundle_root}/${agent_image_tar}" >/dev/null

    remove_existing_modeling_containers "${bundle_root}"
    log "Starting modeling on consumer network ${network}"
    MODELING_IMAGE="${image}" MODELING_AGENT_IMAGE="${agent_image}" CONSUMER_NETWORK="${network}" \
        JIUWENBOX_SOCKET_VOLUME="${jiuwenbox_volume}" compose \
        -p mw-modeling \
        --env-file "${config_file}" \
        -f "${compose_file}" \
        --project-directory "${bundle_root}" \
        up -d --remove-orphans

    wait_for_container "mw-modeling-agent" 180 || die "Modeling Agent container did not become healthy"
    wait_for_container "mw-modeling" 180 || die "Modeling container did not become healthy"
    wait_for_http "http://127.0.0.1:${MODELING_PORT:-5100}/workbench.html" 60 \
        || die "Modeling Workbench page did not become reachable"
    wait_for_http "http://127.0.0.1:${MODELING_MCP_PORT:-5188}/health" 60 \
        || die "Modeling MCP health endpoint did not become reachable"
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

    ensure_source_modeling_service_token "${repo_root}/consumer/backend/.env"
    load_modeling_env "${repo_root}/consumer/backend/.env" "${repo_root}/deploy/.env"
    verify_consumer_service_token

    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die \
        "Could not detect Consumer Docker network; deploy Consumer first or set CONSUMER_NETWORK"
    jiuwenbox_volume="${JIUWENBOX_SOCKET_VOLUME:-$(detect_jiuwenbox_socket_volume)}"
    [ -n "${jiuwenbox_volume}" ] || die \
        "Could not detect JiuwenBox socket volume; deploy Consumer first or set JIUWENBOX_SOCKET_VOLUME"

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
    log "Modeling Workbench deployment finished on port ${MODELING_PORT:-5100}"
}

if [ "$#" -gt 0 ]; then
    die "modeling/deploy/deploy.sh does not accept arguments"
fi

command -v docker >/dev/null 2>&1 || die "Docker is required"
docker info >/dev/null 2>&1 || die "Docker daemon is not reachable"
command -v curl >/dev/null 2>&1 || die "curl is required for Workbench health checks"

if [ -f "${SCRIPT_DIR}/manifest.json" ]; then
    deploy_release \
        "${SCRIPT_DIR}/manifest.json" \
        "$(cd "${SCRIPT_DIR}/.." && pwd)" \
        "${SCRIPT_DIR}/docker-compose.release.yml" \
        "$(cd "${SCRIPT_DIR}/.." && pwd)/.env"
elif [ -f "${SCRIPT_DIR}/../manifest.json" ]; then
    deploy_release \
        "${SCRIPT_DIR}/../manifest.json" \
        "$(cd "${SCRIPT_DIR}/../.." && pwd)" \
        "${SCRIPT_DIR}/docker-compose.release.yml" \
        "$(cd "${SCRIPT_DIR}/../.." && pwd)/backend/.env"
else
    warn "No release manifest found; using source deployment mode"
    deploy_source
fi
