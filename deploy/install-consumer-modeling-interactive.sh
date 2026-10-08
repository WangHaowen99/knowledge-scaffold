#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_ROOT=""
MODE=""
INSTALL_ROOT=""
BACKEND_PORT=""
MCP_SERVER_PORT=""
FRONTEND_PORT=""
MODELING_PORT=""
MODELING_MCP_PORT=""
ONTO_WEB_PORT=""
CONSUMER_PACKAGE=""
MODELING_PACKAGE=""
ONTO_MODELING_PACKAGE=""
PORT_MODE="default"
scope=""
OLD_MODELING_RUNNING=""
MODELING_HANDOFF_ACTIVE=false
MODELING_BACKUP_DIR=""
OLD_MODELING_IMAGE=""
OLD_MODELING_IMAGE_ID=""
OLD_AGENT_IMAGE=""
OLD_AGENT_IMAGE_ID=""

log() { printf '[interactive-deploy] %s\n' "$*"; }
die() { printf '[interactive-deploy][error] %s\n' "$*" >&2; exit 1; }
cleanup() {
    if [ "${MODELING_HANDOFF_ACTIVE:-false}" = true ]; then
        restore_preserved_modeling_containers 2>/dev/null || true
    fi
    if [ -n "${TMP_ROOT:-}" ] && [ -d "${TMP_ROOT}" ]; then
        find "${TMP_ROOT}" -mindepth 1 -delete 2>/dev/null || true
        rmdir "${TMP_ROOT}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

ask() {
    prompt="$1"
    printf '%s' "${prompt}" >/dev/tty || die '需要交互式终端'
    IFS= read -r answer </dev/tty || die '读取输入失败'
    [ -n "${answer}" ] || die '输入不能为空'
    printf '%s' "${answer}"
}

ask_optional() {
    prompt="$1"
    printf '%s' "${prompt}" >/dev/tty || die '需要交互式终端'
    IFS= read -r answer </dev/tty || die '读取输入失败'
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

validate_package() {
    package="$1"; top="$2"; label="$3"
    [ -f "${package}" ] || die "${label}不存在：${package}"
    [ ! -L "${package}" ] || die "${label}不能是符号链接"
    tar -tzf "${package}" >/dev/null || die "${label}不是有效 tar.gz"
    tar -tzf "${package}" | awk -v top="${top}" '
        BEGIN { ok=1 }
        { p=$0; if (p==top || p==top "/") next; if (index(p,top "/")!=1 || p ~ /(^|\/)\.\.?(\/|$)/ || p ~ /^\//) { ok=0; exit } }
        END { exit ok ? 0 : 1 }
    ' || die "${label}目录结构非法"
}

write_env_value() {
    file="$1"; key="$2"; value="$3"; temp="$(mktemp "${file}.tmp.XXXXXX")"
    chmod 600 "${temp}"
    awk -v key="${key}" -v value="${value}" '
        BEGIN { updated=0 }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" { if (!updated) { print key "=" value; updated=1 }; next }
        { print }
        END { if (!updated) print key "=" value }
    ' "${file}" >"${temp}"
    mv "${temp}" "${file}"; chmod 600 "${file}"
}

env_get() {
    local file="$1" key="$2" value
    value="$([ -f "${file}" ] && grep -E "^[[:space:]]*${key}[[:space:]]*=" "${file}" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

require_file() {
    [ -f "$1" ] || die "文件不存在：$1"
}

require_dir() {
    [ -d "$1" ] || die "目录不存在：$1"
}

collect_ports() {
    if [ "${PORT_MODE}" = 1 ]; then
        BACKEND_PORT=5055; MCP_SERVER_PORT=7488; FRONTEND_PORT=8888
        # 5100 归 Onto Modeling，Modeling Workbench 用 8100；两者会同时在跑
        MODELING_PORT=8100; MODELING_MCP_PORT=5188
        ONTO_WEB_PORT=5100
        return
    fi
    BACKEND_PORT="$(ask_port '请输入 Consumer Backend 宿主端口：')"
    MCP_SERVER_PORT="$(ask_port '请输入 Consumer MCP 宿主端口：')"
    FRONTEND_PORT="$(ask_port '请输入 Consumer Frontend 宿主端口：')"
    MODELING_PORT="$(ask_port '请输入 Modeling Workbench 宿主端口：')"
    MODELING_MCP_PORT="$(ask_port '请输入 Modeling MCP 宿主端口：')"
    ONTO_WEB_PORT="$(ask_port '请输入 Onto Modeling 宿主端口：')"
}

collect_consumer_ports() {
    BACKEND_PORT="$(ask_port '请输入 Consumer Backend 宿主端口：')"
    MCP_SERVER_PORT="$(ask_port '请输入 Consumer MCP 宿主端口：')"
    FRONTEND_PORT="$(ask_port '请输入 Consumer Frontend 宿主端口：')"
}

collect_modeling_ports() {
    MODELING_PORT="$(ask_port '请输入 Modeling Workbench 宿主端口：')"
    MODELING_MCP_PORT="$(ask_port '请输入 Modeling MCP 宿主端口：')"
}

load_upgrade_ports() {
    local consumer_env="${INSTALL_ROOT}/onto-agent/backend/.env"
    local modeling_env="${INSTALL_ROOT}/modeling/.env"
    local onto_env="${INSTALL_ROOT}/onto-modeling/.env"
    BACKEND_PORT="$(env_get "${consumer_env}" BACKEND_PORT)"; BACKEND_PORT="${BACKEND_PORT:-5055}"
    MCP_SERVER_PORT="$(env_get "${consumer_env}" MCP_SERVER_PORT)"; MCP_SERVER_PORT="${MCP_SERVER_PORT:-7488}"
    FRONTEND_PORT="$(env_get "${consumer_env}" FRONTEND_PORT)"; FRONTEND_PORT="${FRONTEND_PORT:-8888}"
    MODELING_PORT="$(env_get "${modeling_env}" MODELING_PORT)"; MODELING_PORT="${MODELING_PORT:-5100}"
    MODELING_MCP_PORT="$(env_get "${modeling_env}" MODELING_MCP_PORT)"; MODELING_MCP_PORT="${MODELING_MCP_PORT:-5188}"
    ONTO_WEB_PORT="$(env_get "${onto_env}" ONTO_WEB_PORT)"; ONTO_WEB_PORT="${ONTO_WEB_PORT:-5100}"
    log "升级将保留现有端口：Consumer=${BACKEND_PORT}/${MCP_SERVER_PORT}/${FRONTEND_PORT}，Modeling=${MODELING_PORT}/${MODELING_MCP_PORT}，Onto Modeling=${ONTO_WEB_PORT}"
}

# consumer 由本脚本部署、容器名固定，直接读它的实际网络。不按命名规律猜——
# 猜错会让 onto-modeling 的 compose 以 declared as external, but could not be found 失败。
detect_consumer_network() {
    docker container inspect onto-agent-backend \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null | head -n 1
}

deploy_onto_modeling() {
    local dir="${INSTALL_ROOT}/onto-modeling" env_file="${INSTALL_ROOT}/onto-modeling/.env" network
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
    # 评测要调 Modeling Workbench，容器网内地址与宿主端口无关
    grep -q '^ONTO_WEB_MODELING_URL=..*' "${env_file}" \
        || write_env_value "${env_file}" ONTO_WEB_MODELING_URL 'http://mw-modeling:5100'
    log "Onto Modeling 共享 Consumer 账号中心：网络 ${network}"
    bash "${dir}/deploy.sh"
    wait_for_onto_modeling
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

# Onto Modeling 与 Consumer、Modeling 同时运行，宿主端口不能与它们相同。
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
    local conflict
    while conflict="$(onto_port_conflict)"; do
        printf 'Onto Modeling 宿主端口 %s 与 %s 相同，请换一个端口。\n' \
            "${ONTO_WEB_PORT}" "${conflict}" >/dev/tty
        ONTO_WEB_PORT="$(ask_port '请输入 Onto Modeling 宿主端口：')"
    done
}

# Onto Modeling 排在 Consumer、Modeling 之后部署。能提前发现的问题都在动手前拦下，
# 避免 Consumer、Modeling 已经变更、Onto Modeling 才失败的半完成状态。
precheck_onto_modeling() {
    local dir="${INSTALL_ROOT}/onto-modeling" expected owner
    expected="$(cd "${dir}" 2>/dev/null && pwd || printf '%s' "${dir}")"
    if docker container inspect onto-modeling >/dev/null 2>&1; then
        owner="$(container_working_dir onto-modeling)"
        [ "${owner}" = "${expected}" ] \
            || die "容器 onto-modeling 属于 ${owner:-未知目录}，不是 ${expected}，拒绝接管"
    fi
    mkdir -p "${TMP_ROOT}/onto-preflight"
    tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${TMP_ROOT}/onto-preflight" onto-modeling/preflight.sh \
        || die 'Onto Modeling 发布包缺少 preflight.sh'
    log '开始执行 Onto Modeling 预检'
    # 此时还没有 .env；本脚本总是让 Onto Modeling 接入 Consumer 账号中心，按该模式预检
    (cd "${TMP_ROOT}/onto-preflight/onto-modeling" && ONTO_MODELING_SHARED_AUTH=1 bash ./preflight.sh) \
        || die 'Onto Modeling 预检未通过，尚未做任何变更'
}

container_working_dir() {
    docker container inspect "$1" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true
}

container_mount_volume() {
    docker container inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "'"$2"'"}}{{println .Name}}{{end}}{{end}}' 2>/dev/null | head -n 1
}

validate_modeling_installation() {
    local modeling_root="${INSTALL_ROOT}/modeling"
    local expected_root
    expected_root="$(cd "${modeling_root}" && pwd)"
    require_dir "${INSTALL_ROOT}/onto-agent"
    require_dir "${modeling_root}"
    require_file "${INSTALL_ROOT}/onto-agent/backend/.env"
    require_file "${modeling_root}/.env"
    [ -f "${modeling_root}/deploy/install.sh" ] || die "Modeling 部署脚本不存在：${modeling_root}/deploy/install.sh"
    [ -x "${modeling_root}/deploy/install.sh" ] || chmod +x "${modeling_root}/deploy/install.sh"

    local consumer_token modeling_token
    consumer_token="$(env_get "${INSTALL_ROOT}/onto-agent/backend/.env" MODELING_SERVICE_TOKEN)"
    modeling_token="$(env_get "${modeling_root}/.env" MODELING_SERVICE_TOKEN)"
    [ -n "${consumer_token}" ] || die 'Consumer 未配置 MODELING_SERVICE_TOKEN'
    [ "${#consumer_token}" -ge 32 ] || die 'Consumer MODELING_SERVICE_TOKEN 长度不足 32'
    [ "${modeling_token}" = "${consumer_token}" ] || die 'Consumer 与 Modeling 的 MODELING_SERVICE_TOKEN 不一致'

    local name working_dir modeling_volume agent_volume
    for name in mw-modeling mw-modeling-agent mw-modeling-nl2sql; do
        if docker container inspect "${name}" >/dev/null 2>&1; then
            working_dir="$(container_working_dir "${name}")"
            [ "${working_dir}" = "${expected_root}" ] || die "容器 ${name} 不属于当前 Modeling 目录：${working_dir:-unknown}"
        fi
    done
    docker volume inspect mw-modeling_modeling-data >/dev/null 2>&1 || die '原 Modeling 数据卷不存在：mw-modeling_modeling-data'
    docker volume inspect mw-modeling_modeling-agent-data >/dev/null 2>&1 || die '原 Modeling Agent 数据卷不存在：mw-modeling_modeling-agent-data'
    if docker container inspect mw-modeling >/dev/null 2>&1; then
        modeling_volume="$(container_mount_volume mw-modeling /data)"
        [ "${modeling_volume}" = mw-modeling_modeling-data ] || die "mw-modeling 未使用原数据卷：${modeling_volume:-unknown}"
    fi
    if docker container inspect mw-modeling-agent >/dev/null 2>&1; then
        agent_volume="$(container_mount_volume mw-modeling-agent /data)"
        [ "${agent_volume}" = mw-modeling_modeling-agent-data ] || die "mw-modeling-agent 未使用原数据卷：${agent_volume:-unknown}"
    fi
}

backup_modeling_volume() {
    local volume="$1" image="$2" archive="$3" backup_dir="$4"
    docker image inspect "${image}" >/dev/null 2>&1 || die "旧 Modeling 镜像不存在：${image}"
    docker run --rm --network=none --user 0:0 --entrypoint /bin/sh \
        -v "${volume}:/source:ro" -v "${backup_dir}:/backup:rw" "${image}" -c \
        'tar -C /source -czf "/backup/$1" . && chmod 600 "/backup/$1"' \
        backup-volume "${archive}" || die "备份数据卷失败：${volume}"
}

prepare_modeling_backup() {
    local backup_dir="$1"
    mkdir -p "${backup_dir}"; chmod 700 "${backup_dir}"
    cp "${INSTALL_ROOT}/modeling/.env" "${backup_dir}/modeling.env"
    chmod 600 "${backup_dir}/modeling.env"
    backup_modeling_volume mw-modeling_modeling-data "${OLD_MODELING_IMAGE}" modeling-data.tar.gz "${backup_dir}"
    backup_modeling_volume mw-modeling_modeling-agent-data "${OLD_AGENT_IMAGE}" modeling-agent-data.tar.gz "${backup_dir}"
}

stop_and_remove_modeling_containers() {
    local name running
    OLD_MODELING_RUNNING=""
    for name in mw-modeling mw-modeling-agent mw-modeling-nl2sql; do
        docker container inspect "${name}" >/dev/null 2>&1 || continue
        running="$(docker container inspect "${name}" --format '{{.State.Running}}')"
        [ "${running}" = true ] && docker stop "${name}" >/dev/null || true
        [ "${running}" = true ] && OLD_MODELING_RUNNING="${OLD_MODELING_RUNNING}${name} "
    done
    for name in mw-modeling-nl2sql mw-modeling mw-modeling-agent; do
        docker container inspect "${name}" >/dev/null 2>&1 || continue
        docker rm "${name}" >/dev/null || die "无法移除旧容器：${name}"
    done
}

restore_preserved_modeling_containers() {
    local name failed_root="${TMP_ROOT:-/tmp}/failed-modeling-$$"
    MODELING_HANDOFF_ACTIVE=false
    for name in mw-modeling mw-modeling-agent mw-modeling-nl2sql; do
        docker container inspect "${name}" >/dev/null 2>&1 || continue
        if [ "$(container_working_dir "${name}")" = "$(cd "${INSTALL_ROOT}/modeling" && pwd)" ]; then
            docker rm -f "${name}" >/dev/null 2>&1 || true
        fi
    done
    [ -z "${OLD_MODELING_IMAGE_ID}" ] || docker tag "${OLD_MODELING_IMAGE_ID}" "${OLD_MODELING_IMAGE}" >/dev/null 2>&1 || true
    [ -z "${OLD_AGENT_IMAGE_ID}" ] || docker tag "${OLD_AGENT_IMAGE_ID}" "${OLD_AGENT_IMAGE}" >/dev/null 2>&1 || true
    if [ -d "${MODELING_BACKUP_DIR}/modeling-release" ]; then
        [ ! -d "${INSTALL_ROOT}/modeling" ] || mv "${INSTALL_ROOT}/modeling" "${failed_root}" 2>/dev/null || true
        mv "${MODELING_BACKUP_DIR}/modeling-release" "${INSTALL_ROOT}/modeling" 2>/dev/null || true
    fi
    if [ -n "${OLD_MODELING_RUNNING}" ] && [ -f "${INSTALL_ROOT}/modeling/deploy/install.sh" ]; then
        MODELING_PORT="${MODELING_PORT}" MODELING_MCP_PORT="${MODELING_MCP_PORT}" \
            bash "${INSTALL_ROOT}/modeling/deploy/install.sh" >/dev/null 2>&1 || true
    fi
}

run_modeling_upgrade() {
    local backup_dir="$1" old_env="${backup_dir}/modeling.env" rc
    tar -xzf "${MODELING_PACKAGE}" -C "${TMP_ROOT}/modeling"
    [ -d "${TMP_ROOT}/modeling/modeling" ] || die 'Modeling 发布包目录结构非法'
    OLD_MODELING_IMAGE="$(docker container inspect mw-modeling --format '{{.Config.Image}}' 2>/dev/null || true)"
    OLD_AGENT_IMAGE="$(docker container inspect mw-modeling-agent --format '{{.Config.Image}}' 2>/dev/null || true)"
    [ -n "${OLD_MODELING_IMAGE}" ] || die '无法确定旧 mw-modeling 镜像，拒绝升级'
    [ -n "${OLD_AGENT_IMAGE}" ] || die '无法确定旧 mw-modeling-agent 镜像，拒绝升级'
    OLD_MODELING_IMAGE_ID="$(docker image inspect "${OLD_MODELING_IMAGE}" --format '{{.Id}}' 2>/dev/null || true)"
    OLD_AGENT_IMAGE_ID="$(docker image inspect "${OLD_AGENT_IMAGE}" --format '{{.Id}}' 2>/dev/null || true)"
    [ -n "${OLD_MODELING_IMAGE_ID}" ] || die "旧 Modeling 镜像不存在：${OLD_MODELING_IMAGE}"
    [ -n "${OLD_AGENT_IMAGE_ID}" ] || die "旧 Modeling Agent 镜像不存在：${OLD_AGENT_IMAGE}"
    MODELING_BACKUP_DIR="${backup_dir}"
    prepare_modeling_backup "${backup_dir}"
    MODELING_HANDOFF_ACTIVE=true
    stop_and_remove_modeling_containers
    mv "${INSTALL_ROOT}/modeling" "${backup_dir}/modeling-release"
    mv "${TMP_ROOT}/modeling/modeling" "${INSTALL_ROOT}/modeling"
    cp "${old_env}" "${INSTALL_ROOT}/modeling/.env"
    chmod 600 "${INSTALL_ROOT}/modeling/.env"
    if MODELING_PORT="${MODELING_PORT}" MODELING_MCP_PORT="${MODELING_MCP_PORT}" \
        bash "${INSTALL_ROOT}/modeling/deploy/install.sh"; then
        if [ "$(container_mount_volume mw-modeling /data)" != mw-modeling_modeling-data ] \
            || [ "$(container_mount_volume mw-modeling-agent /data)" != mw-modeling_modeling-agent-data ]; then
            log 'Modeling 升级后的数据卷不符合预期，尝试恢复旧版本'
            restore_preserved_modeling_containers
            return 1
        fi
        MODELING_HANDOFF_ACTIVE=false
        rm -rf "${MODELING_BACKUP_DIR}/modeling-release"
    else
        rc=$?
        log "Modeling 升级失败，尝试恢复旧容器"
        restore_preserved_modeling_containers
        return "${rc}"
    fi
}

check_dependencies() {
    for command_name in docker curl sha256sum flock tar gzip od awk sed tr mktemp; do
        command -v "${command_name}" >/dev/null 2>&1 || die "缺少命令：${command_name}"
    done
    docker info >/dev/null 2>&1 || die 'Docker daemon 不可用'
}

run_consumer_preflight() {
    local consumer_root="$1"
    local session_id report_root report_dir preflight_log rc status
    session_id="$(date -u +%Y%m%dT%H%M%SZ)-interactive-install-$$"
    report_root="${consumer_root}/.onto-runtime/opsctl/reports"
    preflight_log="${TMP_ROOT}/consumer-preflight.log"
    log '开始执行 Consumer 预检；将逐项显示依赖检查结果'
    set +e
    BACKEND_PORT="${BACKEND_PORT}" MCP_SERVER_PORT="${MCP_SERVER_PORT}" FRONTEND_PORT="${FRONTEND_PORT}" \
    OPSCTL_SESSION_ID="${session_id}" \
        bash "${consumer_root}/deploy/install.sh" --preflight-only 2>&1 | tee "${preflight_log}"
    rc=${PIPESTATUS[0]}
    set -e
    report_dir="${report_root}/${session_id}/01-preflight"
    printf '%s\n' '--- Consumer 预检逐项结果 ---'
    if [ -f "${report_dir}/summary.txt" ]; then
        cat "${report_dir}/summary.txt"
        status="$(sed -n 's/^status:[[:space:]]*//p' "${report_dir}/summary.txt" | head -n 1)"
    elif [ -f "${report_dir}/result.json" ]; then
        cat "${report_dir}/result.json"
        status="$(sed -n 's/.*"status":"\([A-Z]*\)".*/\1/p' "${report_dir}/result.json" | head -n 1)"
    else
        status=""
    fi
    if [ -s "${preflight_log}" ]; then
        printf '%s\n' '--- Consumer 预检原始输出 ---'
        cat "${preflight_log}"
        printf '%s\n' '--- Consumer 预检原始输出结束 ---'
    fi
    printf '%s\n' '--- Consumer 预检逐项结果结束 ---'
    if [ "${rc}" -ge 20 ] || [ "${status}" = BLOCK ] || [ "${status}" = ERROR ]; then
        die "Consumer 预检存在 BLOCK/ERROR，已停止部署；报告：${report_dir}"
    fi
    if [ "${rc}" -eq 10 ] || [ "${status}" = WARN ]; then
        log "Consumer 预检存在 WARN，按要求继续部署；报告：${report_dir}"
    fi
}

mode="$(ask_choice '请选择模式（1=首次部署，2=升级部署，默认1）：' 1 '1 2')"
if [ "${mode}" = 1 ]; then
    MODE="install"
    root_choice="$(ask_choice '安装根目录（1=/opt/onto-agent-stack，2=自定义，默认1）：' 1 '1 2')"
    if [ "${root_choice}" = 1 ]; then INSTALL_ROOT=/opt/onto-agent-stack; else INSTALL_ROOT="$(ask '请输入自定义安装根目录：')"; fi
else
    MODE="upgrade"
    INSTALL_ROOT="$(ask '请输入已分离部署的安装根目录：')"
    [ -d "${INSTALL_ROOT}/onto-agent" ] || die 'Consumer 安装目录不存在'
    [ -d "${INSTALL_ROOT}/modeling" ] || die 'Modeling 安装目录不存在'
    scope="$(ask_choice '升级范围（1=只升级 Consumer，2=只升级 Modeling，3=一起升级，默认3）：' 3 '1 2 3')"
fi

if [ "${MODE}" = install ]; then
    PORT_MODE="$(ask_choice '端口配置（1=默认端口，2=自定义端口，默认1）：' 1 '1 2')"
    collect_ports
    CONSUMER_PACKAGE="$(resolve_package "$(ask '请输入 Consumer 发布包路径：')" Consumer发布包)"
    MODELING_PACKAGE="$(resolve_package "$(ask '请输入 Modeling 发布包路径：')" Modeling发布包)"
    ONTO_MODELING_PACKAGE="$(ask_optional '请输入 Onto Modeling 发布包路径（留空跳过）：')"
    [ -z "${ONTO_MODELING_PACKAGE}" ] \
        || ONTO_MODELING_PACKAGE="$(resolve_package "${ONTO_MODELING_PACKAGE}" 'Onto Modeling发布包')"
else
    # 升级部署始终沿用已安装配置中的端口，不重新询问或覆盖端口。
    load_upgrade_ports
    if [ "${scope}" != 2 ]; then
        CONSUMER_PACKAGE="$(resolve_package "$(ask '请输入 Consumer 发布包路径：')" Consumer发布包)"
    fi
    if [ "${scope}" != 1 ]; then
        MODELING_PACKAGE="$(resolve_package "$(ask '请输入 Modeling 发布包路径：')" Modeling发布包)"
    fi
    # Onto Modeling 是独立组件，升不升级与上面的范围选择无关
    ONTO_MODELING_PACKAGE="$(ask_optional '请输入 Onto Modeling 发布包路径（留空跳过）：')"
    [ -z "${ONTO_MODELING_PACKAGE}" ] \
        || ONTO_MODELING_PACKAGE="$(resolve_package "${ONTO_MODELING_PACKAGE}" 'Onto Modeling发布包')"
fi
[ -z "${ONTO_MODELING_PACKAGE}" ] || ensure_onto_web_port_free
check_dependencies
[ -z "${CONSUMER_PACKAGE}" ] || validate_package "${CONSUMER_PACKAGE}" onto-agent Consumer发布包
[ -z "${MODELING_PACKAGE}" ] || validate_package "${MODELING_PACKAGE}" modeling Modeling发布包
[ -z "${ONTO_MODELING_PACKAGE}" ] \
    || validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling 'Onto Modeling发布包'
if [ "${MODE}" = upgrade ] && [ "${scope}" != 1 ]; then
    validate_modeling_installation
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/separated-interactive.XXXXXX")"
[ -z "${ONTO_MODELING_PACKAGE}" ] || precheck_onto_modeling
if [ "${MODE}" = install ]; then
    mkdir -p "${INSTALL_ROOT}"
    tar -xzf "${CONSUMER_PACKAGE}" -C "${INSTALL_ROOT}"
    tar -xzf "${MODELING_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -z "${ONTO_MODELING_PACKAGE}" ] || tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${INSTALL_ROOT}/onto-agent/backend/.env.example" ] || die "Consumer 发布包缺少 backend/.env.example"
    [ -f "${INSTALL_ROOT}/modeling/.env.example" ] || die "Modeling 发布包缺少 .env.example"
    cp "${INSTALL_ROOT}/onto-agent/backend/.env.example" "${INSTALL_ROOT}/onto-agent/backend/.env"
    cp "${INSTALL_ROOT}/modeling/.env.example" "${INSTALL_ROOT}/modeling/.env"
    chmod 600 "${INSTALL_ROOT}/onto-agent/backend/.env" "${INSTALL_ROOT}/modeling/.env"
    token="$(od -An -N 32 -tx1 /dev/urandom | tr -d '[:space:]')"
    password="Aa1!$(od -An -N 8 -tx1 /dev/urandom | tr -d '[:space:]')"
    write_env_value "${INSTALL_ROOT}/onto-agent/backend/.env" MODELING_SERVICE_TOKEN "${token}"
    write_env_value "${INSTALL_ROOT}/modeling/.env" MODELING_SERVICE_TOKEN "${token}"
    write_env_value "${INSTALL_ROOT}/onto-agent/backend/.env" AUTH_BOOTSTRAP_ADMIN_PASSWORD "${password}"
    run_consumer_preflight "${INSTALL_ROOT}/onto-agent"
    BACKEND_PORT="${BACKEND_PORT}" MCP_SERVER_PORT="${MCP_SERVER_PORT}" FRONTEND_PORT="${FRONTEND_PORT}" \
    MODELING_PORT="${MODELING_PORT}" MODELING_MCP_PORT="${MODELING_MCP_PORT}" \
        bash "${INSTALL_ROOT}/onto-agent/deploy/install.sh"
    MODELING_PORT="${MODELING_PORT}" MODELING_MCP_PORT="${MODELING_MCP_PORT}" \
        bash "${INSTALL_ROOT}/modeling/deploy/install.sh"
    [ -z "${ONTO_MODELING_PACKAGE}" ] || deploy_onto_modeling
else
    mkdir -p "${TMP_ROOT}/consumer" "${TMP_ROOT}/modeling"
    if [ "${scope}" != 2 ]; then
        tar -xzf "${CONSUMER_PACKAGE}" -C "${TMP_ROOT}/consumer"
        BACKEND_PORT="${BACKEND_PORT}" MCP_SERVER_PORT="${MCP_SERVER_PORT}" FRONTEND_PORT="${FRONTEND_PORT}" \
            bash "${TMP_ROOT}/consumer/onto-agent/deploy/upgrade.sh" "${INSTALL_ROOT}/onto-agent"
    fi
    if [ "${scope}" != 1 ]; then
        backup_dir="${INSTALL_ROOT}/backups/separated-interactive-upgrade-$(date +%Y%m%d-%H%M%S)"
        run_modeling_upgrade "${backup_dir}" \
            || die "Modeling 升级失败，备份位于 ${backup_dir}"
    fi
    if [ -n "${ONTO_MODELING_PACKAGE}" ]; then
        # onto-modeling 的部署是幂等的：数据在具名卷里，.env 不在发布包内
        # （包里只有 .env.example），解包覆盖交付物后重跑 deploy.sh 即可
        tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"
        deploy_onto_modeling
    fi
fi
log '交互部署完成'
