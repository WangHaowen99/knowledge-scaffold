#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_DIR="${SCRIPT_DIR}/state"
STATE_FILE="${STATE_DIR}/current.env"
BACKUP_ROOT="${SCRIPT_DIR}/backups"
MANIFEST_FILE="${SCRIPT_DIR}/manifest.json"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.release.yml"
BACKEND_COMPOSE_ENV_FILE="${BUNDLE_ROOT}/backend/.env"
LEGACY_COMPOSE_ENV_FILE="${BUNDLE_ROOT}/.env"

mkdir -p "${STATE_DIR}" "${BACKUP_ROOT}"

log() {
    printf '[deploy] %s\n' "$*"
}

warn() {
    printf '[deploy][warn] %s\n' "$*" >&2
}

die() {
    printf '[deploy][error] %s\n' "$*" >&2
    exit 1
}

require_file() {
    local path="$1"
    [ -f "${path}" ] || die "Required file not found: ${path}"
}

require_dir() {
    local path="$1"
    [ -d "${path}" ] || die "Required directory not found: ${path}"
}

manifest_get() {
    local key="$1"
    require_file "${MANIFEST_FILE}"
    sed -n 's/^[[:space:]]*"'${key}'":[[:space:]]*"\([^"]*\)".*$/\1/p' "${MANIFEST_FILE}" | head -n 1
}

load_state() {
    if [ -f "${STATE_FILE}" ]; then
        # shellcheck disable=SC1090
        source "${STATE_FILE}"
    else
        CURRENT_VERSION=""
        PREVIOUS_VERSION=""
        LAST_BACKUP_DIR=""
        CURRENT_BACKEND_IMAGE=""
        CURRENT_FRONTEND_IMAGE=""
        PREVIOUS_BACKEND_IMAGE=""
        PREVIOUS_FRONTEND_IMAGE=""
        CURRENT_AUTH_IMAGE=""
        PREVIOUS_AUTH_IMAGE=""
        CURRENT_JIUWENBOX_IMAGE=""
        PREVIOUS_JIUWENBOX_IMAGE=""
    fi

    # 旧版本发布包写出的 state 文件没有 auth 镜像字段，升级时补默认值
    CURRENT_AUTH_IMAGE="${CURRENT_AUTH_IMAGE:-}"
    PREVIOUS_AUTH_IMAGE="${PREVIOUS_AUTH_IMAGE:-}"
    CURRENT_JIUWENBOX_IMAGE="${CURRENT_JIUWENBOX_IMAGE:-}"
    PREVIOUS_JIUWENBOX_IMAGE="${PREVIOUS_JIUWENBOX_IMAGE:-}"
}

write_state() {
    local current_version="$1"
    local previous_version="$2"
    local last_backup_dir="$3"
    local current_backend_image="$4"
    local current_frontend_image="$5"
    local previous_backend_image="$6"
    local previous_frontend_image="$7"
    local current_auth_image="${8:-}"
    local previous_auth_image="${9:-}"
    local current_jiuwenbox_image="${10:-}"
    local previous_jiuwenbox_image="${11:-}"

    mkdir -p "${STATE_DIR}"
    {
        printf 'CURRENT_VERSION=%q\n' "${current_version}"
        printf 'PREVIOUS_VERSION=%q\n' "${previous_version}"
        printf 'LAST_BACKUP_DIR=%q\n' "${last_backup_dir}"
        printf 'CURRENT_BACKEND_IMAGE=%q\n' "${current_backend_image}"
        printf 'CURRENT_FRONTEND_IMAGE=%q\n' "${current_frontend_image}"
        printf 'PREVIOUS_BACKEND_IMAGE=%q\n' "${previous_backend_image}"
        printf 'PREVIOUS_FRONTEND_IMAGE=%q\n' "${previous_frontend_image}"
        printf 'CURRENT_AUTH_IMAGE=%q\n' "${current_auth_image}"
        printf 'PREVIOUS_AUTH_IMAGE=%q\n' "${previous_auth_image}"
        printf 'CURRENT_JIUWENBOX_IMAGE=%q\n' "${current_jiuwenbox_image}"
        printf 'PREVIOUS_JIUWENBOX_IMAGE=%q\n' "${previous_jiuwenbox_image}"
    } > "${STATE_FILE}"
}

ensure_runtime_layout() {
    # auth-server 为必备组件，其数据目录 .onto-auth 始终创建
    mkdir -p \
        "${BUNDLE_ROOT}/backend/logs" \
        "${BUNDLE_ROOT}/.onto-runtime/config" \
        "${BUNDLE_ROOT}/.onto-runtime/data/chats" \
        "${BUNDLE_ROOT}/.onto-runtime/data/executions" \
        "${BUNDLE_ROOT}/frontend/logs/nginx" \
        "${BUNDLE_ROOT}/.onto-auth"

    if [ ! -d "${BUNDLE_ROOT}/.onto-runtime" ]; then
        mkdir -p "${BUNDLE_ROOT}/.onto-runtime"
    fi
}

ensure_env_files() {
    if [ ! -f "${BUNDLE_ROOT}/backend/.env" ] && [ -f "${BUNDLE_ROOT}/backend/.env.example" ]; then
        cp "${BUNDLE_ROOT}/backend/.env.example" "${BUNDLE_ROOT}/backend/.env"
    fi
}

require_mcp_token_secret() {
    local env_file="${BUNDLE_ROOT}/backend/.env"
    local secret
    require_file "${env_file}"
    secret="$(sed -n 's/^[[:space:]]*MCP_TOKEN_SECRET[[:space:]]*=[[:space:]]*//p' "${env_file}" | tail -n 1)"
    secret="${secret%\"}"
    secret="${secret#\"}"
    secret="${secret%\'}"
    secret="${secret#\'}"
    [ "${#secret}" -ge 32 ] || die \
        "Set MCP_TOKEN_SECRET to an independent random value of at least 32 characters in ${env_file}"
}

detect_host_arch() {
    local arch
    arch="$(uname -m)"
    case "${arch}" in
        x86_64|amd64)
            printf 'amd64\n'
            ;;
        aarch64|arm64)
            printf 'arm64\n'
            ;;
        *)
            printf '%s\n' "${arch}"
            ;;
    esac
}

check_port_available() {
    local port="$1"

    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "( sport = :${port} )" | grep -q ":${port}"; then
            warn "Port ${port} appears to be in use"
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -ltn 2>/dev/null | grep -q "[\.:]${port}[[:space:]]"; then
            warn "Port ${port} appears to be in use"
        fi
    fi
}

docker_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        printf 'docker compose'
        return 0
    fi
    return 1
}

compose_env_files() {
    # Keep legacy host-level overrides effective without using them as backend env.
    printf '%s\n' "${BACKEND_COMPOSE_ENV_FILE}"
    if [ -f "${LEGACY_COMPOSE_ENV_FILE}" ]; then
        printf '%s\n' "${LEGACY_COMPOSE_ENV_FILE}"
    fi
}

build_compose_env_args() {
    COMPOSE_ENV_ARGS=()
    while IFS= read -r env_file; do
        COMPOSE_ENV_ARGS+=(--env-file "${env_file}")
    done < <(compose_env_files)
}

compose_up() {
    local compose_cmd
    compose_cmd="$(docker_compose_cmd)" || die "Docker Compose is not available"
    build_compose_env_args

    # shellcheck disable=SC2086
    BACKEND_IMAGE="$1" FRONTEND_IMAGE="$2" AUTH_SERVER_IMAGE="${3:-}" JIUWENBOX_IMAGE="${4:-onto-agent-jiuwenbox:latest}" ${compose_cmd} \
        "${COMPOSE_ENV_ARGS[@]}" \
        -f "${COMPOSE_FILE}" \
        --project-directory "${BUNDLE_ROOT}" \
        up -d
}

compose_down() {
    local compose_cmd
    compose_cmd="$(docker_compose_cmd)" || die "Docker Compose is not available"
    build_compose_env_args

    # shellcheck disable=SC2086
    BACKEND_IMAGE="$1" FRONTEND_IMAGE="$2" AUTH_SERVER_IMAGE="${3:-}" JIUWENBOX_IMAGE="${4:-onto-agent-jiuwenbox:latest}" ${compose_cmd} \
        "${COMPOSE_ENV_ARGS[@]}" \
        -f "${COMPOSE_FILE}" \
        --project-directory "${BUNDLE_ROOT}" \
        down
}

compose_stop() {
    local compose_cmd
    compose_cmd="$(docker_compose_cmd)" || die "Docker Compose is not available"
    build_compose_env_args

    # shellcheck disable=SC2086
    BACKEND_IMAGE="$1" FRONTEND_IMAGE="$2" AUTH_SERVER_IMAGE="${3:-}" JIUWENBOX_IMAGE="${4:-onto-agent-jiuwenbox:latest}" ${compose_cmd} \
        "${COMPOSE_ENV_ARGS[@]}" \
        -f "${COMPOSE_FILE}" \
        --project-directory "${BUNDLE_ROOT}" \
        stop
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

docker_load_image() {
    local image_tar="$1"
    require_file "${image_tar}"
    docker load -i "${image_tar}" >/dev/null
}
