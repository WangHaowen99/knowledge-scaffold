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
用法（首次安装、upgrade 升级、--upgrade-from 旧版迁移）：
  bash install-consumer-modeling.sh [upgrade] \
    --consumer-package <Consumer发布包> \
    --onto-modeling-package <Onto Modeling发布包> \
    --knowledge-hub-package <知识包 Hub发布包> \
    --evaluation-package <独立评测服务发布包> \
    [--upgrade-from <旧合并部署目录>]

四个必填发布包：
  --consumer-package FILE       Consumer 发布包
  --onto-modeling-package FILE  Onto Modeling 发布包
  --knowledge-hub-package FILE  知识包 Hub 发布包
  --evaluation-package FILE     评测发布包 evaluation-*.tar.gz，或完整包中的 eval-service 目录

可选参数：
  --upgrade-from DIR       旧合并部署根目录；目录不存在时按首次安装处理；不能与 upgrade 同用

发布包支持通配符，但必须唯一匹配。四个必填包缺一即退出，不复用正在运行的服务替代发布包。
部署顺序：Consumer → Hub → Onto Modeling → 评测。
升级会保留现有运行配置、数据卷和评测加密密钥。安装脚本须与发布包版本一致。

环境变量：
  INSTALL_ROOT           安装根目录，默认 /opt/onto-agent-stack；旧版迁移默认放在旧目录旁
  INSTALL_GLOBAL_OPSCTL  是否安装全局 opsctl，默认 0
  BACKEND_PORT MCP_SERVER_PORT FRONTEND_PORT ONTO_WEB_PORT
  EVAL_PORT EVAL_BIND_ADDRESS EVAL_ENV_FILE

示例：
  INSTALL_ROOT=/opt/onto-agent-stack bash install-consumer-modeling.sh \
    --consumer-package /path/to/consumer-release.tar.gz \
    --onto-modeling-package /path/to/onto-modeling-release.tar.gz \
    --knowledge-hub-package /path/to/knowledge-hub-release.tar.gz \
    --evaluation-package /path/to/evaluation-release.tar.gz

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

[ -n "${CONSUMER_PACKAGE}" ] || die "必须指定 --consumer-package"
[ -n "${ONTO_MODELING_PACKAGE}" ] || die "必须指定 --onto-modeling-package"
[ -n "${KNOWLEDGE_HUB_PACKAGE}" ] || die "必须指定 --knowledge-hub-package"
[ -n "${EVALUATION_PACKAGE}" ] || die "必须指定 --evaluation-package"

case "${CONSUMER_PACKAGE}" in
    ''|/*) ;;
    *) CONSUMER_PACKAGE="$(pwd)/${CONSUMER_PACKAGE}" ;;
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

# 独立 Consumer 发布包的 upgrade.sh 只接受安装目录，传入其他参数会直接失败
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
            log "Consumer 升级完成，存在需要关注的警告，继续部署后续服务"
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

package_env_value() {
    package="$1"
    member="$2"
    key="$3"
    tar -xzOf "${package}" "${member}" 2>/dev/null \
        | sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" | tail -n 1 || true
}

# Onto Modeling 排在 Consumer、Hub 之后部署。能提前发现的问题都在动手前拦下，
# 避免 Consumer 已经升级完、Onto Modeling 才失败的半完成状态。
precheck_onto_modeling() {
    current_modeling_port="$1"
    ONTO_WEB_PORT="${ONTO_WEB_PORT:-$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_WEB_PORT)}"
    ONTO_WEB_PORT="${ONTO_WEB_PORT:-$(package_env_value \
        "${ONTO_MODELING_PACKAGE}" onto-modeling/.env.example ONTO_WEB_PORT)}"
    [ -n "${ONTO_WEB_PORT}" ] || die "无法确定 Onto Modeling 宿主端口，请用 ONTO_WEB_PORT 指定"
    # Modeling Workbench 不由本脚本部署，但已有实例仍占用宿主端口
    [ "${ONTO_WEB_PORT}" != "${current_modeling_port}" ] || die \
        "Onto Modeling 与运行中的 Modeling Workbench 都要占用宿主端口 ${ONTO_WEB_PORT}；请用 ONTO_WEB_PORT 错开"
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

ensure_evaluation_service_token() {
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
    fi
}

deploy_evaluation() {
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
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5180/evaluation/api/health', timeout=3)" \
        >/dev/null 2>&1; do
        [ "${elapsed}" -lt 60 ] || die '独立评测服务 60 秒内未就绪，请查看 docker logs knowledge-evaluation'
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log '独立评测服务已就绪（默认访问 http://localhost:5180/evaluation/，端口以评测运行配置为准）'
}

# Hub 是独立 compose 项目，排在 Onto Modeling 之前部署；服务凭据沿用已部署 Onto Modeling 的值，
# 两边都没有时由 Hub 部署脚本生成，随后 deploy_onto_modeling 对齐。值只经子进程环境传递，不打印。
deploy_knowledge_hub() {
    log "部署知识包 Hub：${KNOWLEDGE_HUB_DIR}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可升级
    tar -xzf "${KNOWLEDGE_HUB_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${KNOWLEDGE_HUB_DIR}/deploy.sh" ] || die "知识包 Hub 发布包缺少 deploy.sh"
    network="$(consumer_network)"
    [ -n "${network}" ] || die "无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行"
    onto_token="$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_HUB_SERVICE_TOKEN)"
    if ! (cd "${KNOWLEDGE_HUB_DIR}" \
            && CONSUMER_NETWORK="${network}" HUB_SERVICE_TOKEN="${onto_token}" bash ./deploy.sh); then
        die "知识包 Hub 部署失败；Consumer 已完成，修复后可单独执行 bash ${KNOWLEDGE_HUB_DIR}/deploy.sh"
    fi
    unset onto_token
    log "Knowledge Hub deployment finished: ${KNOWLEDGE_HUB_DIR}"
}

deploy_onto_modeling() {
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
    # 与独立部署的知识包 Hub 使用同一服务凭据；未部署 Hub 时保留原值（缺失由 Onto 部署脚本生成）
    hub_token="$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_SERVICE_TOKEN)"
    [ -z "${hub_token}" ] || write_env_value "${env_file}" ONTO_HUB_SERVICE_TOKEN "${hub_token}"
    unset hub_token
    log "Onto Modeling 共享 Consumer 账号中心：网络 ${network}"
    if ! (cd "${ONTO_MODELING_DIR}" && bash ./deploy.sh); then
        die "Onto Modeling 部署失败；Consumer 与知识包 Hub 已完成，修复后可单独执行 bash ${ONTO_MODELING_DIR}/deploy.sh"
    fi
    elapsed=0
    until curl -fsS --noproxy '*' --max-time 5 \
            "http://127.0.0.1:${ONTO_WEB_PORT}/api/health" >/dev/null 2>&1; do
        [ "${elapsed}" -lt 120 ] || die \
            "Onto Modeling 120 秒内未就绪：http://127.0.0.1:${ONTO_WEB_PORT}/api/health；请查看 docker logs onto-modeling"
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log "Onto Modeling deployment finished: http://localhost:${ONTO_WEB_PORT}"
}

upgrade_core_services() {
    [ -f "${CONSUMER_DIR}/VERSION" ] && [ -f "${CONSUMER_DIR}/backend/.env" ] \
        && [ -f "${CONSUMER_DIR}/deploy/state/current.env" ] \
        || die "Consumer 安装目录缺少版本、配置或部署状态"
    precheck_onto_modeling "$(legacy_host_port mw-modeling 5100 || true)"
    precheck_knowledge_hub
    precheck_evaluation
    mkdir -p "${INSTALL_ROOT}"
    stage_dir="$(mktemp -d "${INSTALL_ROOT}/.core-upgrade.XXXXXX")" || die "无法创建升级暂存目录"
    cleanup_core_stage() {
        find "${stage_dir}" -mindepth 1 -delete 2>/dev/null || true
        rmdir "${stage_dir}" 2>/dev/null || true
    }
    trap cleanup_core_stage EXIT
    tar -xzf "${CONSUMER_PACKAGE}" -C "${stage_dir}"
    ensure_evaluation_service_token
    stop_consumer_containers_for_upgrade "${CONSUMER_DIR}"
    if ! run_consumer_upgrade "${stage_dir}/onto-agent" "${CONSUMER_DIR}"; then
        restore_consumer_containers_after_failure
        die "Consumer 升级失败"
    fi
    cleanup_core_stage
    trap - EXIT
    deploy_knowledge_hub
    deploy_onto_modeling
    deploy_evaluation
    log "Consumer、Onto Modeling、Hub 和评测升级完成；未改动 Modeling Workbench"
}

upgrade_install() {
    legacy_stack_dir="$(cd "${UPGRADE_FROM}" 2>/dev/null && pwd)" \
        || die "旧合并部署根目录不存在：${UPGRADE_FROM}"
    UPGRADE_FROM="${legacy_stack_dir}"
    CONSUMER_DIR="${UPGRADE_FROM}"
    upgrade_core_services
}

validate_package "${CONSUMER_PACKAGE}" onto-agent
validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling
validate_package "${KNOWLEDGE_HUB_PACKAGE}" knowledge-hub
validate_evaluation_package

if [ "${COMMAND}" = "upgrade" ]; then
    upgrade_core_services
    exit 0
fi

if [ -n "${UPGRADE_FROM}" ]; then
    upgrade_install
    exit 0
fi

[ ! -e "${CONSUMER_DIR}" ] \
    || die "Consumer 安装目录已存在：${CONSUMER_DIR}；本脚本只用于首次安装"
precheck_onto_modeling ""
precheck_knowledge_hub
precheck_evaluation

mkdir -p "${INSTALL_ROOT}"
log "解压 Consumer 发布包"
tar -xzf "${CONSUMER_PACKAGE}" -C "${INSTALL_ROOT}"

[ -f "${CONSUMER_DIR}/backend/.env.example" ] \
    || die "Consumer 发布包缺少 backend/.env.example"


cp "${CONSUMER_DIR}/backend/.env.example" "${CONSUMER_DIR}/backend/.env"
chmod 600 "${CONSUMER_DIR}/backend/.env"

MODELING_SERVICE_TOKEN_VALUE="$(generate_hex_secret 32)"
AUTH_BOOTSTRAP_ADMIN_PASSWORD_VALUE="Aa1!$(generate_hex_secret 8)"
trap 'unset MODELING_SERVICE_TOKEN_VALUE AUTH_BOOTSTRAP_ADMIN_PASSWORD_VALUE' EXIT

write_env_value \
    "${CONSUMER_DIR}/backend/.env" \
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

deploy_knowledge_hub
deploy_onto_modeling
deploy_evaluation

log "Consumer、Onto Modeling、Hub 和评测安装完成"
printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
[ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
    || printf '[full-install] 知识包 Hub：%s\n' "${KNOWLEDGE_HUB_DIR}"
[ -z "${ONTO_MODELING_PACKAGE}" ] \
    || printf '[full-install] Onto Modeling：%s\n' "${ONTO_MODELING_DIR}"
printf '[full-install] 管理员初始密码保存在：%s\n' "${CONSUMER_DIR}/backend/.env"
