#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_ROOT=""

log() { printf '[interactive-upgrade] %s\n' "$*"; }
die() { printf '[interactive-upgrade][error] %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "${TMP_ROOT:-}" ] && [ -d "${TMP_ROOT}" ]; then
        find "${TMP_ROOT}" -mindepth 1 -delete 2>/dev/null || true
        rmdir "${TMP_ROOT}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

prompt_value() {
    prompt_text="$1"
    printf '%s' "${prompt_text}" >/dev/tty || die "需要交互式终端"
    IFS= read -r value </dev/tty || die "读取输入失败"
    [ -n "${value}" ] || die "输入不能为空"
    printf '%s' "${value}"
}

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

validate_package() {
    package="$1"
    top_dir="$2"
    label="$3"
    [ -f "${package}" ] || die "${label}不存在：${package}"
    [ ! -L "${package}" ] || die "${label}不能是符号链接：${package}"
    tar -tzf "${package}" >/dev/null || die "${label}不是有效的 tar.gz：${package}"
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
    ' || die "${label}目录结构非法：${package}"
}

check_command() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }

write_env_value() {
    file="$1"; key="$2"; value="$3"
    temp="$(mktemp "${file}.tmp.XXXXXX")" || die "无法更新 ${file}"
    chmod 600 "${temp}"
    awk -v key="${key}" -v value="${value}" '
        BEGIN { updated = 0 }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" { if (!updated) { print key "=" value; updated = 1 }; next }
        { print }
        END { if (!updated) print key "=" value }
    ' "${file}" >"${temp}" || die "无法更新 ${file}"
    mv "${temp}" "${file}"; chmod 600 "${file}"
}

env_get() {
    file="$1"; key="$2"
    value="$([ -f "${file}" ] && sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "${file}" | tail -n 1 || true)"
    value="$(printf '%s' "${value%$'\r'}" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    printf '%s' "${value}"
}

copy_env_key() {
    source_file="$1"; target_file="$2"; key="$3"
    line="$(sed -n "/^[[:space:]]*${key}[[:space:]]*=/p" "${source_file}" | tail -n 1)"
    [ -n "${line}" ] || return 0
    write_env_value "${target_file}" "${key}" "${line#*=}"
}

legacy_host_port() {
    docker port "$1" "$2/tcp" 2>/dev/null \
        | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1
}

old_install_dir="$(prompt_value '请输入旧的合并部署根目录，例如 /opt/onto-agent：')"
consumer_package="$(prompt_value '请输入新的 Consumer 镜像 tar 包路径：')"
modeling_package="$(prompt_value '请输入新的 Modeling 镜像 tar 包路径：')"

case "${old_install_dir}" in /*) ;; *) old_install_dir="$(pwd)/${old_install_dir}" ;; esac
case "${consumer_package}" in /*) ;; *) consumer_package="$(pwd)/${consumer_package}" ;; esac
case "${modeling_package}" in /*) ;; *) modeling_package="$(pwd)/${modeling_package}" ;; esac

consumer_package="$(resolve_package_path "${consumer_package}" 'Consumer 发布包')"
modeling_package="$(resolve_package_path "${modeling_package}" 'Modeling 发布包')"

[ -d "${old_install_dir}" ] || die "旧合并部署根目录不存在：${old_install_dir}"
[ -f "${old_install_dir}/VERSION" ] || die "旧目录缺少 VERSION：${old_install_dir}"
[ -f "${old_install_dir}/backend/.env" ] || die "旧目录缺少 backend/.env"
[ -f "${old_install_dir}/deploy/state/current.env" ] || die "旧目录缺少 deploy/state/current.env"
for command_name in docker tar gzip sha256sum flock awk sed mktemp tee; do check_command "${command_name}"; done
docker info >/dev/null 2>&1 || die "Docker daemon 不可用"
validate_package "${consumer_package}" onto-agent 'Consumer 发布包'
validate_package "${modeling_package}" modeling 'Modeling 发布包'

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/consumer-preflight.XXXXXX")" || die "无法创建预检暂存目录"
tar -xzf "${consumer_package}" -C "${TMP_ROOT}"
staged_root="${TMP_ROOT}/onto-agent"
opsctl="${staged_root}/deploy/opsctl"
manifest="${staged_root}/deploy/manifest.json"
[ -x "${opsctl}" ] || die "Consumer 发布包缺少 deploy/opsctl"
[ -f "${manifest}" ] || die "Consumer 发布包缺少 deploy/manifest.json"

# opsctl preflight reads the staged bundle's backend/.env for published ports.
# Reuse the old deployment's resolved configuration so custom ports are checked
# instead of falling back to the release defaults.
if [ -f "${staged_root}/backend/.env.example" ]; then
    cp "${old_install_dir}/backend/.env" "${staged_root}/backend/.env"
    chmod 600 "${staged_root}/backend/.env"
fi

session_id="$(date -u +%Y%m%dT%H%M%SZ)-interactive-upgrade-$$"
report_root="${old_install_dir}/.onto-runtime/opsctl/reports"
preflight_log="${TMP_ROOT}/preflight.log"

for port_spec in BACKEND_PORT:onto-agent-backend:5055 MCP_SERVER_PORT:onto-agent-mcp:7488 FRONTEND_PORT:onto-agent-frontend:80; do
    IFS=: read -r port_key container_name container_port <<EOF_PORT
${port_spec}
EOF_PORT
    host_port="$(legacy_host_port "${container_name}" "${container_port}" || true)"
    [ -n "${host_port}" ] || continue
    write_env_value "${old_install_dir}/backend/.env" "${port_key}" "${host_port}"
done

# Port values may have been recovered from the running legacy containers above;
# refresh the staged env file after that synchronization so preflight reads the
# actual published ports instead of release defaults.
if [ -f "${staged_root}/backend/.env.example" ]; then
    cp "${old_install_dir}/backend/.env" "${staged_root}/backend/.env"
    chmod 600 "${staged_root}/backend/.env"
fi

lock_file="${old_install_dir}/.consumer-deploy.lock"
exec 9>"${lock_file}" || die "无法打开部署锁：${lock_file}"
flock -n 9 || die "旧部署正在执行其他部署事务"
DEPLOY_TRANSACTION_LOCK_BACKEND=flock
DEPLOY_TRANSACTION_LOCK_FD=9
DEPLOY_TRANSACTION_LOCK_TOKEN="interactive-${session_id}"
DEPLOY_TRANSACTION_LOCK_HELD=true
export DEPLOY_TRANSACTION_LOCK_BACKEND DEPLOY_TRANSACTION_LOCK_FD
export DEPLOY_TRANSACTION_LOCK_TOKEN DEPLOY_TRANSACTION_LOCK_HELD

log "开始执行 Consumer 预检；将逐项显示依赖检查结果"
set +e
OPSCTL_SESSION_ID="${session_id}" \
OPSCTL_REPORT_ROOT="${report_root}" \
OPSCTL_MANAGED_BUNDLE_ROOT="${old_install_dir}" \
OPSCTL_SKIP_DEPLOY_LOCK=1 \
PREFLIGHT_ENV_FILE_OVERRIDE="${old_install_dir}/backend/.env" \
PREFLIGHT_RUNTIME_ROOT_OVERRIDE="${old_install_dir}/.onto-runtime" \
    "${opsctl}" preflight --manifest "${manifest}" --profile core 2>&1 \
    | tee "${preflight_log}"
preflight_status=${PIPESTATUS[0]}
set -e
preflight_report="${report_root}/${session_id}/01-preflight"
if [ -f "${preflight_report}/summary.txt" ]; then
    printf '%s\n' '--- Consumer 预检逐项结果 ---'
    cat "${preflight_report}/summary.txt"
    printf '%s\n' '--- Consumer 预检逐项结果结束 ---'
fi
if [ ! -s "${preflight_log}" ]; then
    printf '[interactive-upgrade][error] 预检没有输出，详细报告：%s\n' \
        "${preflight_report}" >&2
fi

if [ "${preflight_status}" -ge 20 ]; then
    die "Consumer 预检存在 BLOCK/ERROR，已停止部署；报告：${preflight_report}"
fi
if [ "${preflight_status}" -eq 10 ]; then
    log '预检存在 WARN，按要求继续升级'
fi

install_root="$(dirname "${old_install_dir}")"
consumer_dir="${old_install_dir}"
modeling_dir="${install_root}/modeling"

container_mount_volume() {
    docker container inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "'"$2"'"}}{{println .Name}}{{end}}{{end}}' 2>/dev/null | head -n 1
}
container_owner_matches() {
    [ "$(docker container inspect "$1" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)" = "$2" ]
}
stop_owned_containers() {
    install_dir="$1"; variable="$2"; names="$3"
    for name in ${names}; do
        docker container inspect "${name}" >/dev/null 2>&1 || continue
        container_owner_matches "${name}" "${install_dir}" || die "容器 ${name} 不属于 ${install_dir}，拒绝停止"
        running="$(docker container inspect "${name}" --format '{{.State.Running}}' 2>/dev/null || true)"
        if [ "${running}" = true ]; then
            docker stop "${name}" >/dev/null || die "停止容器失败：${name}"
            eval "${variable}=\"\${${variable}:-} ${name}\""
        fi
    done
}
backup_volume() {
    volume="$1"; image="$2"; archive="$3"; backup_dir="$4"
    docker image inspect "${image}" >/dev/null 2>&1 || die "旧镜像不存在：${image}"
    docker run --rm --network=none --user 0:0 --entrypoint /bin/sh \
        -v "${volume}:/source:ro" -v "${backup_dir}:/backup:rw" "${image}" -c \
        'tar -C /source -czf "/backup/$1" . && chmod 600 "/backup/$1"' \
        backup-volume "${archive}" || die "备份数据卷失败：${volume}"
}
run_consumer_upgrade() {
    release_root="$1"; install_dir="$2"
    if bash "${release_root}/deploy/upgrade.sh" "${install_dir}"; then rc=0; else rc=$?; fi
    case "${rc}" in 0) return 0 ;; 10) log 'Consumer 升级完成但存在 WARN，继续升级 Modeling'; return 0 ;; *) return "${rc}" ;; esac
}

upgrade_legacy_deployment() {
    legacy_modeling_dir="${consumer_dir}/deploy/modeling"
    [ -d "${legacy_modeling_dir}" ] || die "旧合并部署缺少 deploy/modeling"
    for name in mw-modeling mw-modeling-agent; do
        container_owner_matches "${name}" "${consumer_dir}" || die "容器 ${name} 不属于旧合并部署目录"
    done
    modeling_volume="$(container_mount_volume mw-modeling /data)"
    agent_volume="$(container_mount_volume mw-modeling-agent /data)"
    [ "${modeling_volume}" = mw-modeling_modeling-data ] || die "旧 Modeling 数据卷不符合预期"
    [ "${agent_volume}" = mw-modeling_modeling-agent-data ] || die "旧 Agent 数据卷不符合预期"
    modeling_image="$(docker container inspect mw-modeling --format '{{.Config.Image}}')"
    agent_image="$(docker container inspect mw-modeling-agent --format '{{.Config.Image}}')"
    legacy_modeling_port="${MODELING_PORT:-$(legacy_host_port mw-modeling 5100 || true)}"
    legacy_modeling_mcp_port="${MODELING_MCP_PORT:-$(legacy_host_port mw-modeling 5188 || true)}"

    backup_dir="${install_root}/backups/interactive-legacy-upgrade-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "${backup_dir}"; chmod 700 "${backup_dir}"
    stage_dir="${TMP_ROOT}/packages"; mkdir -p "${stage_dir}"
    tar -xzf "${consumer_package}" -C "${stage_dir}"
    tar -xzf "${modeling_package}" -C "${stage_dir}"
    cp "${consumer_dir}/backend/.env" "${backup_dir}/consumer.env"; chmod 600 "${backup_dir}/consumer.env"

    stop_owned_containers "${consumer_dir}" STOPPED_CONSUMER_CONTAINERS \
        'onto-agent-frontend onto-agent-mcp onto-agent-backend onto-agent-jiuwenbox onto-agent-auth-server'
    stop_owned_containers "${consumer_dir}" STOPPED_MODELING_CONTAINERS \
        'mw-modeling mw-modeling-agent mw-modeling-nl2sql'
    backup_volume "${modeling_volume}" "${modeling_image}" modeling-data.tar.gz "${backup_dir}"
    backup_volume "${agent_volume}" "${agent_image}" modeling-agent-data.tar.gz "${backup_dir}"

    run_consumer_upgrade "${stage_dir}/onto-agent" "${consumer_dir}" \
        || die "Consumer 升级失败，备份位于 ${backup_dir}"
    upgraded_token="$(env_get "${consumer_dir}/backend/.env" MODELING_SERVICE_TOKEN)"
    [ "${upgraded_token}" = "${consumer_token}" ] || die 'Consumer 升级后服务令牌变化'

    [ ! -e "${modeling_dir}" ] || die "独立 Modeling 目录已存在：${modeling_dir}"
    mv "${stage_dir}/modeling" "${modeling_dir}"
    cp "${modeling_dir}/.env.example" "${modeling_dir}/.env"
    chmod 600 "${modeling_dir}/.env"
    for key in MODELING_LLM_BASE_URL MODELING_LLM_MODEL MODELING_LLM_API_KEY \
               MODELING_TASK_LLM_BASE_URL MODELING_TASK_LLM_MODEL MODELING_TASK_LLM_API_KEY \
               MODELING_MCP_TOKEN_SECRET MODELING_PORT MODELING_MCP_PORT \
               AUTH_ISSUER_URL AUTH_JWT_ISSUER AUTH_JWT_AUDIENCE CONSUMER_API_URL; do
        copy_env_key "${consumer_dir}/backend/.env" "${modeling_dir}/.env" "${key}"
    done
    write_env_value "${modeling_dir}/.env" MODELING_SERVICE_TOKEN "${consumer_token}"
    [ -n "${legacy_modeling_port}" ] \
        && write_env_value "${modeling_dir}/.env" MODELING_PORT "${legacy_modeling_port}"
    [ -n "${legacy_modeling_mcp_port}" ] \
        && write_env_value "${modeling_dir}/.env" MODELING_MCP_PORT "${legacy_modeling_mcp_port}"
    for name in mw-modeling-nl2sql mw-modeling mw-modeling-agent; do
        docker container inspect "${name}" >/dev/null 2>&1 && docker rm "${name}" >/dev/null || true
    done
    HANDOFF_STARTED=true
    (cd "${modeling_dir}" && bash deploy/install.sh) || die "Modeling 部署失败，备份位于 ${backup_dir}"
    [ "$(container_mount_volume mw-modeling /data)" = "${modeling_volume}" ] || die 'Modeling 未复用原数据卷'
    [ "$(container_mount_volume mw-modeling-agent /data)" = "${agent_volume}" ] || die 'Agent 未复用原数据卷'
    log "旧合并部署迁移完成，备份位于 ${backup_dir}"
}

consumer_token="$(env_get "${consumer_dir}/backend/.env" MODELING_SERVICE_TOKEN)"
[ ! -d "${modeling_dir}" ] || die \
    "检测到已分离 Modeling 目录：${modeling_dir}；请使用独立部署升级脚本"
log "检测到旧合并部署，执行迁移到独立 Modeling"
upgrade_legacy_deployment
