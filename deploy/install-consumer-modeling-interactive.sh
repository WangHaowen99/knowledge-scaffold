#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=""
INSTALL_ROOT=""
BACKEND_PORT=""
MCP_SERVER_PORT=""
FRONTEND_PORT=""
ONTO_WEB_PORT=""
CONSUMER_PACKAGE=""
AUTH_PACKAGE=""
ONTO_MODELING_PACKAGE=""
KNOWLEDGE_HUB_PACKAGE=""
EVALUATION_PACKAGE=""
PORT_MODE="default"

log() { printf '[interactive-deploy] %s\n' "$*"; }
die() { printf '[interactive-deploy][error] %s\n' "$*" >&2; exit 1; }
ask() {
    prompt="$1"
    printf '%s' "${prompt}" >/dev/tty || die '需要交互式终端'
    IFS= read -r answer </dev/tty || die '读取输入失败'
    [ -n "${answer}" ] || die '输入不能为空'
    printf '%s' "${answer}"
}

ask_choice() {
    prompt="$1"; default="$2"; choices="$3"
    while :; do
        printf '%s' "${prompt}" >/dev/tty
        IFS= read -r answer </dev/tty || die '读取输入失败'
        answer="${answer:-${default}}"
        case " ${choices} " in *" ${answer} "*) printf '%s' "${answer}"; return ;; esac
        printf '请输入：%s\n' "${choices}" >/dev/tty
    done
}

ask_port() {
    local prompt="$1" value
    while :; do
        value="$(ask "${prompt}")"
        value="$(printf '%s' "${value}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        case "${value}" in
            ''|*[!0-9]*)
                printf '端口必须是1到65535之间的整数，请重新输入。\n' >/dev/tty
                continue
                ;;
        esac
        if [ "${value}" -ge 1 ] && [ "${value}" -le 65535 ]; then
            printf '%s' "${value}"
            return 0
        fi
        printf '端口必须是1到65535之间的整数，请重新输入。\n' >/dev/tty
    done
}

resolve_package() {
    pattern="$1"; label="$2"
    case "${pattern}" in
        *\**|*\?*|*\[* )
            shopt -s nullglob; matches=( ${pattern} ); shopt -u nullglob
            case "${#matches[@]}" in
                0) die "${label}通配符未匹配到文件：${pattern}" ;;
                1) pattern="${matches[0]}" ;;
                *) die "${label}通配符匹配到多个文件：${pattern}" ;;
            esac
            ;;
    esac
    printf '%s' "${pattern}"
}

env_get() {
    local file="$1" key="$2" value
    value="$([ -f "${file}" ] && grep -E "^[[:space:]]*${key}[[:space:]]*=" "${file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

collect_ports() {
    if [ "${PORT_MODE}" = 1 ]; then
        BACKEND_PORT=5055; MCP_SERVER_PORT=7488; FRONTEND_PORT=8888
        ONTO_WEB_PORT=5100
        return
    fi
    BACKEND_PORT="$(ask_port '请输入 Consumer Backend 宿主端口：')"
    MCP_SERVER_PORT="$(ask_port '请输入 Consumer MCP 宿主端口：')"
    FRONTEND_PORT="$(ask_port '请输入 Consumer Frontend 宿主端口：')"
    ONTO_WEB_PORT="$(ask_port '请输入 Onto Modeling 宿主端口：')"
}

load_upgrade_ports() {
    local consumer_env="${INSTALL_ROOT}/onto-agent/backend/.env"
    local onto_env="${INSTALL_ROOT}/onto-modeling/.env"
    BACKEND_PORT="$(env_get "${consumer_env}" BACKEND_PORT)"; BACKEND_PORT="${BACKEND_PORT:-5055}"
    MCP_SERVER_PORT="$(env_get "${consumer_env}" MCP_SERVER_PORT)"; MCP_SERVER_PORT="${MCP_SERVER_PORT:-7488}"
    FRONTEND_PORT="$(env_get "${consumer_env}" FRONTEND_PORT)"; FRONTEND_PORT="${FRONTEND_PORT:-8888}"
    ONTO_WEB_PORT="$(env_get "${onto_env}" ONTO_WEB_PORT)"; ONTO_WEB_PORT="${ONTO_WEB_PORT:-5100}"
    log "升级将保留现有端口：Consumer=${BACKEND_PORT}/${MCP_SERVER_PORT}/${FRONTEND_PORT}，Onto Modeling=${ONTO_WEB_PORT}"
}

# consumer 由本脚本部署、容器名固定，直接读它的实际网络。不按命名规律猜——
# 猜错会让 onto-modeling 的 compose 以 declared as external, but could not be found 失败。
detect_consumer_network() {
    docker container inspect onto-agent-backend \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null | head -n 1
}

deploy_onto_modeling() {
    local dir="${INSTALL_ROOT}/onto-modeling" env_file="${INSTALL_ROOT}/onto-modeling/.env" network hub_token
    [ -f "${dir}/deploy.sh" ] || die "Onto Modeling 部署脚本不存在：${dir}/deploy.sh"
    [ -x "${dir}/deploy.sh" ] || chmod +x "${dir}/deploy.sh"
    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    if [ ! -f "${env_file}" ]; then
        [ -f "${dir}/.env.example" ] || die "Onto Modeling 发布包缺少 .env.example"
        cp "${dir}/.env.example" "${env_file}"
    fi
    chmod 600 "${env_file}"
    write_env_value "${env_file}" ONTO_WEB_PORT "${ONTO_WEB_PORT}"
    # 网络名由本脚本现场探测，是权威值：无条件写入，存量 .env 里失效的名字就地纠正
    write_env_value "${env_file}" ONTO_MODELING_EXTERNAL_AUTH_NETWORK "${network}"
    # consumer 的 jiuwenbox socket 卷：onto-modeling 只读挂载后，脚本运行验证在沙箱中执行；
    # 现场探测为准，探测不到写空值，验证改在 onto-modeling 容器内执行
    write_env_value "${env_file}" ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME \
        "$(container_mount_volume onto-agent-backend /run/jiuwenbox || true)"
    # 与独立部署的知识包 Hub 使用同一服务凭据；未部署 Hub 时保留原值（缺失由 Onto 部署脚本生成）
    hub_token="$(env_get "${INSTALL_ROOT}/knowledge-hub/.env" HUB_SERVICE_TOKEN)"
    [ -z "${hub_token}" ] || write_env_value "${env_file}" ONTO_HUB_SERVICE_TOKEN "${hub_token}"
    log "Onto Modeling 共享 Consumer 账号中心：网络 ${network}"
    bash "${dir}/deploy.sh"
    wait_for_onto_modeling
}

# Hub 是独立 compose 项目，排在 Onto Modeling 之前部署；服务凭据沿用已部署 Onto Modeling 的值，
# 两边都没有时由 Hub 部署脚本生成，随后 deploy_onto_modeling 对齐。值只经子进程环境传递，不打印。
deploy_knowledge_hub() {
    local dir="${INSTALL_ROOT}/knowledge-hub" network onto_token
    [ -f "${dir}/deploy.sh" ] || die "知识包 Hub 部署脚本不存在：${dir}/deploy.sh"
    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    onto_token="$(env_get "${INSTALL_ROOT}/onto-modeling/.env" ONTO_HUB_SERVICE_TOKEN)"
    log "部署知识包 Hub：${dir}（网络 ${network}）"
    (cd "${dir}" && CONSUMER_NETWORK="${network}" HUB_SERVICE_TOKEN="${onto_token}" bash ./deploy.sh) \
        || die "知识包 Hub 部署失败；修复后可单独执行 bash ${dir}/deploy.sh"
}

# Hub 容器名固定：已有容器不属于本安装根目录时，在任何变更之前拒绝接管
precheck_knowledge_hub() {
    local dir="${INSTALL_ROOT}/knowledge-hub" expected owner
    expected="$(cd "${dir}" 2>/dev/null && pwd || printf '%s' "${dir}")"
    if docker container inspect knowledge-hub >/dev/null 2>&1; then
        owner="$(container_working_dir knowledge-hub)"
        [ "${owner}" = "${expected}" ] \
            || die "容器 knowledge-hub 属于 ${owner:-未知目录}，不是 ${expected}，拒绝接管"
    fi
}

wait_for_onto_modeling() {
    local url="http://127.0.0.1:${ONTO_WEB_PORT}/api/health" elapsed=0
    until curl -fsS --noproxy '*' --max-time 5 "${url}" >/dev/null 2>&1; do
        [ "${elapsed}" -lt 120 ] || die "Onto Modeling 120 秒内未就绪：${url}；请查看 docker logs onto-modeling"
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log "Onto Modeling deployment finished: http://localhost:${ONTO_WEB_PORT}"
}

# Onto Modeling 与 Consumer、已有的 Modeling Workbench 同时运行，宿主端口不能与它们相同。
# 0.26.9.0 之前的分离部署 Modeling 仍在 5100，首次加入 Onto Modeling 时默认值会撞上。
onto_port_conflict() {
    local name
    for name in BACKEND_PORT MCP_SERVER_PORT FRONTEND_PORT MODELING_PORT MODELING_MCP_PORT; do
        if [ "${ONTO_WEB_PORT}" = "${!name}" ]; then
            printf '%s' "${name}"
            return 0
        fi
    done
    return 1
}

ensure_onto_web_port_free() {
    local name conflict
    while :; do
        conflict=""
        for name in BACKEND_PORT MCP_SERVER_PORT FRONTEND_PORT; do
            [ "${ONTO_WEB_PORT}" != "${!name}" ] || { conflict="${name}"; break; }
        done
        [ -n "${conflict}" ] || return 0
        printf 'Onto Modeling 宿主端口 %s 与 %s 相同，请换一个端口。\n' \
            "${ONTO_WEB_PORT}" "${conflict}" >/dev/tty
        ONTO_WEB_PORT="$(ask_port '请输入 Onto Modeling 宿主端口：')"
    done
}

mode="$(ask_choice '请选择模式（1=首次部署，2=升级部署，默认1）：' 1 '1 2')"
if [ "${mode}" = 1 ]; then
    MODE="install"
    root_choice="$(ask_choice '安装根目录（1=/opt/onto-agent-stack，2=自定义，默认1）：' 1 '1 2')"
    if [ "${root_choice}" = 1 ]; then INSTALL_ROOT=/opt/onto-agent-stack; else INSTALL_ROOT="$(ask '请输入自定义安装根目录：')"; fi
    PORT_MODE="$(ask_choice '端口配置（1=默认端口，2=自定义端口，默认1）：' 1 '1 2')"
    collect_ports
else
    MODE="upgrade"
    INSTALL_ROOT="$(ask '请输入已分离部署的安装根目录：')"
    [ -d "${INSTALL_ROOT}/onto-agent" ] || die 'Consumer 安装目录不存在'
    load_upgrade_ports
fi
CONSUMER_PACKAGE="$(resolve_package "$(ask '请输入 Consumer 发布包路径：')" Consumer发布包)"
AUTH_PACKAGE="$(resolve_package "$(ask '请输入独立 Auth 发布包路径：')" '独立 Auth 发布包')"
if [ "${MODE}" = install ] && [ ! -f "${INSTALL_ROOT}/onto-agent/deploy/state/auth-owner.json" ]; then
    log "首次安装将自动创建 ${AUTH_OWNER_DIR:-${INSTALL_ROOT}/independent-auth}/.env 并生成 Auth 私有凭据"
fi
ONTO_MODELING_PACKAGE="$(resolve_package "$(ask '请输入 Onto Modeling 发布包路径：')" 'Onto Modeling发布包')"
KNOWLEDGE_HUB_PACKAGE="$(resolve_package "$(ask '请输入知识包 Hub 发布包路径：')" '知识包 Hub发布包')"
EVALUATION_PACKAGE="$(resolve_package "$(ask '请输入独立评测服务发布包路径：')" '独立评测服务发布包')"
ensure_onto_web_port_free
if [ "${MODE}" = install ]; then
    action=()
else
    action=(upgrade)
fi
INSTALL_ROOT="${INSTALL_ROOT}" BACKEND_PORT="${BACKEND_PORT}" \
MCP_SERVER_PORT="${MCP_SERVER_PORT}" FRONTEND_PORT="${FRONTEND_PORT}" \
ONTO_WEB_PORT="${ONTO_WEB_PORT}" \
    bash "${SCRIPT_DIR}/install-consumer-modeling.sh" "${action[@]}" \
        --consumer-package "${CONSUMER_PACKAGE}" \
        --auth-package "${AUTH_PACKAGE}" \
        --onto-modeling-package "${ONTO_MODELING_PACKAGE}" \
        --knowledge-hub-package "${KNOWLEDGE_HUB_PACKAGE}" \
        --evaluation-package "${EVALUATION_PACKAGE}"
