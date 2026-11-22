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

# 与 Consumer 的宿主端口不能冲突；旧 Core 端口由非交互入口复查。
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
