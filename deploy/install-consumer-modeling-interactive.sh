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
ONTO_MODELING_PACKAGE=""
KNOWLEDGE_HUB_PACKAGE=""
EVALUATION_PACKAGE=""
CONSUMER_DIR=""
PORT_MODE="default"

log() { printf '[interactive-deploy] %s\n' "$*"; }
die() { printf '[interactive-deploy][error] %s\n' "$*" >&2; exit 1; }
cleanup() {
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

# 评测交付物：发布包（顶层目录 eval-service），或完整离线包中的 eval-service 目录
validate_evaluation_package() {
    local package="$1" label="$2" relative entry
    if [ -f "${package}" ]; then
        validate_package "${package}" eval-service "${label}"
        for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
            entry="$(tar -tvzf "${package}" "eval-service/${relative}" 2>/dev/null || true)"
            [ "${entry#-}" != "${entry}" ] || die "${label}缺少普通文件 eval-service/${relative}"
        done
        EVALUATION_PACKAGE="$(cd "$(dirname "${package}")" && pwd)/$(basename "${package}")"
        return 0
    fi
    [ -d "${package}" ] || die "${label}不存在：${package}"
    [ ! -L "${package}" ] || die "${label}不能是符号链接：${package}"
    [ ! -L "${package}/deploy" ] || die "${label}的 deploy 不能是符号链接"
    for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
        [ -f "${package}/${relative}" ] && [ ! -L "${package}/${relative}" ] \
            || die "${label}缺少普通文件 ${relative}"
    done
    EVALUATION_PACKAGE="$(cd "${package}" && pwd)"
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

collect_consumer_ports() {
    BACKEND_PORT="$(ask_port '请输入 Consumer Backend 宿主端口：')"
    MCP_SERVER_PORT="$(ask_port '请输入 Consumer MCP 宿主端口：')"
    FRONTEND_PORT="$(ask_port '请输入 Consumer Frontend 宿主端口：')"
}

load_upgrade_ports() {
    local consumer_env="${INSTALL_ROOT}/onto-agent/backend/.env"
    local modeling_env="${INSTALL_ROOT}/modeling/.env"
    local onto_env="${INSTALL_ROOT}/onto-modeling/.env"
    BACKEND_PORT="$(env_get "${consumer_env}" BACKEND_PORT)"; BACKEND_PORT="${BACKEND_PORT:-5055}"
    MCP_SERVER_PORT="$(env_get "${consumer_env}" MCP_SERVER_PORT)"; MCP_SERVER_PORT="${MCP_SERVER_PORT:-7488}"
    FRONTEND_PORT="$(env_get "${consumer_env}" FRONTEND_PORT)"; FRONTEND_PORT="${FRONTEND_PORT:-8888}"
    # 已有的 Modeling Workbench 不由本脚本升级，但仍占用宿主端口，仅用于避开端口冲突
    MODELING_PORT="$(env_get "${modeling_env}" MODELING_PORT)"; MODELING_PORT="${MODELING_PORT:-8100}"
    MODELING_MCP_PORT="$(env_get "${modeling_env}" MODELING_MCP_PORT)"; MODELING_MCP_PORT="${MODELING_MCP_PORT:-5188}"
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
    # 评测要调 Modeling Workbench，容器网内地址与宿主端口无关
    grep -q '^ONTO_WEB_MODELING_URL=..*' "${env_file}" \
        || write_env_value "${env_file}" ONTO_WEB_MODELING_URL 'http://mw-modeling:5100'
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
    local conflict
    while conflict="$(onto_port_conflict)"; do
        printf 'Onto Modeling 宿主端口 %s 与 %s 相同，请换一个端口。\n' \
            "${ONTO_WEB_PORT}" "${conflict}" >/dev/tty
        ONTO_WEB_PORT="$(ask_port '请输入 Onto Modeling 宿主端口：')"
    done
}

ensure_evaluation_service_token() {
    local env_file="$1" secret modeling_token
    secret="$(env_get "${env_file}" EVALUATION_SERVICE_TOKEN)"
    if [ -z "${secret}" ]; then
        secret="$(od -An -N 32 -tx1 /dev/urandom | tr -d '[:space:]')"
    fi
    [ "${#secret}" -ge 32 ] || die 'EVALUATION_SERVICE_TOKEN 至少需要 32 个字符'
    modeling_token="$(env_get "${env_file}" MODELING_SERVICE_TOKEN)"
    [ "${secret}" != "${modeling_token}" ] \
        || die 'EVALUATION_SERVICE_TOKEN 不能与 MODELING_SERVICE_TOKEN 相同'
    write_env_value "${env_file}" EVALUATION_SERVICE_TOKEN "${secret}"
    unset secret modeling_token
}

stage_evaluation_package() {
    local dir="${INSTALL_ROOT}/eval-service" relative
    [ ! -L "${dir}" ] && [ ! -L "${dir}/deploy" ] || die '评测安装目录不能是符号链接'
    mkdir -p "${dir}/deploy" || die '无法创建评测安装目录'
    if [ -f "${EVALUATION_PACKAGE}" ]; then
        for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
            [ ! -L "${dir}/${relative}" ] || die "评测目标文件不能是符号链接：${relative}"
        done
        tar -xzf "${EVALUATION_PACKAGE}" -C "${dir}" --strip-components=1 eval-service/deploy/deploy.sh \
            eval-service/deploy/docker-compose.eval-service.yml eval-service/evaluation-image.tar eval-service/IMAGE \
            || die "无法解压评测发布包：${EVALUATION_PACKAGE}"
        return 0
    fi
    [ "${EVALUATION_PACKAGE}" != "$(cd "${dir}" && pwd)" ] || return 0
    # 仅覆盖交付物，保留 deploy/.env 中的数据加密密钥、端口和其他运行配置。
    for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
        [ ! -L "${dir}/${relative}" ] || die "评测目标文件不能是符号链接：${relative}"
        cp "${EVALUATION_PACKAGE}/${relative}" "${dir}/${relative}" || die "无法复制评测交付物：${relative}"
    done
}

deploy_evaluation() {
    local dir="${INSTALL_ROOT}/eval-service" network hub_token consumer_token modeling_token
    [ -f "${dir}/deploy/deploy.sh" ] || die "评测服务发布目录缺少 deploy/deploy.sh：${dir}"
    network="$(detect_consumer_network)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    hub_token="$(env_get "${INSTALL_ROOT}/knowledge-hub/.env" HUB_EVALUATION_SERVICE_TOKEN)"
    consumer_token="$(env_get "${CONSUMER_DIR:-${INSTALL_ROOT}/onto-agent}/backend/.env" EVALUATION_SERVICE_TOKEN)"
    # Onto Modeling 的 deploy.sh 生成入站凭据；未部署时留空，评测只告警、不发完成通知
    modeling_token="$(env_get "${INSTALL_ROOT}/onto-modeling/.env" ONTO_EVALUATION_SERVICE_TOKEN)"
    [ -n "${hub_token}" ] || [ -n "${EVAL_ENV_FILE:-}" ] \
        || die 'Hub 缺少评测凭据，请先更新知识包 Hub'
    log "部署独立评测服务：${dir}（网络 ${network}）"
    (cd "${dir}" \
        && CONSUMER_NETWORK="${network}" \
        HUB_EVALUATION_SERVICE_TOKEN="${hub_token}" \
        EVALUATION_SERVICE_TOKEN="${consumer_token}" \
        EVAL_MODELING_SERVICE_TOKEN="${modeling_token}" \
        bash ./deploy/deploy.sh) \
        || die "独立评测服务部署失败；修复后可单独执行 bash ${dir}/deploy/deploy.sh"
    unset hub_token consumer_token modeling_token
    local elapsed=0
    until docker exec knowledge-evaluation python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5180/evaluation/api/health', timeout=3)" \
        >/dev/null 2>&1; do
        [ "${elapsed}" -lt 60 ] || die '独立评测服务 60 秒内未就绪，请查看 docker logs knowledge-evaluation'
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log '独立评测服务已就绪（默认访问 http://localhost:5180/evaluation/，端口以评测运行配置为准）'
}

precheck_evaluation() {
    local dir="${INSTALL_ROOT}/eval-service" expected owner
    [ ! -L "${dir}" ] && [ ! -L "${dir}/deploy" ] || die '评测安装目录不能是符号链接'
    expected="$(cd "${dir}/deploy" 2>/dev/null && pwd || printf '%s' "${dir}/deploy")"
    if docker container inspect knowledge-evaluation >/dev/null 2>&1; then
        owner="$(container_working_dir knowledge-evaluation)"
        [ "${owner}" = "${expected}" ] \
            || die "容器 knowledge-evaluation 属于 ${owner:-未知目录}，不是 ${expected}，拒绝接管"
    fi
    if [ -n "${EVAL_ENV_FILE:-}" ]; then
        [ -f "${EVAL_ENV_FILE}" ] && [ -r "${EVAL_ENV_FILE}" ] || die 'EVAL_ENV_FILE 不存在或不可读'
        EVAL_ENV_FILE="$(cd "$(dirname "${EVAL_ENV_FILE}")" && pwd)/$(basename "${EVAL_ENV_FILE}")"
        export EVAL_ENV_FILE
    fi
}

# Onto Modeling 排在 Consumer、Hub 之后部署。能提前发现的问题都在动手前拦下，
# 避免 Consumer 已经变更、Onto Modeling 才失败的半完成状态。
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
    PORT_MODE="$(ask_choice '端口配置（1=默认端口，2=自定义端口，默认1）：' 1 '1 2')"
    collect_ports
else
    MODE="upgrade"
    INSTALL_ROOT="$(ask '请输入已分离部署的安装根目录：')"
    [ -d "${INSTALL_ROOT}/onto-agent" ] || die 'Consumer 安装目录不存在'
    load_upgrade_ports
fi
CONSUMER_PACKAGE="$(resolve_package "$(ask '请输入 Consumer 发布包路径：')" Consumer发布包)"
ONTO_MODELING_PACKAGE="$(resolve_package "$(ask '请输入 Onto Modeling 发布包路径：')" 'Onto Modeling发布包')"
KNOWLEDGE_HUB_PACKAGE="$(resolve_package "$(ask '请输入知识包 Hub 发布包路径：')" '知识包 Hub发布包')"
EVALUATION_PACKAGE="$(resolve_package "$(ask '请输入独立评测服务发布包路径：')" '独立评测服务发布包')"
ensure_onto_web_port_free
check_dependencies
validate_package "${CONSUMER_PACKAGE}" onto-agent Consumer发布包
validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling 'Onto Modeling发布包'
validate_package "${KNOWLEDGE_HUB_PACKAGE}" knowledge-hub '知识包 Hub发布包'
validate_evaluation_package "${EVALUATION_PACKAGE}" '独立评测服务发布包'
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/separated-interactive.XXXXXX")"
precheck_onto_modeling
precheck_knowledge_hub
precheck_evaluation
if [ "${MODE}" = install ]; then
    mkdir -p "${INSTALL_ROOT}"
    tar -xzf "${CONSUMER_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${INSTALL_ROOT}/onto-agent/backend/.env.example" ] || die "Consumer 发布包缺少 backend/.env.example"
    cp "${INSTALL_ROOT}/onto-agent/backend/.env.example" "${INSTALL_ROOT}/onto-agent/backend/.env"
    chmod 600 "${INSTALL_ROOT}/onto-agent/backend/.env"
    token="$(od -An -N 32 -tx1 /dev/urandom | tr -d '[:space:]')"
    password="Aa1!$(od -An -N 8 -tx1 /dev/urandom | tr -d '[:space:]')"
    write_env_value "${INSTALL_ROOT}/onto-agent/backend/.env" MODELING_SERVICE_TOKEN "${token}"
    write_env_value "${INSTALL_ROOT}/onto-agent/backend/.env" AUTH_BOOTSTRAP_ADMIN_PASSWORD "${password}"
    ensure_evaluation_service_token "${INSTALL_ROOT}/onto-agent/backend/.env"
    stage_evaluation_package
    run_consumer_preflight "${INSTALL_ROOT}/onto-agent"
    BACKEND_PORT="${BACKEND_PORT}" MCP_SERVER_PORT="${MCP_SERVER_PORT}" FRONTEND_PORT="${FRONTEND_PORT}" \
        bash "${INSTALL_ROOT}/onto-agent/deploy/install.sh"
else
    mkdir -p "${TMP_ROOT}/consumer"
    ensure_evaluation_service_token "${INSTALL_ROOT}/onto-agent/backend/.env"
    tar -xzf "${CONSUMER_PACKAGE}" -C "${TMP_ROOT}/consumer"
    # 独立 Consumer 发布包的 upgrade.sh 只接受安装目录；退出码 10 表示升级完成但验收有 WARN
    if BACKEND_PORT="${BACKEND_PORT}" MCP_SERVER_PORT="${MCP_SERVER_PORT}" FRONTEND_PORT="${FRONTEND_PORT}" \
        bash "${TMP_ROOT}/consumer/onto-agent/deploy/upgrade.sh" "${INSTALL_ROOT}/onto-agent"; then
        upgrade_rc=0
    else
        upgrade_rc=$?
    fi
    case "${upgrade_rc}" in
        0) ;;
        10) log 'Consumer 升级完成，存在需要关注的警告，继续部署后续服务' ;;
        *) die "Consumer 升级失败（退出码 ${upgrade_rc}）" ;;
    esac
    stage_evaluation_package
fi
tar -xzf "${KNOWLEDGE_HUB_PACKAGE}" -C "${INSTALL_ROOT}"
deploy_knowledge_hub
tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"
deploy_onto_modeling
deploy_evaluation
log '交互部署完成：Consumer、Onto Modeling、Hub 和评测'
