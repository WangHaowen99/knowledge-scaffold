#!/bin/bash
set -euo pipefail

CONSUMER_PACKAGE=""
MODELING_PACKAGE=""
INSTALL_ROOT="${INSTALL_ROOT:-/opt/onto-agent-stack}"
INSTALL_GLOBAL_OPSCTL="${INSTALL_GLOBAL_OPSCTL:-0}"

log() {
    printf '[full-install] %s\n' "$*"
}

die() {
    printf '[full-install][error] %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'HELP'
用法：
  bash install-consumer-modeling.sh \
    --consumer-package <Consumer发布包> \
    --modeling-package <Modeling发布包>

必填参数：
  --consumer-package FILE  Consumer 发布包路径
  --modeling-package FILE  Modeling 发布包路径

可选环境变量：
  INSTALL_ROOT           安装根目录，默认 /opt/onto-agent-stack
  INSTALL_GLOBAL_OPSCTL  是否安装全局 opsctl，默认 0

端口变量可直接放在命令前传入：
  BACKEND_PORT MCP_SERVER_PORT FRONTEND_PORT MODELING_PORT MODELING_MCP_PORT

示例：
  INSTALL_ROOT=/data/onto-agent-stack \
  BACKEND_PORT=45055 MCP_SERVER_PORT=47488 FRONTEND_PORT=48888 \
  MODELING_PORT=55100 MODELING_MCP_PORT=55188 \
  bash install-consumer-modeling.sh \
    --consumer-package /path/to/onto-agent-linux-amd64-v2.tar.gz \
    --modeling-package /path/to/modeling-linux-amd64.tar.gz
HELP
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --consumer-package)
            [ "$#" -ge 2 ] || die "--consumer-package 缺少文件路径"
            CONSUMER_PACKAGE="$2"
            shift 2
            ;;
        --modeling-package)
            [ "$#" -ge 2 ] || die "--modeling-package 缺少文件路径"
            MODELING_PACKAGE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1"
            ;;
    esac
done

[ -n "${CONSUMER_PACKAGE}" ] || { usage >&2; die "必须指定 --consumer-package"; }
[ -n "${MODELING_PACKAGE}" ] || { usage >&2; die "必须指定 --modeling-package"; }

case "${CONSUMER_PACKAGE}" in
    /*) ;;
    *) CONSUMER_PACKAGE="$(pwd)/${CONSUMER_PACKAGE}" ;;
esac
case "${MODELING_PACKAGE}" in
    /*) ;;
    *) MODELING_PACKAGE="$(pwd)/${MODELING_PACKAGE}" ;;
esac

CONSUMER_DIR="${INSTALL_ROOT}/onto-agent"
MODELING_DIR="${INSTALL_ROOT}/modeling"

[ "$(uname -s)" = "Linux" ] || die "仅支持 Linux"
[ "${BASH_VERSINFO[0]}" -gt 3 ] \
    || { [ "${BASH_VERSINFO[0]}" -eq 3 ] && [ "${BASH_VERSINFO[1]}" -ge 2 ]; } \
    || die "需要 Bash 3.2 或更高版本"

for command_name in docker curl sha256sum flock tar gzip od awk sed tr mktemp; do
    command -v "${command_name}" >/dev/null 2>&1 \
        || die "缺少命令：${command_name}"
done
docker info >/dev/null 2>&1 || die "Docker daemon 不可用"

validate_package() {
    package="$1"
    top_dir="$2"
    [ -f "${package}" ] || die "发布包不存在：${package}"
    [ ! -L "${package}" ] || die "发布包不能是符号链接：${package}"
    tar -tzf "${package}" >/dev/null || die "发布包不是有效的 tar.gz：${package}"
    tar -tzf "${package}" | awk -v top="${top_dir}" '
        BEGIN { valid = 1 }
        {
            path = $0
            if (path == top || path == top "/") next
            if (index(path, top "/") != 1 || path ~ /(^|\/)\.\.?(\/|$)/ || path ~ /^\//) {
                valid = 0
                exit
            }
        }
        END { exit valid ? 0 : 1 }
    ' || die "发布包目录结构非法：${package}"
}

write_env_value() {
    env_file="$1"
    key="$2"
    value="$3"
    temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || die "无法更新 ${env_file}"
    chmod 600 "${temp_file}"
    if ! awk -v key="${key}" -v value="${value}" '
        BEGIN { updated = 0 }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            if (!updated) {
                print key "=" value
                updated = 1
            }
            next
        }
        { print }
        END {
            if (!updated) print key "=" value
        }
    ' "${env_file}" > "${temp_file}"; then
        command rm -f -- "${temp_file}"
        die "无法更新 ${env_file}"
    fi
    mv "${temp_file}" "${env_file}"
    chmod 600 "${env_file}"
}

generate_hex_secret() {
    byte_count="$1"
    secret="$(LC_ALL=C od -An -N "${byte_count}" -tx1 /dev/urandom | tr -d '[:space:]')" \
        || die "生成随机值失败"
    case "${secret}" in
        ''|*[!0-9a-f]*) die "随机源返回了非法值" ;;
    esac
    printf '%s' "${secret}"
}

validate_package "${CONSUMER_PACKAGE}" onto-agent
validate_package "${MODELING_PACKAGE}" modeling

[ ! -e "${CONSUMER_DIR}" ] \
    || die "Consumer 安装目录已存在：${CONSUMER_DIR}；本脚本只用于首次安装"
[ ! -e "${MODELING_DIR}" ] \
    || die "Modeling 安装目录已存在：${MODELING_DIR}；本脚本只用于首次安装"

mkdir -p "${INSTALL_ROOT}"
log "解压 Consumer 发布包"
tar -xzf "${CONSUMER_PACKAGE}" -C "${INSTALL_ROOT}"
log "解压 Modeling 发布包"
tar -xzf "${MODELING_PACKAGE}" -C "${INSTALL_ROOT}"

[ -f "${CONSUMER_DIR}/backend/.env.example" ] \
    || die "Consumer 发布包缺少 backend/.env.example"
[ -f "${MODELING_DIR}/.env.example" ] \
    || die "Modeling 发布包缺少 .env.example"

cp "${CONSUMER_DIR}/backend/.env.example" "${CONSUMER_DIR}/backend/.env"
cp "${MODELING_DIR}/.env.example" "${MODELING_DIR}/.env"
chmod 600 "${CONSUMER_DIR}/backend/.env" "${MODELING_DIR}/.env"

MODELING_SERVICE_TOKEN_VALUE="$(generate_hex_secret 32)"
AUTH_BOOTSTRAP_ADMIN_PASSWORD_VALUE="Aa1!$(generate_hex_secret 8)"
trap 'unset MODELING_SERVICE_TOKEN_VALUE AUTH_BOOTSTRAP_ADMIN_PASSWORD_VALUE' EXIT

write_env_value \
    "${CONSUMER_DIR}/backend/.env" \
    MODELING_SERVICE_TOKEN \
    "${MODELING_SERVICE_TOKEN_VALUE}"
write_env_value \
    "${MODELING_DIR}/.env" \
    MODELING_SERVICE_TOKEN \
    "${MODELING_SERVICE_TOKEN_VALUE}"
write_env_value \
    "${CONSUMER_DIR}/backend/.env" \
    AUTH_BOOTSTRAP_ADMIN_PASSWORD \
    "${AUTH_BOOTSTRAP_ADMIN_PASSWORD_VALUE}"

log "共享 Modeling Token 和管理员初始密码已生成并写入配置，值不会打印"

log "开始安装 Consumer"
cd "${CONSUMER_DIR}"
if [ "${INSTALL_GLOBAL_OPSCTL}" = "1" ]; then
    bash deploy/install.sh --install-global-opsctl
elif [ "${INSTALL_GLOBAL_OPSCTL}" = "0" ]; then
    bash deploy/install.sh
else
    die "INSTALL_GLOBAL_OPSCTL 只能是 0 或 1"
fi

log "Consumer 安装成功，开始安装 Modeling"
cd "${MODELING_DIR}"
bash deploy/install.sh

log "Consumer 与 Modeling 安装完成"
printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
printf '[full-install] Modeling：%s\n' "${MODELING_DIR}"
printf '[full-install] 管理员初始密码保存在：%s\n' "${CONSUMER_DIR}/backend/.env"
