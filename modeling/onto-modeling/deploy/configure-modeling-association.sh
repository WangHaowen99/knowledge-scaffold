#!/bin/bash
# 为同机部署配置导入项目的 MCP 关联；不修改全局 Modeling URL、鉴权或网络。
set -euo pipefail

association_env_get() {
    local value
    value="$(sed -n "s/^$2=//p" "$1" | tail -n 1)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

configure_modeling_association() {
    local default_url="${2:-}" env_file="$1/.env"
    local container="${ONTO_MODELING_MIGRATE_FROM:-mw-modeling}" token association_token url port gateway temp_file
    [ -f "${env_file}" ] || { umask 077; touch "${env_file}"; }
    url="$(association_env_get "${env_file}" ONTO_WEB_MODELING_ASSOCIATION_URL)"
    association_token="$(association_env_get "${env_file}" ONTO_WEB_MODELING_ASSOCIATION_TOKEN)"
    if [ -n "${url}" ] || [ -n "${association_token}" ]; then
        [ -n "${url}" ] && [ -n "${association_token}" ] || {
            echo 'ONTO_WEB_MODELING_ASSOCIATION_URL 和 TOKEN 必须同时配置，拒绝拼接不同服务的配置。' >&2
            return 1
        }
        echo 'Modeling MCP 关联配置已存在，保留原地址和令牌。'
        return 0
    fi
    # 已有全局地址可能指向远端且使用用户 JWT；不要把本机服务令牌强行配给它。
    if [ -n "$(association_env_get "${env_file}" ONTO_WEB_MODELING_URL)" ]; then
        echo '检测到已有全局 Modeling 地址，保留原有用户鉴权配置。'
        return 0
    fi
    # 整栈部署传入的地址与当前 Modeling 容器属于同一部署；独立部署则探测本机地址。
    if ! docker container inspect "${container}" >/dev/null 2>&1; then
        echo '未检测到 Modeling，保留独立 Onto 部署；部署 Modeling 后重跑部署脚本可自动配置 MCP 关联。'
        return 0
    fi
    token="$(docker container inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${container}" \
        | sed -n 's/^MODELING_SERVICE_TOKEN=//p' | tail -n 1)"
    if [ "${#token}" -lt 32 ]; then
        echo 'Modeling 服务令牌未就绪，请先使用项目部署脚本部署 Modeling。' >&2
        return 1
    fi
    url="${default_url}"
    if [ -z "${url}" ]; then
        port="$(docker container inspect --format '{{with index .NetworkSettings.Ports "5100/tcp"}}{{(index . 0).HostPort}}{{end}}' "${container}")"
        [ -n "${port}" ] || { echo 'Modeling 未发布 HTTP 端口，无法配置独立部署的 MCP 关联。' >&2; return 1; }
        if docker info --format '{{.OperatingSystem}}' | grep -qi 'Docker Desktop'; then
            gateway=host.docker.internal
        else
            gateway="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
        fi
        [ -n "${gateway}" ] || { echo '无法确定 Modeling 的宿主访问地址。' >&2; return 1; }
        url="http://${gateway}:${port}"
    fi
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
    chmod 600 "${temp_file}"
    # 不输出或传递令牌到子进程的命令行；实际值仅写入部署机的 .env。
    sed '/^ONTO_WEB_MODELING_ASSOCIATION_URL=/d; /^ONTO_WEB_MODELING_ASSOCIATION_TOKEN=/d' "${env_file}" > "${temp_file}"
    printf 'ONTO_WEB_MODELING_ASSOCIATION_URL=%s\nONTO_WEB_MODELING_ASSOCIATION_TOKEN=%s\n' "${url}" "${token}" >> "${temp_file}"
    mv "${temp_file}" "${env_file}"
    echo 'Modeling MCP 自动关联配置已保存（令牌不输出）。'
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    configure_modeling_association "${1:?Usage: configure-modeling-association.sh <onto-directory> [modeling-url]}" "${2:-}"
fi
