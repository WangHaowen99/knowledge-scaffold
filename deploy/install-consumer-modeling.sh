#!/bin/bash
set -euo pipefail

if [ "${1:-}" = "cce" ]; then
    shift
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "${script_dir}/joint-cce.py" ]; then
        exec python3 -B "${script_dir}/joint-cce.py" "$@"
    fi
    exec python3 -B "${script_dir}/../modeling/deploy/cce/joint-cce.py" "$@"
fi

CONSUMER_PACKAGE=""
MODELING_PACKAGE=""
ONTO_MODELING_PACKAGE=""
KNOWLEDGE_HUB_PACKAGE=""
EVALUATION_PACKAGE=""
UPGRADE_FROM=""
COMMAND="install"
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
    --modeling-package <Modeling发布包> \
    [--knowledge-hub-package <知识包 Hub发布包>] \
    [--onto-modeling-package <Onto Modeling发布包>] \
    [--evaluation-package <独立评测服务发布包>]

在已运行的 Consumer 旁部署或升级知识包 Hub、Onto Modeling、独立评测服务
（不安装、不升级、不重启 Consumer，也不修改其配置）：
  bash install-consumer-modeling.sh \
    [--knowledge-hub-package <知识包 Hub发布包>] \
    [--onto-modeling-package <Onto Modeling发布包>] \
    [--evaluation-package <独立评测服务发布包>]

升级已经分离部署的 Consumer 和 Modeling：
  INSTALL_ROOT=/opt/onto-agent-stack \
  bash install-consumer-modeling.sh upgrade \
    --consumer-package <Consumer发布包> \
    --modeling-package <Modeling发布包> \
    [--knowledge-hub-package <知识包 Hub发布包>] \
    [--onto-modeling-package <Onto Modeling发布包>] \
    [--evaluation-package <独立评测服务发布包>]

升级旧的 Consumer、Modeling 合并部署根目录：
  bash install-consumer-modeling.sh \
    --upgrade-from <旧合并部署根目录> \
    --consumer-package <Consumer发布包> \
    --modeling-package <Modeling发布包> \
    [--knowledge-hub-package <知识包 Hub发布包>] \
    [--onto-modeling-package <Onto Modeling发布包>] \
    [--evaluation-package <独立评测服务发布包>]

安装或升级 Consumer 时必填（两者都不指定时沿用已运行的 Consumer，只部署下列可选组件）：
  --consumer-package FILE  Consumer 发布包路径；支持通配符，但必须唯一匹配
  --modeling-package FILE  Modeling 发布包路径；支持通配符，但必须唯一匹配

可选参数：
  --knowledge-hub-package FILE
                           知识包 Hub 发布包路径；支持通配符，但必须唯一匹配。
                           不指定则不部署 Hub；指定后在 Consumer、Modeling 之后、
                           Onto Modeling 之前部署到 INSTALL_ROOT/knowledge-hub，
                           加入 Consumer 网络并与 Onto Modeling 同步服务凭据
                           选择 Onto Modeling 时，未提供此参数则要求已有 Hub 就绪；
                           否则预检停止并提示补齐同版本 Hub 发布包
  --onto-modeling-package FILE
                           Onto Modeling 发布包路径；支持通配符，但必须唯一匹配。
                           不指定则不部署 Onto Modeling；指定后在 Consumer、Modeling
                           之后部署到 INSTALL_ROOT/onto-modeling，共享 Consumer 账号中心
  --upgrade-from DIR       旧合并部署根目录；目录不存在时按首次安装处理
  --evaluation-package FILE
                           eval-service/build.sh 生成的发布包（evaluation-*.tar.gz）；
                           支持通配符，但必须唯一匹配；也接受完整离线包中的 eval-service 目录。
                           不指定则跳过。
                           在 Hub、Onto Modeling 之后部署到 INSTALL_ROOT/eval-service，
                           复用 Consumer 网络和评测凭据，保留已有加密密钥及运行配置

可选环境变量：
  INSTALL_ROOT           安装根目录，默认 /opt/onto-agent-stack
                         升级时默认在旧合并部署旁创建独立 Modeling 目录
  INSTALL_GLOBAL_OPSCTL  是否安装全局 opsctl，默认 0

端口变量可直接放在命令前传入：
  BACKEND_PORT MCP_SERVER_PORT FRONTEND_PORT MODELING_PORT MODELING_MCP_PORT
  ONTO_WEB_PORT（Onto Modeling；默认沿用已部署的值，首次为发布包默认值）
  EVAL_PORT / EVAL_BIND_ADDRESS（评测服务；默认 8090 / 0.0.0.0）
  EVAL_ENV_FILE（可选外部评测运行配置，必须自行配齐且只读沿用）

示例：
  INSTALL_ROOT=/data/onto-agent-stack \
  BACKEND_PORT=45055 MCP_SERVER_PORT=47488 FRONTEND_PORT=48888 \
  MODELING_PORT=55100 MODELING_MCP_PORT=55188 \
  bash install-consumer-modeling.sh \
    --consumer-package /path/to/onto-agent-linux-amd64-v2.tar.gz \
    --modeling-package '/path/to/modeling-*.tar.gz'

  INSTALL_ROOT=/data/onto-agent-stack \
  bash install-consumer-modeling.sh upgrade \
    --consumer-package '/path/to/onto-agent-*.tar.gz' \
    --modeling-package '/path/to/modeling-*.tar.gz' \
    --knowledge-hub-package '/path/to/knowledge-hub-*.tar.gz' \
    --onto-modeling-package '/path/to/onto-modeling-*.tar.gz'

  INSTALL_ROOT=/data/onto-agent-stack \
  bash install-consumer-modeling.sh \
    --upgrade-from /data/onto-agent \
    --consumer-package /path/to/onto-agent-linux-amd64-v2.tar.gz \
    --modeling-package '/path/to/modeling-*.tar.gz'
HELP
}

if [ "${1:-}" = "upgrade" ]; then
    COMMAND="upgrade"
    shift
fi

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
        --onto-modeling-package)
            [ "$#" -ge 2 ] || die "--onto-modeling-package 缺少文件路径"
            ONTO_MODELING_PACKAGE="$2"
            shift 2
            ;;
        --knowledge-hub-package)
            [ "$#" -ge 2 ] || die "--knowledge-hub-package 缺少文件路径"
            KNOWLEDGE_HUB_PACKAGE="$2"
            shift 2
            ;;
        --evaluation-package)
            [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] \
                || die "--evaluation-package 缺少发布包路径"
            EVALUATION_PACKAGE="$2"
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

if [ "${COMMAND}" = "upgrade" ] && [ -n "${UPGRADE_FROM}" ]; then
    die "upgrade 命令用于升级已经分离的部署，不能同时使用 --upgrade-from"
fi

# 不指定 Consumer 发布包时沿用已运行的 Consumer，只部署 Hub、Onto Modeling 和评测
ATTACH_ONLY=0
if [ -z "${CONSUMER_PACKAGE}" ]; then
    [ -z "${UPGRADE_FROM}" ] || die "--upgrade-from 需要同时指定 --consumer-package"
    [ -z "${MODELING_PACKAGE}" ] \
        || die "Modeling 需与 Consumer 一起安装或升级；只部署 Hub、Onto Modeling 或评测时不要指定 --modeling-package"
    [ -n "${KNOWLEDGE_HUB_PACKAGE}${ONTO_MODELING_PACKAGE}${EVALUATION_PACKAGE}" ] \
        || { usage >&2; die "必须指定 --consumer-package，或至少指定一个 Hub、Onto Modeling、评测发布包"; }
    ATTACH_ONLY=1
else
    [ -n "${MODELING_PACKAGE}" ] || { usage >&2; die "必须指定 --modeling-package"; }
fi

case "${CONSUMER_PACKAGE}" in
    ''|/*) ;;
    *) CONSUMER_PACKAGE="$(pwd)/${CONSUMER_PACKAGE}" ;;
esac
case "${MODELING_PACKAGE}" in
    ''|/*) ;;
    *) MODELING_PACKAGE="$(pwd)/${MODELING_PACKAGE}" ;;
esac
case "${ONTO_MODELING_PACKAGE}" in
    ''|/*) ;;
    *) ONTO_MODELING_PACKAGE="$(pwd)/${ONTO_MODELING_PACKAGE}" ;;
esac
case "${KNOWLEDGE_HUB_PACKAGE}" in
    ''|/*) ;;
    *) KNOWLEDGE_HUB_PACKAGE="$(pwd)/${KNOWLEDGE_HUB_PACKAGE}" ;;
esac
case "${EVALUATION_PACKAGE}" in
    ''|/*) ;;
    *) EVALUATION_PACKAGE="$(pwd)/${EVALUATION_PACKAGE}" ;;
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

[ -z "${CONSUMER_PACKAGE}" ] \
    || CONSUMER_PACKAGE="$(resolve_package_path "${CONSUMER_PACKAGE}" Consumer发布包)"
[ -z "${MODELING_PACKAGE}" ] \
    || MODELING_PACKAGE="$(resolve_package_path "${MODELING_PACKAGE}" Modeling发布包)"
[ -z "${ONTO_MODELING_PACKAGE}" ] \
    || ONTO_MODELING_PACKAGE="$(resolve_package_path "${ONTO_MODELING_PACKAGE}" 'Onto Modeling发布包')"
[ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
    || KNOWLEDGE_HUB_PACKAGE="$(resolve_package_path "${KNOWLEDGE_HUB_PACKAGE}" '知识包 Hub发布包')"
[ -z "${EVALUATION_PACKAGE}" ] \
    || EVALUATION_PACKAGE="$(resolve_package_path "${EVALUATION_PACKAGE}" '独立评测服务发布包')"

if [ -n "${UPGRADE_FROM}" ] && [ ! -e "${UPGRADE_FROM}" ]; then
    [ ! -L "${UPGRADE_FROM}" ] || die "--upgrade-from 不能指向符号链接：${UPGRADE_FROM}"
    log "旧合并部署根目录不存在，按首次安装处理：${UPGRADE_FROM}"
    UPGRADE_FROM=""
fi

CONSUMER_DIR="${INSTALL_ROOT}/onto-agent"
MODELING_DIR="${INSTALL_ROOT}/modeling"
ONTO_MODELING_DIR="${INSTALL_ROOT}/onto-modeling"
KNOWLEDGE_HUB_DIR="${INSTALL_ROOT}/knowledge-hub"
EVALUATION_DIR="${INSTALL_ROOT}/eval-service"

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

# 评测交付物：发布包（顶层目录 eval-service）或完整离线包中的 eval-service 目录
EVALUATION_FILES="deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE"

validate_evaluation_package() {
    local relative entry
    [ -n "${EVALUATION_PACKAGE}" ] || return 0
    if [ -f "${EVALUATION_PACKAGE}" ]; then
        validate_package "${EVALUATION_PACKAGE}" eval-service
        for relative in ${EVALUATION_FILES}; do
            entry="$(tar -tvzf "${EVALUATION_PACKAGE}" "eval-service/${relative}" 2>/dev/null || true)"
            [ "${entry#-}" != "${entry}" ] || die "评测发布包缺少普通文件 eval-service/${relative}"
        done
        return 0
    fi
    [ -d "${EVALUATION_PACKAGE}" ] && [ ! -L "${EVALUATION_PACKAGE}" ] \
        || die "评测发布包不存在，或评测发布目录是符号链接：${EVALUATION_PACKAGE}"
    [ ! -L "${EVALUATION_PACKAGE}/deploy" ] || die '评测发布目录的 deploy 不能是符号链接'
    for relative in ${EVALUATION_FILES}; do
        [ -f "${EVALUATION_PACKAGE}/${relative}" ] && [ ! -L "${EVALUATION_PACKAGE}/${relative}" ] \
            || die "评测发布目录缺少普通文件 ${relative}"
    done
    EVALUATION_PACKAGE="$(cd "${EVALUATION_PACKAGE}" && pwd)"
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

ensure_upgrade_bootstrap_password() {
    env_file="$1"
    bootstrap_password="$(env_get "${env_file}" AUTH_BOOTSTRAP_ADMIN_PASSWORD)"
    if [ -z "${bootstrap_password}" ]; then
        bootstrap_password="Aa1!$(generate_hex_secret 8)"
        write_env_value "${env_file}" AUTH_BOOTSTRAP_ADMIN_PASSWORD "${bootstrap_password}"
        log "旧合并部署缺少 AUTH_BOOTSTRAP_ADMIN_PASSWORD，已生成并写入升级配置（值不会打印）"
    fi
}

run_consumer_upgrade() {
    release_dir="$1"
    install_dir="$2"
    log "开始 Consumer 升级；预检中受管旧容器占用自身端口显示 WARN 时不会阻断升级"
    if bash "${release_dir}/deploy/upgrade.sh" "${install_dir}"; then
        upgrade_status=0
    else
        upgrade_status=$?
    fi
    case "${upgrade_status}" in
        0)
            return 0
            ;;
        10)
            log "Consumer 升级完成，存在需要关注的警告，继续升级 Modeling"
            return 0
            ;;
        *)
            return "${upgrade_status}"
            ;;
    esac
}

container_mount_volume() {
    container_name="$1"
    destination="$2"
    docker container inspect "${container_name}" \
        --format '{{range .Mounts}}{{if eq .Destination "'"${destination}"'"}}{{println .Name}}{{end}}{{end}}' \
        2>/dev/null | head -n 1
}

# 先读容器配置里的端口绑定：容器停止后仍然有效；docker port 只对运行中的容器有结果，
# 仅在绑定未写死宿主端口时兜底
legacy_host_port() {
    container_name="$1"
    container_port="$2"
    host_port="$(docker container inspect "${container_name}" \
        --format '{{range $port, $bindings := .HostConfig.PortBindings}}{{if eq $port "'"${container_port}"'/tcp"}}{{range $bindings}}{{println .HostPort}}{{end}}{{end}}{{end}}' \
        2>/dev/null | sed -n '/^[0-9][0-9]*$/p' | head -n 1 || true)"
    [ -n "${host_port}" ] || host_port="$(docker port "${container_name}" "${container_port}/tcp" 2>/dev/null \
        | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1 || true)"
    printf '%s' "${host_port}"
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

validate_modeling_container_owner() {
    container_name="$1"
    expected_dir="$2"
    docker container inspect "${container_name}" >/dev/null 2>&1 || die \
        "已分离部署缺少容器：${container_name}"
    working_dir="$(docker container inspect "${container_name}" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
        2>/dev/null || true)"
    [ "${working_dir}" = "${expected_dir}" ] || die \
        "容器 ${container_name} 不属于独立 Modeling 目录 ${expected_dir}，拒绝升级"
}

stop_consumer_containers_for_upgrade() {
    install_dir="$1"
    STOPPED_CONSUMER_CONTAINERS=""
    for container_name in onto-agent-frontend onto-agent-mcp onto-agent-backend \
                          onto-agent-jiuwenbox onto-agent-auth-server; do
        if ! docker container inspect "${container_name}" >/dev/null 2>&1; then
            continue
        fi
        working_dir="$(docker container inspect "${container_name}" \
            --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
            2>/dev/null || true)"
        [ "${working_dir}" = "${install_dir}" ] || die \
            "Consumer 容器 ${container_name} 不属于安装目录 ${install_dir}，拒绝停止"
        running="$(docker container inspect "${container_name}" \
            --format '{{.State.Running}}' 2>/dev/null || true)"
        if [ "${running}" = true ]; then
            docker stop "${container_name}" >/dev/null || die "停止容器失败：${container_name}"
            STOPPED_CONSUMER_CONTAINERS="${STOPPED_CONSUMER_CONTAINERS} ${container_name}"
        fi
    done
}

restore_consumer_containers_after_failure() {
    for container_name in ${STOPPED_CONSUMER_CONTAINERS:-}; do
        docker start "${container_name}" >/dev/null 2>&1 || true
    done
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

package_env_value() {
    package="$1"
    member="$2"
    key="$3"
    tar -xzOf "${package}" "${member}" 2>/dev/null \
        | sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" | tail -n 1 || true
}

# Onto Modeling 排在 Consumer、Modeling 之后部署。能提前发现的问题都在动手前拦下，
# 避免 Consumer、Modeling 已经升级完、Onto Modeling 才失败的半完成状态。
precheck_onto_modeling() {
    current_modeling_port="$1"
    if [ -z "${ONTO_MODELING_PACKAGE}" ]; then
        log "未指定 --onto-modeling-package，本次不部署 Onto Modeling"
        return 0
    fi
    ONTO_WEB_PORT="${ONTO_WEB_PORT:-$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_WEB_PORT)}"
    ONTO_WEB_PORT="${ONTO_WEB_PORT:-$(package_env_value \
        "${ONTO_MODELING_PACKAGE}" onto-modeling/.env.example ONTO_WEB_PORT)}"
    [ -n "${ONTO_WEB_PORT}" ] || die "无法确定 Onto Modeling 宿主端口，请用 ONTO_WEB_PORT 指定"
    modeling_port="${MODELING_PORT:-${current_modeling_port}}"
    modeling_port="${modeling_port:-$(package_env_value \
        "${MODELING_PACKAGE}" modeling/.env.example MODELING_PORT)}"
    [ "${ONTO_WEB_PORT}" != "${modeling_port}" ] || die \
        "Onto Modeling 与 Modeling Workbench 都要占用宿主端口 ${ONTO_WEB_PORT}；请用 ONTO_WEB_PORT 或 MODELING_PORT 错开"
    if docker container inspect onto-modeling >/dev/null 2>&1; then
        working_dir="$(docker container inspect onto-modeling \
            --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
            2>/dev/null || true)"
        [ "${working_dir}" = "${ONTO_MODELING_DIR}" ] || die \
            "容器 onto-modeling 属于 ${working_dir:-未知目录}，不是 ${ONTO_MODELING_DIR}，拒绝接管"
    fi
    preflight_dir="$(mktemp -d "${TMPDIR:-/tmp}/onto-modeling-preflight.XXXXXX")" \
        || die "无法创建 Onto Modeling 预检目录"
    if ! tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${preflight_dir}" onto-modeling/preflight.sh; then
        rm -rf "${preflight_dir}"
        die "Onto Modeling 发布包缺少 preflight.sh"
    fi
    log "Onto Modeling 部署预检"
    if ! (cd "${preflight_dir}/onto-modeling" && bash ./preflight.sh); then
        rm -rf "${preflight_dir}"
        die "Onto Modeling 部署预检未通过，尚未做任何变更"
    fi
    rm -rf "${preflight_dir}"
}

# Hub 容器名固定：已有容器不属于本安装根目录时，在任何变更之前拒绝接管
precheck_knowledge_hub() {
    if [ -z "${KNOWLEDGE_HUB_PACKAGE}" ]; then
        if [ -n "${ONTO_MODELING_PACKAGE}" ]; then
            docker container inspect knowledge-hub >/dev/null 2>&1 && docker exec knowledge-hub python -c \
                "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8200/api/health', timeout=3)" \
                >/dev/null 2>&1 || die \
                "知识包发布不可用：未提供 --knowledge-hub-package，且已有知识包 Hub 未就绪；请使用本版本安装脚本并提供配套 Hub 发布包"
        fi
        log "未指定 --knowledge-hub-package，本次不部署知识包 Hub"
        return 0
    fi
    if docker container inspect knowledge-hub >/dev/null 2>&1; then
        working_dir="$(docker container inspect knowledge-hub \
            --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
            2>/dev/null || true)"
        [ "${working_dir}" = "${KNOWLEDGE_HUB_DIR}" ] || die \
            "容器 knowledge-hub 属于 ${working_dir:-未知目录}，不是 ${KNOWLEDGE_HUB_DIR}，拒绝接管"
    fi
}

# 网络按运行中的 Consumer 容器现场探测，不按命名规律猜
consumer_network() {
    docker container inspect onto-agent-backend \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null | head -n 1
}

# 评测工作空间接口的 HTTP 状态：接口存在时未登录请求返回 401，旧版 Consumer 返回 404；无法探测时为空
consumer_evaluation_route_status() {
    docker exec -i onto-agent-backend python - 2>/dev/null <<'PY' || true
import urllib.error
import urllib.request

request = urllib.request.Request("http://127.0.0.1:5055/agentic/internal/evaluation/workspaces", data=b"{}",
                                 method="POST", headers={"Content-Type": "application/json"})
try:
    print(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5).status)
except urllib.error.HTTPError as error:
    print(error.code)
except Exception:
    print("")
PY
}

# 在已运行的 Consumer 旁部署 Hub、Onto Modeling 和评测：不安装、不升级、不重启 Consumer，也不修改其配置
attach_install() {
    local running route_status
    running="$(docker container inspect onto-agent-backend --format '{{.State.Running}}' 2>/dev/null || true)"
    [ "${running}" = true ] \
        || die "未指定 --consumer-package 时需要已运行的 Consumer（容器 onto-agent-backend）"
    CONSUMER_DIR="$(docker container inspect onto-agent-backend \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    [ -n "${CONSUMER_DIR}" ] && [ -f "${CONSUMER_DIR}/backend/.env" ] \
        || die "无法定位运行中 Consumer 的安装目录：${CONSUMER_DIR:-未知}"
    if [ "${INSTALL_ROOT_EXPLICIT}" -eq 0 ] && [ "$(basename "${CONSUMER_DIR}")" = onto-agent ]; then
        INSTALL_ROOT="$(dirname "${CONSUMER_DIR}")"
        ONTO_MODELING_DIR="${INSTALL_ROOT}/onto-modeling"
        KNOWLEDGE_HUB_DIR="${INSTALL_ROOT}/knowledge-hub"
        EVALUATION_DIR="${INSTALL_ROOT}/eval-service"
    fi
    log "沿用运行中的 Consumer：${CONSUMER_DIR}；组件安装根目录：${INSTALL_ROOT}"

    precheck_onto_modeling "$(legacy_host_port mw-modeling 5100)"
    precheck_knowledge_hub
    precheck_evaluation
    if [ -n "${EVALUATION_PACKAGE}" ] && [ -z "${EVAL_ENV_FILE:-}" ]; then
        [ -n "$(env_get "${CONSUMER_DIR}/backend/.env" EVALUATION_SERVICE_TOKEN)" ] || die \
            "运行中的 Consumer 未配置 EVALUATION_SERVICE_TOKEN：请先用 --consumer-package 升级 Consumer，再部署评测"
        route_status="$(consumer_evaluation_route_status)"
        [ "${route_status}" != 404 ] || die \
            "运行中的 Consumer 版本不支持独立评测（缺少评测工作空间接口）：请先用 --consumer-package 升级 Consumer"
        [ -n "${route_status}" ] || log "警告：无法确认 Consumer 是否支持独立评测，继续部署"
    fi

    mkdir -p "${INSTALL_ROOT}"
    deploy_knowledge_hub
    deploy_onto_modeling
    deploy_evaluation
    log "已在运行中的 Consumer 旁完成部署"
    [ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
        || printf '[full-install] 知识包 Hub：%s\n' "${KNOWLEDGE_HUB_DIR}"
    [ -z "${ONTO_MODELING_PACKAGE}" ] \
        || printf '[full-install] Onto Modeling：%s\n' "${ONTO_MODELING_DIR}"
    [ -z "${EVALUATION_PACKAGE}" ] \
        || printf '[full-install] 独立评测服务：%s\n' "${EVALUATION_DIR}"
}

ensure_evaluation_service_token() {
    [ -n "${EVALUATION_PACKAGE}" ] || return 0
    local env_file="${CONSUMER_DIR}/backend/.env" secret modeling_token
    secret="$(env_get "${env_file}" EVALUATION_SERVICE_TOKEN)"
    [ -n "${secret}" ] || secret="$(generate_hex_secret 32)"
    [ "${#secret}" -ge 32 ] || die 'EVALUATION_SERVICE_TOKEN 至少需要 32 个字符'
    modeling_token="$(env_get "${env_file}" MODELING_SERVICE_TOKEN)"
    [ "${secret}" != "${modeling_token}" ] \
        || die 'EVALUATION_SERVICE_TOKEN 不能与 MODELING_SERVICE_TOKEN 相同'
    write_env_value "${env_file}" EVALUATION_SERVICE_TOKEN "${secret}"
    # Consumer 向评测推送用户反馈（数据飞轮）的服务凭据：已有值保留，缺失时生成
    local flywheel
    flywheel="$(env_get "${env_file}" FLYWHEEL_SERVICE_TOKEN)"
    [ -n "${flywheel}" ] || flywheel="$(generate_hex_secret 32)"
    [ "${#flywheel}" -ge 32 ] || die 'FLYWHEEL_SERVICE_TOKEN 至少需要 32 个字符'
    [ "${flywheel}" != "${secret}" ] && [ "${flywheel}" != "${modeling_token}" ] \
        || die 'FLYWHEEL_SERVICE_TOKEN 不能与其他服务凭据相同'
    write_env_value "${env_file}" FLYWHEEL_SERVICE_TOKEN "${flywheel}"
}

precheck_evaluation() {
    [ -n "${EVALUATION_PACKAGE}" ] || return 0
    local expected owner
    [ ! -L "${EVALUATION_DIR}" ] && [ ! -L "${EVALUATION_DIR}/deploy" ] \
        || die '评测安装目录不能是符号链接'
    expected="$(cd "${EVALUATION_DIR}/deploy" 2>/dev/null && pwd || printf '%s' "${EVALUATION_DIR}/deploy")"
    if docker container inspect knowledge-evaluation >/dev/null 2>&1; then
        owner="$(docker container inspect knowledge-evaluation \
            --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
        [ "${owner}" = "${expected}" ] \
            || die "容器 knowledge-evaluation 属于 ${owner:-未知目录}，不是 ${expected}，拒绝接管"
    fi
    if [ -n "${EVAL_ENV_FILE:-}" ]; then
        [ -f "${EVAL_ENV_FILE}" ] && [ -r "${EVAL_ENV_FILE}" ] || die 'EVAL_ENV_FILE 不存在或不可读'
        EVAL_ENV_FILE="$(cd "$(dirname "${EVAL_ENV_FILE}")" && pwd)/$(basename "${EVAL_ENV_FILE}")"
        export EVAL_ENV_FILE
    elif [ -z "${KNOWLEDGE_HUB_PACKAGE}" ]; then
        [ -n "$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_EVALUATION_SERVICE_TOKEN)" ] \
            || die '独立评测服务需要知识包 Hub：请提供 Hub 发布包或先部署含评测凭据的 Hub'
    fi
}

deploy_evaluation() {
    [ -n "${EVALUATION_PACKAGE}" ] || return 0
    local relative network hub_token consumer_token modeling_token flywheel_token elapsed=0 members
    [ ! -L "${EVALUATION_DIR}" ] && [ ! -L "${EVALUATION_DIR}/deploy" ] \
        || die '评测安装目录不能是符号链接'
    mkdir -p "${EVALUATION_DIR}/deploy" || die '无法创建评测安装目录'
    # 仅覆盖交付物，保留 deploy/.env 中的数据加密密钥、端口和其他运行配置。
    for relative in ${EVALUATION_FILES}; do
        [ ! -L "${EVALUATION_DIR}/${relative}" ] || die "评测目标文件不能是符号链接：${relative}"
    done
    if [ -f "${EVALUATION_PACKAGE}" ]; then
        members=()
        for relative in ${EVALUATION_FILES}; do members+=("eval-service/${relative}"); done
        tar -xzf "${EVALUATION_PACKAGE}" -C "${EVALUATION_DIR}" --strip-components=1 "${members[@]}" \
            || die "无法解压评测发布包：${EVALUATION_PACKAGE}"
    elif [ "${EVALUATION_PACKAGE}" != "$(cd "${EVALUATION_DIR}" && pwd)" ]; then
        for relative in ${EVALUATION_FILES}; do
            cp "${EVALUATION_PACKAGE}/${relative}" "${EVALUATION_DIR}/${relative}" \
                || die "无法复制评测交付物：${relative}"
        done
    fi
    network="$(consumer_network)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    hub_token="$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_EVALUATION_SERVICE_TOKEN)"
    consumer_token="$(env_get "${CONSUMER_DIR}/backend/.env" EVALUATION_SERVICE_TOKEN)"
    # Onto Modeling 的 deploy.sh 生成入站凭据；未部署时留空，评测只告警、不发完成通知
    modeling_token="$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_EVALUATION_SERVICE_TOKEN)"
    flywheel_token="$(env_get "${CONSUMER_DIR}/backend/.env" FLYWHEEL_SERVICE_TOKEN)"
    [ -n "${hub_token}" ] || [ -n "${EVAL_ENV_FILE:-}" ] || die 'Hub 缺少评测凭据，请先更新知识包 Hub'
    log "部署独立评测服务：${EVALUATION_DIR}（网络 ${network}）"
    (cd "${EVALUATION_DIR}" \
        && CONSUMER_NETWORK="${network}" \
        HUB_EVALUATION_SERVICE_TOKEN="${hub_token}" EVALUATION_SERVICE_TOKEN="${consumer_token}" \
        EVAL_MODELING_SERVICE_TOKEN="${modeling_token}" EVAL_FLYWHEEL_SERVICE_TOKEN="${flywheel_token}" \
        bash ./deploy/deploy.sh) \
        || die "独立评测服务部署失败；修复后可单独执行 bash ${EVALUATION_DIR}/deploy/deploy.sh"
    unset hub_token consumer_token modeling_token flywheel_token
    until docker exec knowledge-evaluation python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/evaluation/api/health', timeout=3)" \
        >/dev/null 2>&1; do
        [ "${elapsed}" -lt 60 ] || die '独立评测服务 60 秒内未就绪，请查看 docker logs knowledge-evaluation'
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log '独立评测服务已就绪（默认访问 http://localhost:8090/evaluation/，端口以评测运行配置为准）'
}

# Hub 是独立 compose 项目，排在 Onto Modeling 之前部署；服务凭据沿用已部署 Onto Modeling 的值，
# 两边都没有时由 Hub 部署脚本生成，随后 deploy_onto_modeling 对齐。值只经子进程环境传递，不打印。
deploy_knowledge_hub() {
    [ -n "${KNOWLEDGE_HUB_PACKAGE}" ] || return 0
    log "部署知识包 Hub：${KNOWLEDGE_HUB_DIR}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可升级
    tar -xzf "${KNOWLEDGE_HUB_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${KNOWLEDGE_HUB_DIR}/deploy.sh" ] || die "知识包 Hub 发布包缺少 deploy.sh"
    network="$(consumer_network)"
    [ -n "${network}" ] || die "无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行"
    onto_token="$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_HUB_SERVICE_TOKEN)"
    if ! (cd "${KNOWLEDGE_HUB_DIR}" \
            && CONSUMER_NETWORK="${network}" HUB_SERVICE_TOKEN="${onto_token}" bash ./deploy.sh); then
        die "知识包 Hub 部署失败；Consumer 与 Modeling 已完成，修复后可单独执行 bash ${KNOWLEDGE_HUB_DIR}/deploy.sh"
    fi
    unset onto_token
    log "Knowledge Hub deployment finished: ${KNOWLEDGE_HUB_DIR}"
}

deploy_onto_modeling() {
    [ -n "${ONTO_MODELING_PACKAGE}" ] || return 0
    env_file="${ONTO_MODELING_DIR}/.env"
    log "部署 Onto Modeling：${ONTO_MODELING_DIR}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可升级
    tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${ONTO_MODELING_DIR}/deploy.sh" ] || die "Onto Modeling 发布包缺少 deploy.sh"
    network="$(consumer_network)"
    [ -n "${network}" ] || die "无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行"
    if [ ! -f "${env_file}" ]; then
        [ -f "${ONTO_MODELING_DIR}/.env.example" ] || die "Onto Modeling 发布包缺少 .env.example"
        cp "${ONTO_MODELING_DIR}/.env.example" "${env_file}"
    fi
    chmod 600 "${env_file}"
    write_env_value "${env_file}" ONTO_WEB_PORT "${ONTO_WEB_PORT}"
    write_env_value "${env_file}" ONTO_MODELING_EXTERNAL_AUTH_NETWORK "${network}"
    # consumer 的 jiuwenbox socket 卷：onto-modeling 只读挂载后，脚本运行验证在沙箱中执行；
    # 现场探测为准，探测不到写空值，验证改在 onto-modeling 容器内执行
    write_env_value "${env_file}" ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME \
        "$(container_mount_volume onto-agent-backend /run/jiuwenbox || true)"
    # 评测调用 Modeling Workbench 走容器网络，与宿主端口无关
    [ -n "$(env_get "${env_file}" ONTO_WEB_MODELING_URL)" ] \
        || write_env_value "${env_file}" ONTO_WEB_MODELING_URL 'http://mw-modeling:5100'
    # 与独立部署的知识包 Hub 使用同一服务凭据；未部署 Hub 时保留原值（缺失由 Onto 部署脚本生成）
    hub_token="$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_SERVICE_TOKEN)"
    [ -z "${hub_token}" ] || write_env_value "${env_file}" ONTO_HUB_SERVICE_TOKEN "${hub_token}"
    unset hub_token
    log "Onto Modeling 共享 Consumer 账号中心：网络 ${network}"
    if ! (cd "${ONTO_MODELING_DIR}" && bash ./deploy.sh); then
        die "Onto Modeling 部署失败；Consumer 与 Modeling 已完成，修复后可单独执行 bash ${ONTO_MODELING_DIR}/deploy.sh"
    fi
    elapsed=0
    until curl -fsS --noproxy '*' --max-time 5 \
            "http://127.0.0.1:${ONTO_WEB_PORT}/api/health" >/dev/null 2>&1; do
        [ "${elapsed}" -lt 120 ] || die \
            "Onto Modeling 120 秒内未就绪：http://127.0.0.1:${ONTO_WEB_PORT}/api/health；请查看 docker logs onto-modeling"
        sleep 2
        elapsed=$((elapsed + 2))
    done
    curl -fsS --noproxy '*' --max-time 15 \
        "http://127.0.0.1:${ONTO_WEB_PORT}/api/health/dependencies" >/dev/null \
        || die 'Onto Modeling 已启动，但知识包发布不可用：检查 Hub 部署、共享网络和 ONTO_HUB_* 配置'
    log "Onto Modeling deployment finished: http://localhost:${ONTO_WEB_PORT}"
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
    ensure_upgrade_bootstrap_password "${CONSUMER_DIR}/backend/.env"

    legacy_backend_port="$(legacy_host_port onto-agent-backend 5055 || true)"
    legacy_mcp_port="$(legacy_host_port onto-agent-mcp 7488 || true)"
    legacy_frontend_port="$(legacy_host_port onto-agent-frontend 80 || true)"

    for container_name in mw-modeling mw-modeling-agent mw-modeling-nl2sql; do
        validate_legacy_modeling_container "${container_name}"
    done

    modeling_volume="$(resolve_legacy_volume mw-modeling mw-modeling_modeling-data)"
    modeling_image="$(docker container inspect mw-modeling --format '{{.Config.Image}}' 2>/dev/null || true)"
    if [ -z "${modeling_image}" ]; then
        modeling_image="$(sed -n \
            's/^[[:space:]]*"modeling_image":[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${CONSUMER_DIR}/deploy/manifest.json" | head -n 1)"
    fi
    [ -n "${modeling_image}" ] || die "无法确定旧 Modeling 镜像"

    # 0.26.9.0 起合并部署才带 mw-modeling-agent；更早的旧版只有 Workbench，Agent 由新包首次创建。
    # 旧清单声明了 Agent 却找不到容器属于异常现场，不猜测数据去向
    legacy_agent_image="$(sed -n \
        's/^[[:space:]]*"modeling_agent_image":[[:space:]]*"\([^"]*\)".*/\1/p' \
        "${CONSUMER_DIR}/deploy/manifest.json" 2>/dev/null | head -n 1 || true)"
    agent_volume=""
    agent_image=""
    if docker container inspect mw-modeling-agent >/dev/null 2>&1; then
        agent_volume="$(resolve_legacy_volume mw-modeling-agent mw-modeling_modeling-agent-data)"
        agent_image="$(docker container inspect mw-modeling-agent --format '{{.Config.Image}}' 2>/dev/null || true)"
        agent_image="${agent_image:-${legacy_agent_image}}"
        [ -n "${agent_image}" ] || die "无法确定旧 Modeling Agent 镜像"
    elif [ -n "${legacy_agent_image}" ]; then
        die "旧部署清单声明了 Modeling Agent（${legacy_agent_image}），但容器 mw-modeling-agent 不存在；请确认 Agent 数据去向后再升级"
    else
        log "旧合并部署没有 Modeling Agent，升级后由新发布包首次创建"
    fi

    # 端口必须在停止旧容器前确定；Workbench 端口读不到时不能静默落回新包默认值
    legacy_modeling_port="${MODELING_PORT:-$(legacy_host_port mw-modeling 5100 || true)}"
    legacy_modeling_mcp_port="${MODELING_MCP_PORT:-$(legacy_host_port mw-modeling 5188 || true)}"
    [ -n "${legacy_modeling_port}" ] || die \
        "无法读取旧 Modeling Workbench 的宿主端口；请用 MODELING_PORT 显式指定后重试"
    precheck_onto_modeling "${legacy_modeling_port}"
    precheck_knowledge_hub
    precheck_evaluation

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

    legacy_config_items="VERSION backend/.env deploy/manifest.json"
    [ ! -e "${CONSUMER_DIR}/deploy/modeling" ] || legacy_config_items="${legacy_config_items} deploy/modeling"
    tar -C "${CONSUMER_DIR}" -czf "${backup_dir}/legacy-config.tar.gz" \
        ${legacy_config_items} 2>/dev/null \
        || die "备份旧部署配置失败"
    chmod 600 "${backup_dir}/legacy-config.tar.gz"
    ensure_evaluation_service_token

    stopped_containers=""
    modeling_handoff_started=false
    restore_stopped_containers() {
        [ "${modeling_handoff_started}" = false ] || return 0
        restore_consumer_containers_after_failure
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
    [ -z "${agent_volume}" ] || backup_docker_volume "${agent_volume}" "${agent_image}" \
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
    log "停止旧 Consumer 容器，避免升级备份期间日志文件继续写入"
    stop_consumer_containers_for_upgrade "${CONSUMER_DIR}"
    if ! run_consumer_upgrade "${stage_dir}/onto-agent" "${CONSUMER_DIR}"; then
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
    write_env_value "${MODELING_DIR}/.env" MODELING_PORT "${legacy_modeling_port}"
    [ -z "${legacy_modeling_mcp_port}" ] \
        || write_env_value "${MODELING_DIR}/.env" MODELING_MCP_PORT "${legacy_modeling_mcp_port}"

    modeling_handoff_started=true
    for container_name in mw-modeling-nl2sql mw-modeling mw-modeling-agent; do
        if docker container inspect "${container_name}" >/dev/null 2>&1; then
            docker rm "${container_name}" >/dev/null || die "删除旧容器失败：${container_name}"
        fi
    done

    log "部署独立 Modeling，并复用 ${modeling_volume}${agent_volume:+ 和 ${agent_volume}}"
    if ! (cd "${MODELING_DIR}" && bash deploy/install.sh); then
        die "Modeling 部署失败；旧数据卷和备份均已保留，备份位于 ${backup_dir}"
    fi

    [ "$(container_mount_volume mw-modeling /data)" = "${modeling_volume}" ] || die \
        "升级后 Modeling 未挂载原数据卷"
    [ "$(container_mount_volume mw-modeling-agent /data)" \
        = "${agent_volume:-mw-modeling_modeling-agent-data}" ] || die \
        "升级后 Modeling Agent 未挂载${agent_volume:+原}数据卷"

    cleanup_upgrade_stage
    trap - EXIT
    deploy_knowledge_hub
    deploy_onto_modeling
    deploy_evaluation
    log "Consumer 与 Modeling 分离升级完成"
    printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
    printf '[full-install] Modeling：%s\n' "${MODELING_DIR}"
    [ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
        || printf '[full-install] 知识包 Hub：%s\n' "${KNOWLEDGE_HUB_DIR}"
    [ -z "${ONTO_MODELING_PACKAGE}" ] \
        || printf '[full-install] Onto Modeling：%s\n' "${ONTO_MODELING_DIR}"
    printf '[full-install] 升级备份：%s\n' "${backup_dir}"
}

upgrade_separated_install() {
    CONSUMER_DIR="${INSTALL_ROOT}/onto-agent"
    MODELING_DIR="${INSTALL_ROOT}/modeling"

    [ -d "${CONSUMER_DIR}" ] || die "Consumer 安装目录不存在：${CONSUMER_DIR}"
    [ -f "${CONSUMER_DIR}/VERSION" ] || die "Consumer 安装目录缺少 VERSION"
    [ -f "${CONSUMER_DIR}/backend/.env" ] || die "Consumer 安装目录缺少 backend/.env"
    [ -f "${CONSUMER_DIR}/deploy/state/current.env" ] || die "Consumer 安装目录缺少部署状态"
    [ -d "${MODELING_DIR}" ] || die "Modeling 安装目录不存在：${MODELING_DIR}"
    [ -f "${MODELING_DIR}/VERSION" ] || die "Modeling 安装目录缺少 VERSION"
    [ -f "${MODELING_DIR}/.env" ] || die "Modeling 安装目录缺少 .env"

    validate_modeling_container_owner mw-modeling "${MODELING_DIR}"
    validate_modeling_container_owner mw-modeling-agent "${MODELING_DIR}"
    modeling_volume="$(resolve_legacy_volume mw-modeling mw-modeling_modeling-data)"
    agent_volume="$(resolve_legacy_volume mw-modeling-agent mw-modeling_modeling-agent-data)"
    modeling_image="$(docker container inspect mw-modeling --format '{{.Config.Image}}')"
    agent_image="$(docker container inspect mw-modeling-agent --format '{{.Config.Image}}')"

    consumer_token="$(env_get "${CONSUMER_DIR}/backend/.env" MODELING_SERVICE_TOKEN)"
    modeling_token="$(env_get "${MODELING_DIR}/.env" MODELING_SERVICE_TOKEN)"
    [ "${#consumer_token}" -ge 32 ] || die "Consumer 未配置有效的 MODELING_SERVICE_TOKEN"
    [ "${consumer_token}" = "${modeling_token}" ] || die \
        "Consumer 与 Modeling 的 MODELING_SERVICE_TOKEN 不一致，拒绝升级"
    precheck_onto_modeling "$(env_get "${MODELING_DIR}/.env" MODELING_PORT)"
    precheck_knowledge_hub
    precheck_evaluation

    stage_dir="$(mktemp -d "${INSTALL_ROOT}/.separated-upgrade.XXXXXX")" \
        || die "无法创建升级暂存目录"
    cleanup_separated_stage() {
        if [ -n "${stage_dir:-}" ] && [ -d "${stage_dir}" ]; then
            find "${stage_dir}" -mindepth 1 -delete 2>/dev/null || true
            rmdir "${stage_dir}" 2>/dev/null || true
        fi
    }
    trap cleanup_separated_stage EXIT

    backup_dir="${INSTALL_ROOT}/backups/separated-upgrade-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "${backup_dir}"
    chmod 700 "${backup_dir}"

    log "解压新发布包到升级暂存目录"
    tar -xzf "${CONSUMER_PACKAGE}" -C "${stage_dir}"
    tar -xzf "${MODELING_PACKAGE}" -C "${stage_dir}"
    [ -x "${stage_dir}/onto-agent/deploy/upgrade.sh" ] || die "Consumer 发布包缺少 upgrade.sh"
    [ -x "${stage_dir}/modeling/deploy/install.sh" ] || die "Modeling 发布包缺少 install.sh"

    cp "${CONSUMER_DIR}/backend/.env" "${backup_dir}/consumer.env"
    cp "${MODELING_DIR}/.env" "${backup_dir}/modeling.env"
    chmod 600 "${backup_dir}/consumer.env" "${backup_dir}/modeling.env"
    ensure_evaluation_service_token

    stopped_containers=""
    handoff_started=false
    restore_separated_containers() {
        [ "${handoff_started}" = false ] || return 0
        restore_consumer_containers_after_failure
        for container_name in ${stopped_containers}; do
            docker start "${container_name}" >/dev/null 2>&1 || true
        done
    }
    trap 'restore_separated_containers; cleanup_separated_stage' EXIT
    for container_name in mw-modeling-nl2sql mw-modeling mw-modeling-agent; do
        running="$(docker container inspect "${container_name}" \
            --format '{{.State.Running}}' 2>/dev/null || true)"
        if [ "${running}" = true ]; then
            docker stop "${container_name}" >/dev/null || die "停止容器失败：${container_name}"
            stopped_containers="${stopped_containers} ${container_name}"
        fi
    done

    log "备份 Modeling 数据卷"
    backup_docker_volume "${modeling_volume}" "${modeling_image}" \
        modeling-data.tar.gz "${backup_dir}"
    backup_docker_volume "${agent_volume}" "${agent_image}" \
        modeling-agent-data.tar.gz "${backup_dir}"

    log "升级独立 Consumer：${CONSUMER_DIR}"
    [ -z "${BACKEND_PORT:-}" ] \
        || write_env_value "${CONSUMER_DIR}/backend/.env" BACKEND_PORT "${BACKEND_PORT}"
    [ -z "${MCP_SERVER_PORT:-}" ] \
        || write_env_value "${CONSUMER_DIR}/backend/.env" MCP_SERVER_PORT "${MCP_SERVER_PORT}"
    [ -z "${FRONTEND_PORT:-}" ] \
        || write_env_value "${CONSUMER_DIR}/backend/.env" FRONTEND_PORT "${FRONTEND_PORT}"
    log "停止 Consumer 容器，避免升级备份期间日志文件继续写入"
    stop_consumer_containers_for_upgrade "${CONSUMER_DIR}"
    if ! run_consumer_upgrade "${stage_dir}/onto-agent" "${CONSUMER_DIR}"; then
        die "Consumer 升级失败；Modeling 容器已尝试恢复，备份位于 ${backup_dir}"
    fi
    upgraded_token="$(env_get "${CONSUMER_DIR}/backend/.env" MODELING_SERVICE_TOKEN)"
    [ "${upgraded_token}" = "${consumer_token}" ] || die \
        "Consumer 升级后 MODELING_SERVICE_TOKEN 发生变化，拒绝继续"

    log "升级独立 Modeling：${MODELING_DIR}"
    tar -C "${stage_dir}/modeling" -c -f - . | tar -C "${MODELING_DIR}" -x -f -
    cp "${backup_dir}/modeling.env" "${MODELING_DIR}/.env"
    chmod 600 "${MODELING_DIR}/.env"
    handoff_started=true
    if ! (cd "${MODELING_DIR}" && bash deploy/install.sh); then
        die "Modeling 升级失败；原数据卷和备份均已保留，备份位于 ${backup_dir}"
    fi

    [ "$(container_mount_volume mw-modeling /data)" = "${modeling_volume}" ] || die \
        "升级后 Modeling 未挂载原数据卷"
    [ "$(container_mount_volume mw-modeling-agent /data)" = "${agent_volume}" ] || die \
        "升级后 Modeling Agent 未挂载原数据卷"

    cleanup_separated_stage
    trap - EXIT
    deploy_knowledge_hub
    deploy_onto_modeling
    deploy_evaluation
    log "分离部署升级完成"
    printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
    printf '[full-install] Modeling：%s\n' "${MODELING_DIR}"
    [ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
        || printf '[full-install] 知识包 Hub：%s\n' "${KNOWLEDGE_HUB_DIR}"
    [ -z "${ONTO_MODELING_PACKAGE}" ] \
        || printf '[full-install] Onto Modeling：%s\n' "${ONTO_MODELING_DIR}"
    printf '[full-install] 升级备份：%s\n' "${backup_dir}"
}

[ -z "${CONSUMER_PACKAGE}" ] || validate_package "${CONSUMER_PACKAGE}" onto-agent
[ -z "${MODELING_PACKAGE}" ] || validate_package "${MODELING_PACKAGE}" modeling
[ -z "${ONTO_MODELING_PACKAGE}" ] || validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling
[ -z "${KNOWLEDGE_HUB_PACKAGE}" ] || validate_package "${KNOWLEDGE_HUB_PACKAGE}" knowledge-hub
validate_evaluation_package

if [ "${ATTACH_ONLY}" = 1 ]; then
    attach_install
    exit 0
fi

if [ "${COMMAND}" = "upgrade" ]; then
    upgrade_separated_install
    exit 0
fi

if [ -n "${UPGRADE_FROM}" ]; then
    upgrade_install
    exit 0
fi

[ ! -e "${CONSUMER_DIR}" ] \
    || die "Consumer 安装目录已存在：${CONSUMER_DIR}；本脚本只用于首次安装"
[ ! -e "${MODELING_DIR}" ] \
    || die "Modeling 安装目录已存在：${MODELING_DIR}；本脚本只用于首次安装"
precheck_onto_modeling ""
precheck_knowledge_hub
precheck_evaluation

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
ensure_evaluation_service_token

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
deploy_knowledge_hub
deploy_onto_modeling
deploy_evaluation

log "Consumer 与 Modeling 安装完成"
printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
printf '[full-install] Modeling：%s\n' "${MODELING_DIR}"
[ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
    || printf '[full-install] 知识包 Hub：%s\n' "${KNOWLEDGE_HUB_DIR}"
[ -z "${ONTO_MODELING_PACKAGE}" ] \
    || printf '[full-install] Onto Modeling：%s\n' "${ONTO_MODELING_DIR}"
printf '[full-install] 管理员初始密码保存在：%s\n' "${CONSUMER_DIR}/backend/.env"
