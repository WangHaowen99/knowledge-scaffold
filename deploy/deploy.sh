#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--consumer-only|--modeling-only|--onto-modeling-only|--eval-only]

Deploy the full stack — consumer, Modeling Workbench, Onto Modeling and evaluation —
from either a source checkout or an extracted release package.
Full-stack and --eval-only deployments require EVAL_ENV_FILE pointing to a
protected evaluation runtime configuration file outside the repository.
EOF
}

log() {
    printf '[deploy] %s\n' "$*"
}

die() {
    printf '[deploy][error] %s\n' "$*" >&2
    exit 1
}

warn() {
    printf '[deploy][warn] %s\n' "$*" >&2
}

env_get() {
    local file="$1" key="$2" value
    value="$([ -f "${file}" ] && grep -E "^${key}=" "${file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

ensure_release_modeling_service_token() {
    local bundle_root="$1" env_file="${1}/backend/.env" example_file="${1}/backend/.env.example"
    local secret temp_file
    if [ ! -f "${env_file}" ]; then
        [ -f "${example_file}" ] || die "Consumer environment template not found: ${example_file}"
        cp "${example_file}" "${env_file}"
        chmod 600 "${env_file}"
    fi
    secret="${MODELING_SERVICE_TOKEN:-$(env_get "${env_file}" MODELING_SERVICE_TOKEN)}"
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
    awk -v secret="${secret}" '
        BEGIN { updated = 0 }
        /^MODELING_SERVICE_TOKEN=/ {
            if (!updated) { print "MODELING_SERVICE_TOKEN=" secret; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print "MODELING_SERVICE_TOKEN=" secret }
    ' "${env_file}" > "${temp_file}" || { rm -f "${temp_file}"; die "Cannot update ${env_file}"; }
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
    MODELING_SERVICE_TOKEN="${secret}"
    export MODELING_SERVICE_TOKEN
    log "Modeling service token is ready (value is not printed)"
}

ensure_release_modeling_mcp_token_secret() {
    local bundle_root="$1" env_file="${1}/backend/.env" example_file="${1}/backend/.env.example"
    local secret temp_file
    if [ ! -f "${env_file}" ]; then
        [ -f "${example_file}" ] || die "Consumer environment template not found: ${example_file}"
        cp "${example_file}" "${env_file}"
        chmod 600 "${env_file}"
    fi
    secret="${MODELING_MCP_TOKEN_SECRET:-$(env_get "${env_file}" MODELING_MCP_TOKEN_SECRET)}"
    if [ -z "${secret}" ]; then
        if command -v openssl >/dev/null 2>&1; then
            secret="$(openssl rand -hex 32)"
        elif command -v python3 >/dev/null 2>&1; then
            secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        else
            die "Cannot generate MODELING_MCP_TOKEN_SECRET: openssl or Python 3 is required"
        fi
    fi
    [ "${#secret}" -ge 32 ] || die "MODELING_MCP_TOKEN_SECRET must contain at least 32 characters"
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "Cannot update ${env_file}"
    chmod 600 "${temp_file}"
    awk -v secret="${secret}" '
        BEGIN { updated = 0 }
        /^MODELING_MCP_TOKEN_SECRET=/ {
            if (!updated) { print "MODELING_MCP_TOKEN_SECRET=" secret; updated = 1 }
            next
        }
        { print }
        END { if (!updated) print "MODELING_MCP_TOKEN_SECRET=" secret }
    ' "${env_file}" > "${temp_file}" || { rm -f "${temp_file}"; die "Cannot update ${env_file}"; }
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
    MODELING_MCP_TOKEN_SECRET="${secret}"
    export MODELING_MCP_TOKEN_SECRET
    log "Modeling MCP token secret is ready (value is not printed)"
}

# onto-modeling 接入整栈（开箱即用）：共享 consumer 账号中心 + 同网络 +
# 指向 Modeling Workbench 的评测服务地址。数据评测阶段依赖这三项接线。
consumer_container() {
    # 限定 --type container：同名镜像也会被 docker inspect 命中
    if docker inspect --type container onto-agent-backend >/dev/null 2>&1; then
        printf 'onto-agent-backend\n'
        return 0
    fi
    docker ps --format '{{.Names}}' | grep -E '^onto-agent-backend($|-)|^backend$' | head -n 1 || true
}

# 与 modeling/deploy/deploy.sh 同策略：显式覆盖 > 运行中 consumer 容器的实际
# 网络 > 命名规律兜底。compose 项目名不同则网络名不同（onto-agent_default 即
# 一例），只按 'onto-agent-network$' 匹配会漏。探测不到返回空、由调用方报错——
# 不能猜名字，猜错会让 compose 以 declared as external, but could not be found 失败。
detect_consumer_network() {
    local container network
    if [ -n "${CONSUMER_NETWORK:-}" ]; then
        printf '%s\n' "${CONSUMER_NETWORK}"
        return 0
    fi
    container="$(consumer_container)"
    if [ -n "${container}" ]; then
        network="$(docker container inspect \
            --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
            "${container}" 2>/dev/null | head -n 1)"
        if [ -n "${network}" ]; then
            printf '%s\n' "${network}"
            return 0
        fi
    fi
    docker network ls --format '{{.Name}}' 2>/dev/null \
        | grep -E '(^|_|-)onto-agent-network$|^onto-agent_default$' \
        | head -n 1 || true
}

require_consumer_network() {
    local network
    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die \
        "无法探测 consumer Docker 网络；请先部署 consumer，或显式设置 CONSUMER_NETWORK"
    printf '%s\n' "${network}"
}

container_mount_volume() { # <容器> <容器内挂载点>
    docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"$2"'"}}{{println .Name}}{{end}}{{end}}' \
        "$1" 2>/dev/null | head -n 1 || true
}

# consumer 的 jiuwenbox 管理 socket 卷：只读挂给 onto-modeling 的脚本验证网关后，代码生成的脚本
# 运行验证在沙箱中执行。与 deploy-stack.sh 同策略：取运行中 consumer 后端在 /run/jiuwenbox 的挂载，
# 兜底按卷名查找；探测不到返回空，调用方跳过挂载（验证改在 onto-modeling 容器内执行）。
detect_jiuwenbox_socket_volume() {
    local container volume
    container="$(consumer_container)"
    if [ -n "${container}" ]; then
        volume="$(container_mount_volume "${container}" /run/jiuwenbox)"
        [ -z "${volume}" ] || { printf '%s\n' "${volume}"; return 0; }
    fi
    docker volume ls --format '{{.Name}}' 2>/dev/null \
        | grep -E '(^|_)onto-jiuwenbox-sock$' | head -n 1 || true
}

write_onto_modeling_stack_override() { # <onto-modeling 目录> <mw-modeling 地址>
    local dir="$1" modeling_url="$2" network socket_volume sandbox_service="" sandbox_volume=""
    local hub_env="${dir}/.env" hub_secret
    if ! grep -q '^ONTO_HUB_SERVICE_TOKEN=..*' "${hub_env}" 2>/dev/null; then
        hub_secret="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
        printf '\nONTO_HUB_SERVICE_TOKEN=%s\n' "${hub_secret}" >> "${hub_env}"
        chmod 600 "${hub_env}"
        unset hub_secret
    fi
    network="$(require_consumer_network)"
    socket_volume="$(detect_jiuwenbox_socket_volume)"
    if [ -n "${socket_volume}" ]; then
        # socket 只挂给脚本验证网关，不挂进跑建模 agent 的 onto-modeling 容器
        sandbox_service=$'  sandbox-gateway:\n    volumes:\n      - jiuwenbox-sock:/run/jiuwenbox:ro\n'
        sandbox_volume=$'volumes:\n  jiuwenbox-sock:\n    external: true\n    name: '"${socket_volume}"
    else
        warn "未探测到 consumer 的 jiuwenbox socket 卷：onto-modeling 的脚本运行验证将在容器内执行"
    fi
    cat > "${dir}/docker-compose.stack.yml" <<EOF2
# 由 deploy.sh 生成（整栈部署形态，勿手改；单独部署 onto-modeling 时删除即可）：
# 停用自带 dev auth-server，共享 consumer 账号中心；接入 consumer 网络；
# 配置数据评测所需的 Modeling Workbench 地址；把 consumer 的 jiuwenbox socket 只读挂给脚本验证网关。
services:
  auth-server:
    profiles: ["disabled"]
  knowledge-hub:
    networks:
      - default
      - consumer-net
  onto-modeling:
    depends_on: !override []
    environment:
      - ONTO_WEB_AUTH_URL=http://auth-server:5056
      - ONTO_WEB_MODELING_URL=${modeling_url}
    networks:
      - default
      - consumer-net
${sandbox_service}networks:
  consumer-net:
    external: true
    name: ${network}
${sandbox_volume}
EOF2
}

seed_onto_modeling_bundle_env() { # <bundle 的 onto-modeling 目录>
    local dir="$1" env_file="$1/.env" network current_network socket_volume
    [ -f "${env_file}" ] || { [ -f "${dir}/.env.example" ] && cp "${dir}/.env.example" "${env_file}"; }
    [ -f "${env_file}" ] || touch "${env_file}"
    chmod 600 "${env_file}"
    network="$(require_consumer_network)"
    grep -q '^ONTO_WEB_MODELING_URL=..*' "${env_file}"         || printf 'ONTO_WEB_MODELING_URL=%s
' 'http://mw-modeling:5100' >> "${env_file}"
    # 存量 .env 可能记着一个已不存在的网络（旧版探测失败时写入了固定猜测名），
    # 那样重跑部署也不会自愈——就地删掉失效值，让下面重新写入探测结果。
    current_network="$(sed -n 's/^ONTO_MODELING_EXTERNAL_AUTH_NETWORK=//p' "${env_file}" | head -n 1)"
    if [ -n "${current_network}" ] && ! docker network inspect "${current_network}" >/dev/null 2>&1; then
        warn "配置中的 consumer 网络 ${current_network} 不存在，改用探测到的 ${network}"
        sed -i.bak '/^ONTO_MODELING_EXTERNAL_AUTH_NETWORK=/d' "${env_file}"
        command rm -f "${env_file}.bak"
        chmod 600 "${env_file}"
    fi
    grep -q '^ONTO_MODELING_EXTERNAL_AUTH_NETWORK=..*' "${env_file}"         || printf 'ONTO_MODELING_EXTERNAL_AUTH_NETWORK=%s
' "${network}" >> "${env_file}"
    # jiuwenbox socket 卷以现场探测为准：每次部署重写，探测不到就不挂载
    socket_volume="$(detect_jiuwenbox_socket_volume)"
    sed -i.bak '/^ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=/d' "${env_file}"
    command rm -f "${env_file}.bak"
    if [ -n "${socket_volume}" ]; then
        printf 'ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=%s\n' "${socket_volume}" >> "${env_file}"
    else
        warn "未探测到 consumer 的 jiuwenbox socket 卷：onto-modeling 的脚本运行验证将在容器内执行"
    fi
    chmod 600 "${env_file}"
}

CONSUMER_ONLY=0
MODELING_ONLY=0
ONTO_MODELING_ONLY=0
EVAL_ONLY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --eval-only)
            EVAL_ONLY=1
            ;;
        --consumer-only)
            CONSUMER_ONLY=1
            ;;
        --modeling-only)
            MODELING_ONLY=1
            ;;
        --onto-modeling-only)
            ONTO_MODELING_ONLY=1
            ;;
        -h|--help|help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "Unknown argument: $1"
            ;;
    esac
    shift
done

if [ "$(( CONSUMER_ONLY + MODELING_ONLY + ONTO_MODELING_ONLY + EVAL_ONLY ))" -gt 1 ]; then
    die "--consumer-only / --modeling-only / --onto-modeling-only / --eval-only cannot be combined"
fi

DEPLOY_EVALUATION=0
if [ "$(( CONSUMER_ONLY + MODELING_ONLY + ONTO_MODELING_ONLY ))" = "0" ]; then
    DEPLOY_EVALUATION=1
    # Fail before changing any services if evaluation cannot be deployed.
    [ -f "$SCRIPT_DIR/../eval-service/deploy/deploy.sh" ] \
        || die "Evaluation deployment files missing; use a full release package including eval-service"
    [ -n "${EVAL_ENV_FILE:-}" ] \
        || die "Set EVAL_ENV_FILE to a protected evaluation runtime configuration file"
    [ -f "$EVAL_ENV_FILE" ] && [ -r "$EVAL_ENV_FILE" ] \
        || die "Evaluation runtime configuration file is missing or unreadable"
    # Child deployment scripts can change directories.
    EVAL_ENV_FILE="$(cd "$(dirname "$EVAL_ENV_FILE")" && pwd)/$(basename "$EVAL_ENV_FILE")"
    export EVAL_ENV_FILE
fi

deploy_evaluation() {
    if [ "$DEPLOY_EVALUATION" = "1" ]; then
        log "Deploying independent evaluation service"
        bash "$SCRIPT_DIR/../eval-service/deploy/deploy.sh"
    fi
}

if [ "$EVAL_ONLY" = "1" ]; then
    deploy_evaluation
    exit 0
fi

# A release archive is commonly unpacked into a staging directory by CI or a
# remote deployment runner. When an existing install is present elsewhere,
# dispatch through its upgrade flow so BUNDLE_ROOT and persistent data stay
# bound to the real installation directory.
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
    if [ "${EXISTING_INSTALL}" = true ] && [ "${ONTO_MODELING_ONLY}" = "0" ]; then
        [ -f "${SCRIPT_DIR}/upgrade.sh" ] \
            || die "Existing installation detected but upgrade.sh is missing"
        log "Existing installation detected at ${INSTALL_DIR}; dispatching upgrade flow"
        UPGRADE_ARGS=("${INSTALL_DIR}")
        if [ "${CONSUMER_ONLY}" = "1" ]; then
            UPGRADE_ARGS+=(--consumer-only)
        elif [ "${MODELING_ONLY}" = "1" ]; then
            UPGRADE_ARGS+=(--modeling-only)
        fi
        bash "${SCRIPT_DIR}/upgrade.sh" "${UPGRADE_ARGS[@]}"
        # upgrade.sh 只覆盖 consumer 与 modeling；onto-modeling 的部署自身幂等，
        # 升级后继续拉起（数据卷由 compose 项目名锚定，不受升级影响）
        if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]             && [ -f "${BUNDLE_ROOT}/onto-modeling/deploy.sh" ]; then
            log "Deploying Onto Modeling (guided modeling workbench)"
            seed_onto_modeling_bundle_env "${BUNDLE_ROOT}/onto-modeling"
            bash "${BUNDLE_ROOT}/onto-modeling/deploy.sh"
        fi
        deploy_evaluation
        exit 0
    fi
fi

DEPLOY_ARGS=()
if [ "${CONSUMER_ONLY}" = "1" ]; then
    DEPLOY_ARGS+=(--consumer-only)
elif [ "${MODELING_ONLY}" = "1" ]; then
    DEPLOY_ARGS+=(--modeling-only)
fi

if [ -f "${SCRIPT_DIR}/deploy-consumer.sh" ]; then
    CONSUMER_DEPLOY="${SCRIPT_DIR}/deploy-consumer.sh"
    MODELING_DEPLOY="${SCRIPT_DIR}/modeling/deploy.sh"
elif [ -f "${SCRIPT_DIR}/deploy-stack.sh" ]; then
    log "Detected source checkout; deploying with deploy-stack.sh"
    if [ "${ONTO_MODELING_ONLY}" = "0" ]; then
        bash "${SCRIPT_DIR}/deploy-stack.sh" up "${DEPLOY_ARGS[@]}"
    fi
    if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
        if [ -d "${SCRIPT_DIR}/../modeling/onto-modeling" ]; then
            log "Deploying Onto Modeling (source, docker compose, joined to consumer stack)"
            write_onto_modeling_stack_override \
                "${SCRIPT_DIR}/../modeling/onto-modeling" "http://mw-modeling:5100"
            (cd "${SCRIPT_DIR}/../modeling/onto-modeling" \
                && docker compose -f docker-compose.yml -f docker-compose.stack.yml up -d --build)
        else
            log "Onto Modeling source directory not present; skipping"
        fi
    fi
    deploy_evaluation
    log "Deployment finished successfully"
    exit 0
else
    die "Could not find source or release deployment scripts under ${SCRIPT_DIR}"
fi

BUNDLE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ensure_release_modeling_service_token "${BUNDLE_ROOT}"
ensure_release_modeling_mcp_token_secret "${BUNDLE_ROOT}"

if [ "${MODELING_ONLY}" = "0" ] && [ "${ONTO_MODELING_ONLY}" = "0" ]; then
    [ -f "${CONSUMER_DEPLOY}" ] || die "Consumer deploy script not found: ${CONSUMER_DEPLOY}"
    log "Deploying consumer services"
    bash "${CONSUMER_DEPLOY}"
fi

if [ "${CONSUMER_ONLY}" = "0" ] && [ "${ONTO_MODELING_ONLY}" = "0" ]; then
    [ -f "${MODELING_DEPLOY}" ] || die "Modeling deploy script not found: ${MODELING_DEPLOY}"
    log "Deploying Modeling Workbench (core + Agent)"
    bash "${MODELING_DEPLOY}"
fi

ONTO_MODELING_DEPLOY="${BUNDLE_ROOT}/onto-modeling/deploy.sh"
if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
    if [ -f "${ONTO_MODELING_DEPLOY}" ]; then
        log "Deploying Onto Modeling (guided modeling workbench)"
        seed_onto_modeling_bundle_env "${BUNDLE_ROOT}/onto-modeling"
        bash "${ONTO_MODELING_DEPLOY}"
    elif [ "${ONTO_MODELING_ONLY}" = "1" ]; then
        die "Onto Modeling deploy script not found: ${ONTO_MODELING_DEPLOY}"
    else
        log "Onto Modeling bundle not present; skipping (older release package)"
    fi
fi

deploy_evaluation
log "Deployment finished successfully"
