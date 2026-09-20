#!/bin/bash
set -euo pipefail

CONSUMER_PACKAGE=""
MODELING_PACKAGE=""
UPGRADE_FROM=""
INSTALL_ROOT_EXPLICIT=0
[ -n "${INSTALL_ROOT+x}" ] && INSTALL_ROOT_EXPLICIT=1
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

升级旧的 Consumer、Modeling 合并部署根目录：
  bash install-consumer-modeling.sh \
    --upgrade-from <旧合并部署根目录> \
    --consumer-package <Consumer发布包> \
    --modeling-package <Modeling发布包>

必填参数：
  --consumer-package FILE  Consumer 发布包路径；支持通配符，但必须唯一匹配
  --modeling-package FILE  Modeling 发布包路径；支持通配符，但必须唯一匹配

可选参数：
  --upgrade-from DIR       旧合并部署根目录；目录不存在时按首次安装处理

可选环境变量：
  INSTALL_ROOT           安装根目录，默认 /opt/onto-agent-stack
                         升级时默认在旧合并部署旁创建独立 Modeling 目录
  INSTALL_GLOBAL_OPSCTL  是否安装全局 opsctl，默认 0

端口变量可直接放在命令前传入：
  BACKEND_PORT MCP_SERVER_PORT FRONTEND_PORT MODELING_PORT MODELING_MCP_PORT

示例：
  INSTALL_ROOT=/data/onto-agent-stack \
  BACKEND_PORT=45055 MCP_SERVER_PORT=47488 FRONTEND_PORT=48888 \
  MODELING_PORT=55100 MODELING_MCP_PORT=55188 \
  bash install-consumer-modeling.sh \
    --consumer-package /path/to/onto-agent-linux-amd64-v2.tar.gz \
    --modeling-package '/path/to/modeling-*.tar.gz'

  INSTALL_ROOT=/data/onto-agent-stack \
  bash install-consumer-modeling.sh \
    --upgrade-from /data/onto-agent \
    --consumer-package /path/to/onto-agent-linux-amd64-v2.tar.gz \
    --modeling-package '/path/to/modeling-*.tar.gz'
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
        --upgrade-from)
            [ "$#" -ge 2 ] || die "--upgrade-from 缺少旧合并部署根目录"
            UPGRADE_FROM="$2"
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
if [ -n "${UPGRADE_FROM}" ]; then
    case "${UPGRADE_FROM}" in
        /*) ;;
        *) UPGRADE_FROM="$(pwd)/${UPGRADE_FROM}" ;;
    esac
    if [ "${INSTALL_ROOT_EXPLICIT}" -eq 0 ]; then
        case "$(basename "${UPGRADE_FROM}")" in
            onto-agent)
                INSTALL_ROOT="$(dirname "${UPGRADE_FROM}")"
                ;;
            *)
                INSTALL_ROOT="${UPGRADE_FROM}"
                ;;
        esac
    fi
fi

resolve_package_path() {
    package_pattern="$1"
    package_label="$2"
    case "${package_pattern}" in
        *\**|*\?*|*\[* )
            shopt -s nullglob
            matches=( ${package_pattern} )
            shopt -u nullglob
            case "${#matches[@]}" in
                0) die "${package_label}通配符未匹配到文件：${package_pattern}" ;;
                1) package_pattern="${matches[0]}" ;;
                *) die "${package_label}通配符匹配到多个文件，请改为唯一匹配：${package_pattern}" ;;
            esac
            ;;
    esac
    printf '%s\n' "${package_pattern}"
}

CONSUMER_PACKAGE="$(resolve_package_path "${CONSUMER_PACKAGE}" Consumer发布包)"
MODELING_PACKAGE="$(resolve_package_path "${MODELING_PACKAGE}" Modeling发布包)"

if [ -n "${UPGRADE_FROM}" ] && [ ! -e "${UPGRADE_FROM}" ]; then
    [ ! -L "${UPGRADE_FROM}" ] || die "--upgrade-from 不能指向符号链接：${UPGRADE_FROM}"
    log "旧合并部署根目录不存在，按首次安装处理：${UPGRADE_FROM}"
    UPGRADE_FROM=""
fi

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

env_get() {
    env_file="$1"
    key="$2"
    value="$([ -f "${env_file}" ] \
        && sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "${env_file}" | tail -n 1 \
        || true)"
    value="${value%$'\r'}"
    value="$(printf '%s' "${value}" \
        | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//")"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    printf '%s' "${value}"
}

copy_env_key() {
    source_file="$1"
    target_file="$2"
    key="$3"
    [ -f "${source_file}" ] || return 0
    line="$(sed -n "/^[[:space:]]*${key}[[:space:]]*=/p" "${source_file}" | tail -n 1)"
    [ -n "${line}" ] || return 0
    write_env_value "${target_file}" "${key}" "${line#*=}"
}

container_mount_volume() {
    container_name="$1"
    destination="$2"
    docker container inspect "${container_name}" \
        --format '{{range .Mounts}}{{if eq .Destination "'"${destination}"'"}}{{println .Name}}{{end}}{{end}}' \
        2>/dev/null | head -n 1
}

legacy_host_port() {
    container_name="$1"
    container_port="$2"
    docker port "${container_name}" "${container_port}/tcp" 2>/dev/null \
        | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1
}

validate_legacy_modeling_container() {
    container_name="$1"
    [ -n "${container_name}" ] || return 0
    if ! docker container inspect "${container_name}" >/dev/null 2>&1; then
        return 0
    fi
    working_dir="$(docker container inspect "${container_name}" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
        2>/dev/null || true)"
    [ "${working_dir}" = "${UPGRADE_FROM}" ] || die \
        "容器 ${container_name} 不属于旧合并部署根目录 ${UPGRADE_FROM}，拒绝接管"
}

resolve_legacy_volume() {
    container_name="$1"
    expected_volume="$2"
    resolved_volume=""
    docker container inspect "${container_name}" >/dev/null 2>&1 || die \
        "旧 Modeling 容器不存在：${container_name}；无法确认数据卷归属"
    resolved_volume="$(container_mount_volume "${container_name}" /data)"
    resolved_volume="${resolved_volume:-${expected_volume}}"
    [ "${resolved_volume}" = "${expected_volume}" ] || die \
        "旧容器 ${container_name} 使用数据卷 ${resolved_volume}，预期为 ${expected_volume}，拒绝自动迁移"
    docker volume inspect "${resolved_volume}" >/dev/null 2>&1 || die \
        "旧 Modeling 数据卷不存在：${resolved_volume}"
    printf '%s' "${resolved_volume}"
}

backup_docker_volume() {
    volume_name="$1"
    image_name="$2"
    archive_name="$3"
    backup_dir="$4"
    host_uid="$(id -u)"
    host_gid="$(id -g)"
    docker image inspect "${image_name}" >/dev/null 2>&1 || die \
        "备份数据卷需要旧镜像，但镜像不存在：${image_name}"
    docker run --rm --network=none --user 0:0 --entrypoint /bin/sh \
        -v "${volume_name}:/source:ro" \
        -v "${backup_dir}:/backup:rw" \
        "${image_name}" -c \
        'tar -C /source -czf "/backup/$1" . && chmod 600 "/backup/$1" && chown "$2:$3" "/backup/$1"' \
        backup-volume "${archive_name}" "${host_uid}" "${host_gid}" \
        || die "备份数据卷失败：${volume_name}"
}

upgrade_install() {
    legacy_stack_dir="$(cd "${UPGRADE_FROM}" 2>/dev/null && pwd)" \
        || die "旧合并部署根目录不存在：${UPGRADE_FROM}"
    UPGRADE_FROM="${legacy_stack_dir}"
    CONSUMER_DIR="${UPGRADE_FROM}"
    MODELING_DIR="${INSTALL_ROOT}/modeling"

    [ -f "${CONSUMER_DIR}/VERSION" ] || die "旧合并部署根目录缺少 VERSION：${CONSUMER_DIR}"
    [ -f "${CONSUMER_DIR}/backend/.env" ] || die "旧合并部署根目录缺少 backend/.env"
    [ -f "${CONSUMER_DIR}/deploy/state/current.env" ] || die \
        "旧合并部署根目录缺少部署状态，无法执行受控升级"
    [ ! -e "${MODELING_DIR}" ] || die \
        "独立 Modeling 目录已存在：${MODELING_DIR}；请确认现状后再升级"
    if [ -e "${INSTALL_ROOT}/onto-agent" ] \
        && [ "$(cd "${INSTALL_ROOT}/onto-agent" 2>/dev/null && pwd)" != "${CONSUMER_DIR}" ]; then
        die "目标根目录已有另一套 Consumer：${INSTALL_ROOT}/onto-agent"
    fi

    legacy_token="$(env_get "${CONSUMER_DIR}/backend/.env" MODELING_SERVICE_TOKEN)"
    [ "${#legacy_token}" -ge 32 ] || die \
        "旧部署未配置有效的 MODELING_SERVICE_TOKEN，无法保证升级后服务认证一致"

    legacy_backend_port="$(legacy_host_port onto-agent-backend 5055 || true)"
    legacy_mcp_port="$(legacy_host_port onto-agent-mcp 7488 || true)"
    legacy_frontend_port="$(legacy_host_port onto-agent-frontend 80 || true)"

    for container_name in mw-modeling mw-modeling-agent mw-modeling-nl2sql; do
        validate_legacy_modeling_container "${container_name}"
    done

    modeling_volume="$(resolve_legacy_volume mw-modeling mw-modeling_modeling-data)"
    agent_volume="$(resolve_legacy_volume mw-modeling-agent mw-modeling_modeling-agent-data)"
    modeling_image="$(docker container inspect mw-modeling --format '{{.Config.Image}}' 2>/dev/null || true)"
    agent_image="$(docker container inspect mw-modeling-agent --format '{{.Config.Image}}' 2>/dev/null || true)"
    if [ -z "${modeling_image}" ]; then
        modeling_image="$(sed -n \
            's/^[[:space:]]*"modeling_image":[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${CONSUMER_DIR}/deploy/manifest.json" | head -n 1)"
    fi
    if [ -z "${agent_image}" ]; then
        agent_image="$(sed -n \
            's/^[[:space:]]*"modeling_agent_image":[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${CONSUMER_DIR}/deploy/manifest.json" | head -n 1)"
    fi
    [ -n "${modeling_image}" ] || die "无法确定旧 Modeling 镜像"
    [ -n "${agent_image}" ] || die "无法确定旧 Modeling Agent 镜像"

    mkdir -p "${INSTALL_ROOT}"
    stage_dir="$(mktemp -d "${INSTALL_ROOT}/.consumer-modeling-upgrade.XXXXXX")" \
        || die "无法创建升级暂存目录"
    cleanup_upgrade_stage() {
        if [ -n "${stage_dir:-}" ] && [ -d "${stage_dir}" ]; then
            find "${stage_dir}" -mindepth 1 -delete 2>/dev/null || true
            rmdir "${stage_dir}" 2>/dev/null || true
        fi
    }
    trap cleanup_upgrade_stage EXIT
    backup_dir="${INSTALL_ROOT}/backups/consumer-modeling-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "${backup_dir}"
    chmod 700 "${backup_dir}"

    log "解压新发布包到升级暂存目录"
    tar -xzf "${CONSUMER_PACKAGE}" -C "${stage_dir}"
    tar -xzf "${MODELING_PACKAGE}" -C "${stage_dir}"
    [ -x "${stage_dir}/onto-agent/deploy/upgrade.sh" ] || die "Consumer 发布包缺少 upgrade.sh"
    [ -x "${stage_dir}/modeling/deploy/install.sh" ] || die "Modeling 发布包缺少 install.sh"

    tar -C "${CONSUMER_DIR}" -czf "${backup_dir}/legacy-config.tar.gz" \
        VERSION backend/.env deploy/manifest.json deploy/modeling 2>/dev/null \
        || die "备份旧部署配置失败"
    chmod 600 "${backup_dir}/legacy-config.tar.gz"

    stopped_containers=""
    modeling_handoff_started=false
    restore_stopped_containers() {
        [ "${modeling_handoff_started}" = false ] || return 0
        for container_name in ${stopped_containers}; do
            docker start "${container_name}" >/dev/null 2>&1 || true
        done
    }
    trap 'restore_stopped_containers; cleanup_upgrade_stage' EXIT
    for container_name in mw-modeling-nl2sql mw-modeling mw-modeling-agent; do
        running="$(docker container inspect "${container_name}" \
            --format '{{.State.Running}}' 2>/dev/null || true)"
        if [ "${running}" = true ]; then
            docker stop "${container_name}" >/dev/null || die "停止旧容器失败：${container_name}"
            stopped_containers="${stopped_containers} ${container_name}"
        fi
    done

    log "备份旧 Modeling 数据卷，卷名保持不变"
    backup_docker_volume "${modeling_volume}" "${modeling_image}" \
        modeling-data.tar.gz "${backup_dir}"
    backup_docker_volume "${agent_volume}" "${agent_image}" \
        modeling-agent-data.tar.gz "${backup_dir}"

    log "原地升级 Consumer：${CONSUMER_DIR}"
    [ -z "${BACKEND_PORT:-${legacy_backend_port}}" ] \
        || write_env_value "${CONSUMER_DIR}/backend/.env" BACKEND_PORT \
            "${BACKEND_PORT:-${legacy_backend_port}}"
    [ -z "${MCP_SERVER_PORT:-${legacy_mcp_port}}" ] \
        || write_env_value "${CONSUMER_DIR}/backend/.env" MCP_SERVER_PORT \
            "${MCP_SERVER_PORT:-${legacy_mcp_port}}"
    [ -z "${FRONTEND_PORT:-${legacy_frontend_port}}" ] \
        || write_env_value "${CONSUMER_DIR}/backend/.env" FRONTEND_PORT \
            "${FRONTEND_PORT:-${legacy_frontend_port}}"
    if ! bash "${stage_dir}/onto-agent/deploy/upgrade.sh" "${CONSUMER_DIR}"; then
        die "Consumer 升级失败；旧 Modeling 容器已尝试恢复，备份位于 ${backup_dir}"
    fi

    upgraded_token="$(env_get "${CONSUMER_DIR}/backend/.env" MODELING_SERVICE_TOKEN)"
    [ "${upgraded_token}" = "${legacy_token}" ] || die \
        "Consumer 升级后 MODELING_SERVICE_TOKEN 发生变化，拒绝继续 Modeling 交接"

    mv "${stage_dir}/modeling" "${MODELING_DIR}"
    cp "${MODELING_DIR}/.env.example" "${MODELING_DIR}/.env"
    chmod 600 "${MODELING_DIR}/.env"

    for source_env in "${CONSUMER_DIR}/.env" "${CONSUMER_DIR}/backend/.env"; do
        [ -f "${source_env}" ] || continue
        for key in $(sed -n 's/^[[:space:]]*\(MODELING_[A-Z0-9_]*\)[[:space:]]*=.*/\1/p' "${source_env}" | sort -u); do
            copy_env_key "${source_env}" "${MODELING_DIR}/.env" "${key}"
        done
        for key in EVAL_DB_URL NL2SQL_TOKEN AUTH_ISSUER_URL AUTH_JWT_ISSUER \
                   AUTH_JWT_AUDIENCE CONSUMER_API_URL; do
            copy_env_key "${source_env}" "${MODELING_DIR}/.env" "${key}"
        done
    done
    write_env_value "${MODELING_DIR}/.env" MODELING_SERVICE_TOKEN "${legacy_token}"
    legacy_modeling_port="$(legacy_host_port mw-modeling 5100 || true)"
    legacy_modeling_mcp_port="$(legacy_host_port mw-modeling 5188 || true)"
    [ -n "${legacy_modeling_port}" ] \
        && write_env_value "${MODELING_DIR}/.env" MODELING_PORT "${legacy_modeling_port}"
    [ -n "${legacy_modeling_mcp_port}" ] \
        && write_env_value "${MODELING_DIR}/.env" MODELING_MCP_PORT "${legacy_modeling_mcp_port}"
    [ -z "${MODELING_PORT:-}" ] \
        || write_env_value "${MODELING_DIR}/.env" MODELING_PORT "${MODELING_PORT}"
    [ -z "${MODELING_MCP_PORT:-}" ] \
        || write_env_value "${MODELING_DIR}/.env" MODELING_MCP_PORT "${MODELING_MCP_PORT}"

    modeling_handoff_started=true
    for container_name in mw-modeling-nl2sql mw-modeling mw-modeling-agent; do
        if docker container inspect "${container_name}" >/dev/null 2>&1; then
            docker rm "${container_name}" >/dev/null || die "删除旧容器失败：${container_name}"
        fi
    done

    log "部署独立 Modeling，并复用 ${modeling_volume} 和 ${agent_volume}"
    if ! (cd "${MODELING_DIR}" && bash deploy/install.sh); then
        die "Modeling 部署失败；旧数据卷和备份均已保留，备份位于 ${backup_dir}"
    fi

    [ "$(container_mount_volume mw-modeling /data)" = "${modeling_volume}" ] || die \
        "升级后 Modeling 未挂载原数据卷"
    [ "$(container_mount_volume mw-modeling-agent /data)" = "${agent_volume}" ] || die \
        "升级后 Modeling Agent 未挂载原数据卷"

    cleanup_upgrade_stage
    trap - EXIT
    log "Consumer 与 Modeling 分离升级完成"
    printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
    printf '[full-install] Modeling：%s\n' "${MODELING_DIR}"
    printf '[full-install] 升级备份：%s\n' "${backup_dir}"
}

validate_package "${CONSUMER_PACKAGE}" onto-agent
validate_package "${MODELING_PACKAGE}" modeling

if [ -n "${UPGRADE_FROM}" ]; then
    upgrade_install
    exit 0
fi

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
