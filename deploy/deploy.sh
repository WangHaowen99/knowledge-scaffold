#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--consumer-only|--modeling-only|--onto-modeling-only|--knowledge-hub-only|--eval-only]

Deploy the full stack — consumer, Modeling Workbench, Knowledge Package Hub,
Onto Modeling and evaluation — from either a source checkout or an extracted
release package. Knowledge Package Hub and Onto Modeling are independent
compose projects; --onto-modeling-only does not touch the Hub.
Evaluation keeps its runtime configuration in eval-service/deploy/.env and
reuses the evaluation credentials of the Hub and consumer, plus Onto Modeling's
notification credential; set EVAL_ENV_FILE to use a protected configuration
file outside the repository instead.
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

env_set() { # <文件> <键> <值>：替换或追加，权限 600，值不输出
    local temp_file
    [ -f "$1" ] || (umask 077 && : > "$1")
    temp_file="$(mktemp "$1.tmp.XXXXXX")" || die "Cannot update $1"
    chmod 600 "${temp_file}"
    awk -v key="$2" -v value="$3" '
        BEGIN { updated = 0 }
        index($0, key "=") == 1 { if (!updated) { print key "=" value; updated = 1 } next }
        { print }
        END { if (!updated) print key "=" value }
    ' "$1" > "${temp_file}" || { rm -f "${temp_file}"; die "Cannot update $1"; }
    mv "${temp_file}" "$1"
    chmod 600 "$1"
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
    # Hub 未部署过时补一个凭据，onto-modeling 才能启动；部署过 Hub 时调用方已对齐为 Hub 的值
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
# 停用自带 dev auth-server，共享 consumer 账号中心；接入 consumer 网络（独立部署的知识包 Hub 也在此网络）；
# 配置数据评测所需的 Modeling Workbench 地址；把 consumer 的 jiuwenbox socket 只读挂给脚本验证网关。
services:
  auth-server:
    profiles: ["disabled"]
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

# 知识包 Hub 与 onto-modeling 分属两个 compose 项目，经 consumer 网络互通。Hub 沿用 onto-modeling
# 已有的服务凭据（都没有时由 Hub 部署脚本生成）；值只经子进程环境传递，不输出。
deploy_knowledge_hub() { # <Hub 部署脚本> <onto-modeling 目录>
    local network onto_token
    network="$(require_consumer_network)"
    onto_token="$(env_get "$2/.env" ONTO_HUB_SERVICE_TOKEN)"
    log "Deploying Knowledge Package Hub (independent compose project)"
    CONSUMER_NETWORK="${network}" HUB_SERVICE_TOKEN="${onto_token:-${HUB_SERVICE_TOKEN:-}}" bash "$1"
}

# onto-modeling 部署前改用 Hub 实际生效的服务凭据；Hub 未部署过时保持原值。
adopt_hub_service_token() { # <Hub .env> <onto-modeling .env>
    local token
    token="$(env_get "$1" HUB_SERVICE_TOKEN)"
    [ -z "${token}" ] || env_set "$2" ONTO_HUB_SERVICE_TOKEN "${token}"
}

# onto-modeling 接收评测完成通知的入站凭据：已有值保留（发布包解压到暂存目录时沿用安装目录的值，否则
# 单独升级 onto-modeling 会与评测侧失配），缺失时生成；评测部署时沿用它作为 EVAL_MODELING_SERVICE_TOKEN。
# 不得与 Hub 服务凭据相同，值不输出。
ensure_onto_evaluation_service_token() { # <onto-modeling .env>
    local secret
    secret="${ONTO_EVALUATION_SERVICE_TOKEN:-$(first_env_value ONTO_EVALUATION_SERVICE_TOKEN \
        "$1" "${INSTALL_DIR}/onto-modeling/.env")}"
    [ -n "${secret}" ] || secret="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    [ "${#secret}" -ge 32 ] || die "ONTO_EVALUATION_SERVICE_TOKEN must contain at least 32 characters"
    [ "${secret}" != "$(env_get "$1" ONTO_HUB_SERVICE_TOKEN)" ] \
        || die "ONTO_EVALUATION_SERVICE_TOKEN must differ from ONTO_HUB_SERVICE_TOKEN"
    env_set "$1" ONTO_EVALUATION_SERVICE_TOKEN "${secret}"
    log "Onto Modeling evaluation notification token is ready (value is not printed)"
}

# consumer 的评测服务凭据：已有值保留，缺失时生成；consumer 拒绝与 MODELING_SERVICE_TOKEN 相同的值。
ensure_evaluation_service_token() { # <consumer backend/.env>
    local secret
    secret="${EVALUATION_SERVICE_TOKEN:-$(env_get "$1" EVALUATION_SERVICE_TOKEN)}"
    [ -n "${secret}" ] || secret="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    [ "${#secret}" -ge 32 ] || die "EVALUATION_SERVICE_TOKEN must contain at least 32 characters"
    [ "${secret}" != "$(env_get "$1" MODELING_SERVICE_TOKEN)" ] \
        || die "EVALUATION_SERVICE_TOKEN must differ from MODELING_SERVICE_TOKEN"
    env_set "$1" EVALUATION_SERVICE_TOKEN "${secret}"
    log "Consumer evaluation service token is ready (value is not printed)"
}

first_env_value() { # <键> <文件>...：返回第一个非空值，用于在源码、发布包与安装目录布局间查找
    local key="$1" file value
    shift
    for file in "$@"; do
        value="$(env_get "${file}" "${key}")"
        [ -z "${value}" ] || { printf '%s' "${value}"; return 0; }
    done
}

CONSUMER_ONLY=0
MODELING_ONLY=0
ONTO_MODELING_ONLY=0
KNOWLEDGE_HUB_ONLY=0
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
        --knowledge-hub-only)
            KNOWLEDGE_HUB_ONLY=1
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

ONLY_FLAGS="$(( CONSUMER_ONLY + MODELING_ONLY + ONTO_MODELING_ONLY + KNOWLEDGE_HUB_ONLY ))"
if [ "$(( ONLY_FLAGS + EVAL_ONLY ))" -gt 1 ]; then
    die "--consumer-only / --modeling-only / --onto-modeling-only / --knowledge-hub-only / --eval-only cannot be combined"
fi

INSTALL_DIR="${ONTO_AGENT_INSTALL_DIR:-/opt/onto-agent}"
DEPLOY_EVALUATION=0
if [ "${ONLY_FLAGS}" = "0" ]; then
    DEPLOY_EVALUATION=1
    # Fail before changing any services if evaluation cannot be deployed.
    [ -f "$SCRIPT_DIR/../eval-service/deploy/deploy.sh" ] \
        || die "Evaluation deployment files missing; use a full release package including eval-service"
    # Without EVAL_ENV_FILE the evaluation deployer maintains eval-service/deploy/.env itself.
    if [ -n "${EVAL_ENV_FILE:-}" ]; then
        [ -f "$EVAL_ENV_FILE" ] && [ -r "$EVAL_ENV_FILE" ] || die \
            "Evaluation runtime configuration file is missing or unreadable: EVAL_ENV_FILE=$EVAL_ENV_FILE; fix the path, or unset EVAL_ENV_FILE to let eval-service/deploy/deploy.sh maintain its own .env"
        # Child deployment scripts can change directories.
        EVAL_ENV_FILE="$(cd "$(dirname "$EVAL_ENV_FILE")" && pwd)/$(basename "$EVAL_ENV_FILE")"
        export EVAL_ENV_FILE
    fi
fi

# 评测沿用 Hub 与 consumer 实际生效的评测凭据，以及 onto-modeling 接收完成通知的凭据，按源码、
# 发布包、安装目录的布局依次查找；值只经子进程环境传递。consumer 网络必填，探测不到时由评测部署脚本在改动配置前报错。
deploy_evaluation() {
    local root="${SCRIPT_DIR}/.."
    [ "$DEPLOY_EVALUATION" = "1" ] || return 0
    log "Deploying independent evaluation service"
    CONSUMER_NETWORK="$(detect_consumer_network)" \
    HUB_EVALUATION_SERVICE_TOKEN="$(first_env_value HUB_EVALUATION_SERVICE_TOKEN \
        "${root}/knowledge-hub/deploy/.env" "${root}/knowledge-hub/.env" "${INSTALL_DIR}/knowledge-hub/.env")" \
    EVALUATION_SERVICE_TOKEN="$(first_env_value EVALUATION_SERVICE_TOKEN \
        "${root}/consumer/backend/.env" "${root}/backend/.env" "${INSTALL_DIR}/backend/.env")" \
    EVAL_MODELING_SERVICE_TOKEN="$(first_env_value ONTO_EVALUATION_SERVICE_TOKEN \
        "${root}/modeling/onto-modeling/.env" "${root}/onto-modeling/.env" "${INSTALL_DIR}/onto-modeling/.env")" \
        bash "$SCRIPT_DIR/../eval-service/deploy/deploy.sh"
}

# 发布包内的 knowledge-hub/ 与 onto-modeling/ 是两个独立子包：先 Hub，再 onto-modeling（沿用
# Hub 的服务凭据）。各自部署幂等，数据卷由各自固定的 compose 项目名锚定，升级后照常拉起。
deploy_bundle_hub_and_onto_modeling() {
    local hub_dir="${BUNDLE_ROOT}/knowledge-hub" onto_dir="${BUNDLE_ROOT}/onto-modeling"
    if [ "${ONTO_MODELING_ONLY}" = "0" ]; then
        if [ -f "${hub_dir}/deploy.sh" ]; then
            deploy_knowledge_hub "${hub_dir}/deploy.sh" "${onto_dir}"
        elif [ "${KNOWLEDGE_HUB_ONLY}" = "1" ]; then
            die "Knowledge Hub deploy script not found: ${hub_dir}/deploy.sh"
        else
            log "Knowledge Hub bundle not present; skipping (older release package)"
        fi
    fi
    [ "${KNOWLEDGE_HUB_ONLY}" = "0" ] || return 0
    if [ -f "${onto_dir}/deploy.sh" ]; then
        log "Deploying Onto Modeling (guided modeling workbench)"
        seed_onto_modeling_bundle_env "${onto_dir}"
        adopt_hub_service_token "${hub_dir}/.env" "${onto_dir}/.env"
        ensure_onto_evaluation_service_token "${onto_dir}/.env"
        bash "${onto_dir}/deploy.sh"
    elif [ "${ONTO_MODELING_ONLY}" = "1" ]; then
        die "Onto Modeling deploy script not found: ${onto_dir}/deploy.sh"
    else
        log "Onto Modeling bundle not present; skipping (older release package)"
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
    if [ "${EXISTING_INSTALL}" = true ] && [ "${ONTO_MODELING_ONLY}" = "0" ] \
        && [ "${KNOWLEDGE_HUB_ONLY}" = "0" ]; then
        [ -f "${SCRIPT_DIR}/upgrade.sh" ] \
            || die "Existing installation detected but upgrade.sh is missing"
        log "Existing installation detected at ${INSTALL_DIR}; dispatching upgrade flow"
        UPGRADE_ARGS=("${INSTALL_DIR}")
        if [ "${CONSUMER_ONLY}" = "1" ]; then
            UPGRADE_ARGS+=(--consumer-only)
        elif [ "${MODELING_ONLY}" = "1" ]; then
            UPGRADE_ARGS+=(--modeling-only)
        fi
        # upgrade.sh 沿用安装目录的 consumer 配置：先补齐评测凭据，consumer 升级重启后生效
        if [ "${MODELING_ONLY}" = "0" ] && [ -f "${INSTALL_DIR}/backend/.env" ]; then
            ensure_evaluation_service_token "${INSTALL_DIR}/backend/.env"
        fi
        bash "${SCRIPT_DIR}/upgrade.sh" "${UPGRADE_ARGS[@]}"
        # upgrade.sh 只覆盖 consumer 与 modeling；Hub 与 onto-modeling 升级后继续拉起
        if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
            deploy_bundle_hub_and_onto_modeling
        fi
        # 评测配置含数据加密密钥：沿用安装目录中的配置，不在暂存目录生成新密钥
        if [ "${DEPLOY_EVALUATION}" = "1" ] && [ -z "${EVAL_ENV_FILE:-}" ] \
                && [ -f "${INSTALL_DIR}/eval-service/deploy/.env" ] \
                && [ ! -f "${BUNDLE_ROOT}/eval-service/deploy/.env" ]; then
            (umask 077 && cp "${INSTALL_DIR}/eval-service/deploy/.env" "${BUNDLE_ROOT}/eval-service/deploy/.env")
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
    if [ "${ONTO_MODELING_ONLY}" = "0" ] && [ "${KNOWLEDGE_HUB_ONLY}" = "0" ]; then
        bash "${SCRIPT_DIR}/deploy-stack.sh" up "${DEPLOY_ARGS[@]}"
    fi
    if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
        ONTO_MODELING_SOURCE="${SCRIPT_DIR}/../modeling/onto-modeling"
        if [ "${ONTO_MODELING_ONLY}" = "0" ]; then
            if [ -f "${SCRIPT_DIR}/../knowledge-hub/deploy/deploy.sh" ]; then
                deploy_knowledge_hub "${SCRIPT_DIR}/../knowledge-hub/deploy/deploy.sh" "${ONTO_MODELING_SOURCE}"
            else
                log "Knowledge Hub source directory not present; skipping"
            fi
        fi
        if [ "${KNOWLEDGE_HUB_ONLY}" = "0" ]; then
            if [ -d "${ONTO_MODELING_SOURCE}" ]; then
                log "Deploying Onto Modeling (source, docker compose, joined to consumer stack)"
                adopt_hub_service_token "${SCRIPT_DIR}/../knowledge-hub/deploy/.env" "${ONTO_MODELING_SOURCE}/.env"
                ensure_onto_evaluation_service_token "${ONTO_MODELING_SOURCE}/.env"
                write_onto_modeling_stack_override "${ONTO_MODELING_SOURCE}" "http://mw-modeling:5100"
                # --remove-orphans 清掉旧版本随 onto-modeling 部署的 Hub 容器，避免与独立 Hub 抢同一服务名
                (cd "${ONTO_MODELING_SOURCE}" \
                    && docker compose -f docker-compose.yml -f docker-compose.stack.yml up -d --build --remove-orphans)
            else
                log "Onto Modeling source directory not present; skipping"
            fi
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

if [ "${MODELING_ONLY}" = "0" ] && [ "${ONTO_MODELING_ONLY}" = "0" ] && [ "${KNOWLEDGE_HUB_ONLY}" = "0" ]; then
    [ -f "${CONSUMER_DEPLOY}" ] || die "Consumer deploy script not found: ${CONSUMER_DEPLOY}"
    ensure_evaluation_service_token "${BUNDLE_ROOT}/backend/.env"
    log "Deploying consumer services"
    bash "${CONSUMER_DEPLOY}"
fi

if [ "${CONSUMER_ONLY}" = "0" ] && [ "${ONTO_MODELING_ONLY}" = "0" ] && [ "${KNOWLEDGE_HUB_ONLY}" = "0" ]; then
    [ -f "${MODELING_DEPLOY}" ] || die "Modeling deploy script not found: ${MODELING_DEPLOY}"
    log "Deploying Modeling Workbench (core + Agent)"
    bash "${MODELING_DEPLOY}"
fi

if [ "${CONSUMER_ONLY}" = "0" ] && [ "${MODELING_ONLY}" = "0" ]; then
    deploy_bundle_hub_and_onto_modeling
fi

deploy_evaluation
log "Deployment finished successfully"
