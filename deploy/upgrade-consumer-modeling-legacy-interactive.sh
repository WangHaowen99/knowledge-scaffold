#!/usr/bin/env bash
# Upgrade a legacy directory and migrate merged Auth ownership when needed.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ask() {
    local prompt="$1" answer
    printf '%s' "${prompt}" >/dev/tty
    IFS= read -r answer </dev/tty || exit 1
    [ -n "${answer}" ] || { printf '输入不能为空\n' >&2; exit 1; }
    printf '%s' "${answer}"
}

prompt_port() {
    while :; do
        value="$(prompt_value "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        case "${value}" in
            ''|*[!0-9]*) ;;
            *) if [ "${value}" -ge 1 ] && [ "${value}" -le 65535 ]; then printf '%s' "${value}"; return 0; fi ;;
        esac
        printf '端口必须是1到65535之间的整数，请重新输入。\n' >/dev/tty
    done
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

# 评测交付物：发布包（顶层目录 eval-service），或完整离线包中的 eval-service 目录
validate_evaluation_package() {
    local package="$1" label="$2" relative entry
    if [ -f "${package}" ]; then
        validate_package "${package}" eval-service "${label}"
        for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
            entry="$(tar -tvzf "${package}" "eval-service/${relative}" 2>/dev/null || true)"
            [ "${entry#-}" != "${entry}" ] || die "${label}缺少普通文件 eval-service/${relative}"
        done
        evaluation_package="$(cd "$(dirname "${package}")" && pwd)/$(basename "${package}")"
        return 0
    fi
    [ -d "${package}" ] || die "${label}不存在：${package}"
    [ ! -L "${package}" ] || die "${label}不能是符号链接：${package}"
    [ ! -L "${package}/deploy" ] || die "${label}的 deploy 不能是符号链接"
    for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
        [ -f "${package}/${relative}" ] && [ ! -L "${package}/${relative}" ] \
            || die "${label}缺少普通文件 ${relative}"
    done
    evaluation_package="$(cd "${package}" && pwd)"
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

# 先读容器配置里的端口绑定：容器停止后仍然有效；docker port 只对运行中的容器有结果
legacy_host_port() {
    host_port="$(docker container inspect "$1" \
        --format '{{range $port, $bindings := .HostConfig.PortBindings}}{{if eq $port "'"$2"'/tcp"}}{{range $bindings}}{{println .HostPort}}{{end}}{{end}}{{end}}' \
        2>/dev/null | sed -n '/^[0-9][0-9]*$/p' | head -n 1 || true)"
    [ -n "${host_port}" ] || host_port="$(docker port "$1" "$2/tcp" 2>/dev/null \
        | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1 || true)"
    printf '%s' "${host_port}"
}

package_env_value() {
    tar -xzOf "$1" "$2" 2>/dev/null \
        | sed -n "s/^[[:space:]]*$3[[:space:]]*=[[:space:]]*//p" | tail -n 1 || true
}

# Onto Modeling 排在 Consumer、Hub 之后部署。端口、容器归属和预检都在动手前完成，
# 避免 Consumer 已经升级、Onto Modeling 才失败的半完成状态。
precheck_onto_modeling() {
    # Modeling Workbench 不在本次升级范围内，但旧实例仍占用宿主端口
    modeling_port="$(legacy_host_port mw-modeling 5100 || true)"
    modeling_port="${modeling_port:-$(env_get "${old_install_dir}/backend/.env" MODELING_PORT)}"
    # 旧合并部署的 Modeling 通常仍在 5100，与 Onto Modeling 默认端口相同
    used_ports=" ${modeling_port} $(legacy_host_port mw-modeling 5188 || true)"
    used_ports="${used_ports} $(legacy_host_port onto-agent-backend 5055 || true)"
    used_ports="${used_ports} $(legacy_host_port onto-agent-mcp 7488 || true)"
    used_ports="${used_ports} $(legacy_host_port onto-agent-frontend 80 || true) "
    onto_web_port="$(env_get "${onto_modeling_dir}/.env" ONTO_WEB_PORT)"
    onto_web_port="${onto_web_port:-$(package_env_value "${onto_package}" onto-modeling/.env.example ONTO_WEB_PORT)}"
    while [ -z "${onto_web_port}" ] || case "${used_ports}" in *" ${onto_web_port} "*) true ;; *) false ;; esac; do
        [ -z "${onto_web_port}" ] || printf 'Onto Modeling 宿主端口 %s 已被当前部署的其他服务使用，请换一个端口。\n' \
            "${onto_web_port}" >/dev/tty
        onto_web_port="$(prompt_port '请输入 Onto Modeling 宿主端口：')"
    done
    if docker container inspect onto-modeling >/dev/null 2>&1; then
        owner="$(docker container inspect onto-modeling --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
        [ "${owner}" = "${onto_modeling_dir}" ] \
            || die "容器 onto-modeling 属于 ${owner:-未知目录}，不是 ${onto_modeling_dir}，拒绝接管"
    fi
    mkdir -p "${TMP_ROOT}/onto-preflight"
    tar -xzf "${onto_package}" -C "${TMP_ROOT}/onto-preflight" onto-modeling/preflight.sh \
        || die 'Onto Modeling 发布包缺少 preflight.sh'
    log '开始执行 Onto Modeling 预检'
    # 此时还没有 .env；本脚本总是让 Onto Modeling 接入 Consumer 账号中心，按该模式预检
    (cd "${TMP_ROOT}/onto-preflight/onto-modeling" && ONTO_MODELING_SHARED_AUTH=1 bash ./preflight.sh) \
        || die 'Onto Modeling 预检未通过，尚未做任何变更'
}

deploy_onto_modeling() {
    env_file="${onto_modeling_dir}/.env"
    log "部署 Onto Modeling：${onto_modeling_dir}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可
    tar -xzf "${onto_package}" -C "$(dirname "${onto_modeling_dir}")"
    # 网络按运行中的 Consumer 容器现场探测，不按命名规律猜
    network="$(docker container inspect onto-agent-backend \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null | head -n 1)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    if [ ! -f "${env_file}" ]; then
        [ -f "${onto_modeling_dir}/.env.example" ] || die 'Onto Modeling 发布包缺少 .env.example'
        cp "${onto_modeling_dir}/.env.example" "${env_file}"
    fi
    chmod 600 "${env_file}"
    write_env_value "${env_file}" ONTO_WEB_PORT "${onto_web_port}"
    write_env_value "${env_file}" ONTO_MODELING_EXTERNAL_AUTH_NETWORK "${network}"
    # consumer 的 jiuwenbox socket 卷：onto-modeling 只读挂载后，脚本运行验证在沙箱中执行；
    # 现场探测为准，探测不到写空值，验证改在 onto-modeling 容器内执行
    write_env_value "${env_file}" ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME \
        "$(container_mount_volume onto-agent-backend /run/jiuwenbox || true)"
    # 与独立部署的知识包 Hub 使用同一服务凭据；未部署 Hub 时保留原值（缺失由 Onto 部署脚本生成）
    hub_token="$(env_get "${hub_dir}/.env" HUB_SERVICE_TOKEN)"
    [ -z "${hub_token}" ] || write_env_value "${env_file}" ONTO_HUB_SERVICE_TOKEN "${hub_token}"
    unset hub_token
    log "Onto Modeling 共享 Consumer 账号中心：网络 ${network}"
    (cd "${onto_modeling_dir}" && bash ./deploy.sh) || die \
        "Onto Modeling 部署失败；Consumer 与知识包 Hub 已完成，修复后可单独执行 bash ${onto_modeling_dir}/deploy.sh"
    health_url="http://127.0.0.1:${onto_web_port}/api/health"
    elapsed=0
    until curl -fsS --noproxy '*' --max-time 5 "${health_url}" >/dev/null 2>&1; do
        [ "${elapsed}" -lt 120 ] || die "Onto Modeling 120 秒内未就绪：${health_url}；请查看 docker logs onto-modeling"
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log "Onto Modeling deployment finished: http://localhost:${onto_web_port}"
}

# Hub 容器名固定：已有容器不属于迁移目标目录时，在任何变更之前拒绝接管
precheck_knowledge_hub() {
    if docker container inspect knowledge-hub >/dev/null 2>&1; then
        owner="$(docker container inspect knowledge-hub --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
        [ "${owner}" = "${hub_dir}" ] \
            || die "容器 knowledge-hub 属于 ${owner:-未知目录}，不是 ${hub_dir}，拒绝接管"
    fi
}

# Hub 是独立 compose 项目，排在 Onto Modeling 之前部署；服务凭据沿用已部署 Onto Modeling 的值，
# 两边都没有时由 Hub 部署脚本生成，随后 deploy_onto_modeling 对齐。值只经子进程环境传递，不打印。
deploy_knowledge_hub() {
    log "部署知识包 Hub：${hub_dir}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可
    tar -xzf "${hub_package}" -C "$(dirname "${hub_dir}")"
    [ -f "${hub_dir}/deploy.sh" ] || die '知识包 Hub 发布包缺少 deploy.sh'
    network="$(docker container inspect onto-agent-backend \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null | head -n 1)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    onto_token="$(env_get "${onto_modeling_dir}/.env" ONTO_HUB_SERVICE_TOKEN)"
    (cd "${hub_dir}" && CONSUMER_NETWORK="${network}" HUB_SERVICE_TOKEN="${onto_token}" bash ./deploy.sh) || die \
        "知识包 Hub 部署失败；Consumer 已完成，修复后可单独执行 bash ${hub_dir}/deploy.sh"
    unset onto_token
    log "Knowledge Hub deployment finished: ${hub_dir}"
}

ensure_evaluation_service_token() {
    local env_file="$1" secret modeling_token
    secret="$(env_get "${env_file}" EVALUATION_SERVICE_TOKEN)"
    [ -n "${secret}" ] || secret="$(od -An -N 32 -tx1 /dev/urandom | tr -d '[:space:]')"
    [ "${#secret}" -ge 32 ] || die 'EVALUATION_SERVICE_TOKEN 至少需要 32 个字符'
    modeling_token="$(env_get "${env_file}" MODELING_SERVICE_TOKEN)"
    [ "${secret}" != "${modeling_token}" ] \
        || die 'EVALUATION_SERVICE_TOKEN 不能与 MODELING_SERVICE_TOKEN 相同'
    write_env_value "${env_file}" EVALUATION_SERVICE_TOKEN "${secret}"
    unset secret modeling_token
}

stage_evaluation_package() {
    local dir="$(dirname "${old_install_dir}")/eval-service" relative
    [ ! -L "${dir}" ] && [ ! -L "${dir}/deploy" ] || die '评测安装目录不能是符号链接'
    mkdir -p "${dir}/deploy" || die '无法创建评测安装目录'
    if [ -f "${evaluation_package}" ]; then
        for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
            [ ! -L "${dir}/${relative}" ] || die "评测目标文件不能是符号链接：${relative}"
        done
        tar -xzf "${evaluation_package}" -C "${dir}" --strip-components=1 eval-service/deploy/deploy.sh \
            eval-service/deploy/docker-compose.eval-service.yml eval-service/evaluation-image.tar eval-service/IMAGE \
            || die "无法解压评测发布包：${evaluation_package}"
        return 0
    fi
    [ "${evaluation_package}" != "$(cd "${dir}" && pwd)" ] || return 0
    # 仅覆盖交付物，保留 deploy/.env 中的数据加密密钥、端口和其他运行配置。
    for relative in deploy/deploy.sh deploy/docker-compose.eval-service.yml evaluation-image.tar IMAGE; do
        [ ! -L "${dir}/${relative}" ] || die "评测目标文件不能是符号链接：${relative}"
        cp "${evaluation_package}/${relative}" "${dir}/${relative}" || die "无法复制评测交付物：${relative}"
    done
}

precheck_evaluation() {
    local dir="$(dirname "${old_install_dir}")/eval-service" expected owner
    [ ! -L "${dir}" ] && [ ! -L "${dir}/deploy" ] || die '评测安装目录不能是符号链接'
    expected="$(cd "${dir}/deploy" 2>/dev/null && pwd || printf '%s' "${dir}/deploy")"
    if docker container inspect knowledge-evaluation >/dev/null 2>&1; then
        owner="$(docker container inspect knowledge-evaluation --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
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
    local dir="$(dirname "${old_install_dir}")/eval-service" network hub_token consumer_token modeling_token
    [ -f "${dir}/deploy/deploy.sh" ] || die "评测服务发布目录缺少 deploy/deploy.sh：${dir}"
    network="$(docker container inspect onto-agent-backend \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null | head -n 1)"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    hub_token="$(env_get "${hub_dir}/.env" HUB_EVALUATION_SERVICE_TOKEN)"
    consumer_token="$(env_get "${consumer_dir}/backend/.env" EVALUATION_SERVICE_TOKEN)"
    # Onto Modeling 的 deploy.sh 生成入站凭据；未部署时留空，评测只告警、不发完成通知
    modeling_token="$(env_get "${onto_modeling_dir}/.env" ONTO_EVALUATION_SERVICE_TOKEN)"
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

old_install_dir="$(prompt_value '请输入旧的合并部署根目录，例如 /opt/onto-agent：')"
consumer_package="$(prompt_value '请输入新的 Consumer 镜像 tar 包路径：')"
onto_package="$(prompt_value '请输入新的 Onto Modeling 发布包路径：')"
hub_package="$(prompt_value '请输入新的知识包 Hub 发布包路径：')"
evaluation_package="$(prompt_value '请输入新的独立评测服务发布包路径：')"

case "${old_install_dir}" in /*) ;; *) old_install_dir="$(pwd)/${old_install_dir}" ;; esac
case "${consumer_package}" in /*) ;; *) consumer_package="$(pwd)/${consumer_package}" ;; esac
case "${onto_package}" in ''|/*) ;; *) onto_package="$(pwd)/${onto_package}" ;; esac
case "${hub_package}" in ''|/*) ;; *) hub_package="$(pwd)/${hub_package}" ;; esac
case "${evaluation_package}" in ''|/*) ;; *) evaluation_package="$(pwd)/${evaluation_package}" ;; esac

consumer_package="$(resolve_package_path "${consumer_package}" 'Consumer 发布包')"
onto_package="$(resolve_package_path "${onto_package}" 'Onto Modeling 发布包')"
hub_package="$(resolve_package_path "${hub_package}" '知识包 Hub 发布包')"
onto_modeling_dir="$(dirname "${old_install_dir}")/onto-modeling"
hub_dir="$(dirname "${old_install_dir}")/knowledge-hub"

[ -d "${old_install_dir}" ] || die "旧合并部署根目录不存在：${old_install_dir}"
[ -f "${old_install_dir}/VERSION" ] || die "旧目录缺少 VERSION：${old_install_dir}"
[ -f "${old_install_dir}/backend/.env" ] || die "旧目录缺少 backend/.env"
[ -f "${old_install_dir}/deploy/state/current.env" ] || die "旧目录缺少 deploy/state/current.env"
for command_name in docker tar gzip sha256sum flock awk sed mktemp tee od tr; do check_command "${command_name}"; done
docker info >/dev/null 2>&1 || die "Docker daemon 不可用"
validate_package "${consumer_package}" onto-agent 'Consumer 发布包'
validate_package "${onto_package}" onto-modeling 'Onto Modeling 发布包'
check_command curl
validate_package "${hub_package}" knowledge-hub '知识包 Hub 发布包'
evaluation_package="$(resolve_package_path "${evaluation_package}" '独立评测服务发布包')"
validate_evaluation_package "${evaluation_package}" '独立评测服务发布包'

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/consumer-preflight.XXXXXX")" || die "无法创建预检暂存目录"
tar -xzf "${consumer_package}" -C "${TMP_ROOT}"
staged_root="${TMP_ROOT}/onto-agent"
opsctl="${staged_root}/deploy/opsctl"
manifest="${staged_root}/deploy/manifest.json"
[ -x "${opsctl}" ] || die "Consumer 发布包缺少 deploy/opsctl"
[ -f "${manifest}" ] || die "Consumer 发布包缺少 deploy/manifest.json"
precheck_onto_modeling
precheck_knowledge_hub
precheck_evaluation

# opsctl preflight reads the staged bundle's backend/.env for published ports.
# Reuse the old deployment's resolved configuration so custom ports are checked
# instead of falling back to the release defaults.
if [ -f "${staged_root}/backend/.env.example" ]; then
    cp "${old_install_dir}/backend/.env" "${staged_root}/backend/.env"
    chmod 600 "${staged_root}/backend/.env"
fi

if [ -n "${existing_port}" ]; then
    onto_port="${existing_port}"
    printf '沿用现有 Onto Modeling 宿主端口：%s\n' "${onto_port}" >/dev/tty
else
    onto_port="$(ask '请输入 Onto Modeling 宿主端口：')"
fi
case "${onto_port}" in
    ''|*[!0-9]*) printf 'Onto Modeling 宿主端口必须为 1 到 65535 的整数\n' >&2; exit 1 ;;
esac
[ "${onto_port}" -ge 1 ] && [ "${onto_port}" -le 65535 ] \
    || { printf 'Onto Modeling 宿主端口必须为 1 到 65535 的整数\n' >&2; exit 1; }

ONTO_WEB_PORT="${onto_port}" bash "${script_dir}/install-consumer-modeling.sh" \
    --upgrade-from "${legacy_dir}" \
    --consumer-package "${consumer_package}" \
    --auth-package "${auth_package}" \
    --onto-modeling-package "${onto_package}" \
    --knowledge-hub-package "${hub_package}" \
    --evaluation-package "${evaluation_package}"
