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
AUTH_PACKAGE=""
ONTO_MODELING_PACKAGE=""
KNOWLEDGE_HUB_PACKAGE=""
EVALUATION_PACKAGE=""
UPGRADE_FROM=""
COMMAND="install"
INSTALL_ROOT_EXPLICIT=0
[ -n "${INSTALL_ROOT+x}" ] && INSTALL_ROOT_EXPLICIT=1
INSTALL_ROOT="${INSTALL_ROOT:-/opt/onto-agent-stack}"
INSTALL_GLOBAL_OPSCTL="${INSTALL_GLOBAL_OPSCTL:-0}"
AUTH_OWNER_DIR_EXPLICIT=0
[ -n "${AUTH_OWNER_DIR+x}" ] && AUTH_OWNER_DIR_EXPLICIT=1
AUTH_OWNER_DIR="${AUTH_OWNER_DIR:-${INSTALL_ROOT}/independent-auth}"
AUTH_PROJECT="${AUTH_PROJECT:-independent-auth}"
AUTH_STAGE_DIR=""

cleanup_auth_stage() {
    [ -n "${AUTH_STAGE_DIR}" ] || return 0
    find "${AUTH_STAGE_DIR}" -mindepth 1 -delete 2>/dev/null || true
    rmdir "${AUTH_STAGE_DIR}" 2>/dev/null || true
}

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
  bash install-consumer-modeling.sh [upgrade] <五个发布包参数>
  bash install-consumer-modeling.sh --upgrade-from DIR <五个发布包参数>
  bash install-consumer-modeling.sh --help

三个流程：
  首次安装            不带 upgrade 或 --upgrade-from；安装根目录尚无 Consumer。
  upgrade             升级 Auth 已有独立 owner 的部署；不执行 Auth 所有权迁移。
  --upgrade-from DIR  Auth 仍由 Consumer 管理时，迁移 Auth 所有权并升级。
                      适用于旧整套合并部署，也适用于业务服务已分离但 Auth 未独立的部署。
                      DIR 指向当前 Consumer 安装目录（如 /path/to/old-stack/onto-agent），
                      不能与 upgrade 同用；DIR 必须是现有 Consumer 安装目录。
                      默认在 Consumer 安装目录旁创建独立 Auth owner。

五个发布包参数（均必填）：
  --consumer-package FILE       不含 Auth 的 external Consumer 发布包
  --auth-package FILE           独立 Auth 包 onto-auth-<版本>-<架构>.tar.gz
  --onto-modeling-package FILE  Onto Modeling 发布包
  --knowledge-hub-package FILE  知识包 Hub 发布包
  --evaluation-package PATH     独立评测发布包，或完整包内的 eval-service 目录

其他参数：
  --upgrade-from DIR  Auth 仍由 Consumer 管理时的 Consumer 安装目录
  -h, --help          显示本说明

发布包路径可为绝对或相对路径；通配符必须唯一匹配。请使用与发布包配套的
联合脚本。脚本先校验发布包，再按 Auth → Consumer → Hub → Onto Modeling →
评测的顺序安装或升级；使用 --upgrade-from 时先迁移 Auth 所有权，
再升级镜像和业务服务。无需为旧 Auth 生成新的首装凭据。


环境变量：
  INSTALL_ROOT           安装根目录；默认 /opt/onto-agent-stack

首次安装示例：
  INSTALL_ROOT=/opt/onto-agent-stack bash deploy/install-consumer-modeling.sh \
    --consumer-package /path/to/consumer-release.tar.gz \
    --auth-package /path/to/onto-auth-release.tar.gz \
    --onto-modeling-package /path/to/onto-modeling-release.tar.gz \
    --knowledge-hub-package /path/to/knowledge-hub-release.tar.gz \
    --evaluation-package /path/to/evaluation-release.tar.gz

升级 Auth 已独立的部署示例：
  INSTALL_ROOT=/opt/onto-agent-stack bash deploy/install-consumer-modeling.sh upgrade \
    --consumer-package /path/to/consumer-release.tar.gz \
    --auth-package /path/to/onto-auth-release.tar.gz \
    --onto-modeling-package /path/to/onto-modeling-release.tar.gz \
    --knowledge-hub-package /path/to/knowledge-hub-release.tar.gz \
    --evaluation-package /path/to/evaluation-release.tar.gz

迁移旧 Consumer 管理的 Auth 并升级示例（两种起点均使用此命令）：
  bash deploy/install-consumer-modeling.sh \
    --upgrade-from /path/to/old-stack/onto-agent \
    --consumer-package /path/to/consumer-release.tar.gz \
    --auth-package /path/to/onto-auth-release.tar.gz \
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
        --auth-package)
            [ "$#" -ge 2 ] || die "--auth-package 缺少文件路径"
            AUTH_PACKAGE="$2"
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
            [ "$#" -ge 2 ] || die "--upgrade-from 缺少 Consumer 安装目录"
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
[ -n "${AUTH_PACKAGE}" ] || die "必须指定 --auth-package"
[ -n "${ONTO_MODELING_PACKAGE}" ] || die "必须指定 --onto-modeling-package"
[ -n "${KNOWLEDGE_HUB_PACKAGE}" ] || die "必须指定 --knowledge-hub-package"
[ -n "${EVALUATION_PACKAGE}" ] || die "必须指定 --evaluation-package"

case "${CONSUMER_PACKAGE}" in
    ''|/*) ;;
    *) CONSUMER_PACKAGE="$(pwd)/${CONSUMER_PACKAGE}" ;;
esac
case "${AUTH_PACKAGE}" in
    ''|/*) ;;
    *) AUTH_PACKAGE="$(pwd)/${AUTH_PACKAGE}" ;;
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
if [ "${AUTH_OWNER_DIR_EXPLICIT}" -eq 0 ]; then
    AUTH_OWNER_DIR="${INSTALL_ROOT}/independent-auth"
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
[ -z "${AUTH_PACKAGE}" ] \
    || AUTH_PACKAGE="$(resolve_package_path "${AUTH_PACKAGE}" '独立 Auth 发布包')"
[ -z "${ONTO_MODELING_PACKAGE}" ] \
    || ONTO_MODELING_PACKAGE="$(resolve_package_path "${ONTO_MODELING_PACKAGE}" 'Onto Modeling发布包')"
[ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
    || KNOWLEDGE_HUB_PACKAGE="$(resolve_package_path "${KNOWLEDGE_HUB_PACKAGE}" '知识包 Hub发布包')"
[ -z "${EVALUATION_PACKAGE}" ] \
    || EVALUATION_PACKAGE="$(resolve_package_path "${EVALUATION_PACKAGE}" '独立评测服务发布包')"

if [ -n "${UPGRADE_FROM}" ]; then
    [ ! -L "${UPGRADE_FROM}" ] || die "--upgrade-from 不能指向符号链接：${UPGRADE_FROM}"
    [ -d "${UPGRADE_FROM}" ] || die "--upgrade-from 指向的 Consumer 安装目录不存在：${UPGRADE_FROM}"
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
command -v python3 >/dev/null 2>&1 || die '缺少命令：python3'
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

prepare_auth_package() {
    local bundle
    [ -f "${AUTH_PACKAGE}" ] && [ ! -L "${AUTH_PACKAGE}" ] \
        || die '独立 Auth 发布包不存在或是符号链接'
    bundle="$(python3 - "${AUTH_PACKAGE}" <<'PY'
import re
import sys
import tarfile

required = {'auth-release.json', 'images/auth-server.tar',
            'deploy/auth-install.py', 'deploy/auth-offline.py', 'deploy/auth-upgrade.sh',
            'deploy/auth_upgrade.py', 'deploy/auth_external.py'}
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    members = archive.getmembers()
    roots = {item.name.split('/', 1)[0] for item in members}
    if len(roots) != 1:
        raise SystemExit('Auth 发布包必须只有一个顶层目录')
    root = roots.pop()
    if not re.fullmatch(r'onto-auth-[A-Za-z0-9_.-]+-(amd64|arm64)', root):
        raise SystemExit('Auth 发布包顶层目录名称非法')
    files = set()
    for item in members:
        parts = item.name.rstrip('/').split('/')
        if (parts[0] != root or any(part in ('', '.', '..') for part in parts)
                or not (item.isfile() or item.isdir())):
            raise SystemExit('Auth 发布包包含非法路径或链接')
        if item.isfile():
            files.add('/'.join(parts[1:]))
    if not required <= files:
        raise SystemExit('Auth 发布包缺少镜像、清单或部署工具')
    print(root)
PY
)" || die '独立 Auth 发布包结构校验失败'
    AUTH_STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/independent-auth-release.XXXXXX")" \
        || die '无法创建 Auth 发布包暂存目录'
    trap cleanup_auth_stage EXIT
    tar -xzf "${AUTH_PACKAGE}" -C "${AUTH_STAGE_DIR}" || die '无法解压独立 Auth 发布包'
    AUTH_PACKAGE_DIR="${AUTH_STAGE_DIR}/${bundle}"
    python3 - "${AUTH_PACKAGE_DIR}" <<'PY' || die '独立 Auth 发布包哈希或元数据校验失败'
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
manifest = json.loads((root / 'auth-release.json').read_text())
files = manifest.get('files')
if (manifest.get('schema_version') != 1 or manifest.get('contract') != 'auth-v1'
        or not isinstance(files, dict) or not files
        or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest.get('image_id', ''))):
    raise SystemExit(1)
if not {'images/auth-server.tar', 'deploy/auth-install.py', 'deploy/auth-upgrade.sh'} <= files.keys():
    raise SystemExit(1)
actual_files = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}
if actual_files != set(files) | {'auth-release.json'}:
    raise SystemExit(1)
for name, expected in files.items():
    if not isinstance(name, str) or not isinstance(expected, str):
        raise SystemExit(1)
    path = root / name
    if (path.is_symlink() or not path.is_file()
            or root not in path.resolve().parents):
        raise SystemExit(1)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise SystemExit(1)
PY
}

install_independent_auth() {
    [ -f "${CONSUMER_DIR}/deploy/state/auth-owner.json" ] && return 0
    check_auth_owner_env
    log '从独立 Auth 发布包创建 Auth owner'
    python3 "${AUTH_PACKAGE_DIR}/deploy/auth-install.py" \
        --package "${AUTH_PACKAGE_DIR}" --env-file "${AUTH_OWNER_DIR}/.env" \
        --consumer-dir "${CONSUMER_DIR}" --owner-dir "${AUTH_OWNER_DIR}" \
        --project "${AUTH_PROJECT}" || die '独立 Auth 首装失败；请检查状态后按 Auth 工具说明恢复'
}

prepare_auth_owner_env() {
    [ ! -L "${AUTH_OWNER_DIR}" ] && [ ! -L "${AUTH_OWNER_DIR}/.env" ] \
        || die '独立 Auth owner 目录或 .env 不能是符号链接'
    (umask 077; mkdir -p "${AUTH_OWNER_DIR}") \
        || die '无法创建独立 Auth owner 目录'
    [ -d "${AUTH_OWNER_DIR}" ] || die '独立 Auth owner 目录无效'
    if [ -e "${AUTH_OWNER_DIR}/.env" ]; then
        check_auth_owner_env
    fi
    python3 - "${AUTH_OWNER_DIR}/.env" <<'PY' \
        || die '无法准备独立 Auth .env；请检查模板、文件权限和配置格式'
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import tempfile
import sys

path = Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit('Auth owner dotenv is a link')
existing = path.is_file()
if existing and stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit('Auth owner dotenv must have mode 0600')
lines = path.read_text(encoding='utf-8').splitlines() if existing else [
    'AUTH_SERVER_HOST=0.0.0.0',
    'AUTH_SERVER_PORT=5056',
    'AUTH_SERVER_DATA_DIR=/data',
    'AUTH_ACCESS_TTL_SECONDS=900',
    'AUTH_REFRESH_TTL_SECONDS=259200',
    'AUTH_USER_MAX_COUNT=0',
    'AUTH_ENABLE_REGISTRATION=true',
]
required = ('AUTH_SERVER_ISSUER', 'AUTH_SERVER_AUDIENCE',
            'AUTH_SERVER_ADMIN_TOKEN', 'AUTH_BOOTSTRAP_ADMIN_PASSWORD')
pattern = re.compile(r'^\s*(?:export\s+)?(' + '|'.join(required) + r')\s*=(.*)$')
seen = set()
changes = False

def generated(key):
    return {'AUTH_SERVER_ISSUER': 'http://auth-server:5056',
            'AUTH_SERVER_AUDIENCE': 'knowledge-scaffold'}.get(key) or (
            secrets.token_hex(32) if key == 'AUTH_SERVER_ADMIN_TOKEN'
            else 'Aa1!' + secrets.token_urlsafe(32))

for index, line in enumerate(lines):
    match = pattern.match(line)
    if not match:
        continue
    key, raw = match.groups()
    if key in seen:
        raise SystemExit('Duplicate required Auth key: ' + key)
    seen.add(key)
    value = raw.strip()
    if value.startswith(("'", '"')):
        parts = shlex.split(value, comments=True)
        if len(parts) != 1:
            raise SystemExit('Invalid required Auth value: ' + key)
        value = parts[0]
    else:
        value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
    if not value:
        lines[index] = key + '=' + generated(key)
        changes = True
for key in required:
    if key not in seen:
        lines.append(key + '=' + generated(key))
        changes = True
if changes or not existing:
    fd, temporary = tempfile.mkstemp(prefix='.auth-env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write('\n'.join(lines) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
PY
    check_auth_owner_env
}

check_auth_owner_env() {
    [ -d "${AUTH_OWNER_DIR}" ] && [ ! -L "${AUTH_OWNER_DIR}" ] \
        || die "请先准备独立 Auth owner 目录：${AUTH_OWNER_DIR}"
    [ -f "${AUTH_OWNER_DIR}/.env" ] && [ ! -L "${AUTH_OWNER_DIR}/.env" ] \
        || die "请先在独立 Auth owner 目录准备 .env：${AUTH_OWNER_DIR}/.env"
    [ -r "${AUTH_OWNER_DIR}/.env" ] \
        || die "独立 Auth .env 不可读：${AUTH_OWNER_DIR}/.env"
    local mode
    mode="$(stat -c '%a' "${AUTH_OWNER_DIR}/.env")" \
        || die '无法读取独立 Auth .env 文件权限'
    [ "${mode}" = 600 ] \
        || die "独立 Auth .env 文件权限必须为 0600：${AUTH_OWNER_DIR}/.env"
}

migrate_legacy_auth_ownership() {
    local release_dir="$1" state="${CONSUMER_DIR}/deploy/state/auth-owner.json" tool target backup_dir owner project resume=()
    [ -n "${UPGRADE_FROM}" ] || die 'Consumer 缺少独立 Auth owner 记录；旧合并部署请使用 --upgrade-from'
    tool="${AUTH_PACKAGE_DIR}/deploy/auth-offline.py"
    [ -f "${tool}" ] || die '独立 Auth 发布包缺少所有权迁移工具'
    for target in common.sh auth_external.sh auth_external.py auth_upgrade.py auth_hooks.py auth_contract.py; do
        [ -f "${release_dir}/deploy/${target}" ] && [ ! -L "${release_dir}/deploy/${target}" ] \
            || die "新版 Consumer 发布包缺少 Auth 迁移前置工具：${target}"
        [ ! -L "${CONSUMER_DIR}/deploy/${target}" ] \
            || die "旧 Consumer 部署工具不能是符号链接：${target}"
    done
    if [ -f "${state}" ]; then
        [ ! -L "${state}" ] || die 'Auth 所有权迁移记录不能是符号链接'
        owner="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ownerDir"])' "${state}")" \
            || die '无法读取中断的 Auth owner 目录'
        project="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["targetProject"])' "${state}")" \
            || die '无法读取中断的 Auth Compose 项目名'
        [ -n "${owner}" ] && [ -n "${project}" ] || die 'Auth 所有权迁移记录不完整'
        resume=(--resume)
    else
        owner="${AUTH_OWNER_DIR}"
        project="${AUTH_PROJECT}"
    fi
    [ "${owner}" != "${CONSUMER_DIR}" ] || die '独立 Auth owner 不能等于 Consumer 安装目录'
    # 迁移器要求 Consumer 目录先具备新版 external 生命周期工具；只替换部署工具，保留运行配置和数据。
    backup_dir="${CONSUMER_DIR}/deploy/backups/auth-migration-tools"
    [ ! -L "${backup_dir}" ] || die 'Auth 迁移工具备份目录不能是符号链接'
    mkdir -p "${backup_dir}"
    chmod 700 "${backup_dir}"
    for target in common.sh auth_external.sh auth_external.py auth_upgrade.py auth_hooks.py auth_contract.py; do
        [ ! -L "${backup_dir}/${target}" ] || die "Auth 迁移工具备份不能是符号链接：${target}"
    done
    for target in common.sh auth_external.sh auth_external.py auth_upgrade.py auth_hooks.py auth_contract.py; do
        if [ -f "${CONSUMER_DIR}/deploy/${target}" ] && [ ! -f "${backup_dir}/${target}" ]; then
            cp -p "${CONSUMER_DIR}/deploy/${target}" "${backup_dir}/${target}" \
                || die "无法备份旧 Consumer 部署工具：${target}"
        fi
        cp "${release_dir}/deploy/${target}" "${CONSUMER_DIR}/deploy/${target}" \
            || die "无法准备新版 Consumer 部署工具：${target}"
    done
    log '迁移旧 Auth 所有权到独立 owner，保留账号卷、容器名和网络别名'
    python3 "${tool}" --install-dir "${CONSUMER_DIR}" --owner-dir "${owner}" \
        --project "${project}" "${resume[@]}" \
        || die '旧 Auth 所有权迁移失败；保留迁移状态和旧部署工具备份，请检查后重跑 --upgrade-from'
}

upgrade_independent_auth() {
    local state="${CONSUMER_DIR}/deploy/state/auth-owner.json" owner container current_image target_image
    owner="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ownerDir"])' "${state}")"
    container="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["container"])' "${state}")"
    target_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_id"])' "${AUTH_PACKAGE_DIR}/auth-release.json")"
    current_image="$(docker container inspect "${container}" --format '{{.Image}}' 2>/dev/null)" \
        || die '无法读取独立 Auth 当前镜像'
    if [ "${current_image}" = "${target_image}" ]; then
        log '独立 Auth 当前镜像与发布包一致，跳过重复升级'
        verify_independent_auth
        return 0
    fi
    log '使用独立 Auth 发布包升级 Auth owner'
    bash "${AUTH_PACKAGE_DIR}/deploy/auth-upgrade.sh" \
        --install-dir "${owner}" --package "${AUTH_PACKAGE_DIR}" \
        --container "${container}" || die '独立 Auth 升级失败；Consumer 和建模服务尚未升级'
    verify_independent_auth
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
    # 旧 Core 容器若仍在运行，其宿主端口须避开 Onto Modeling。
    [ "${ONTO_WEB_PORT}" != "${current_modeling_port}" ] || die \
        "Onto Modeling 与运行中的旧 Core 容器都要占用宿主端口 ${ONTO_WEB_PORT}；请用 ONTO_WEB_PORT 错开"
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
        2>/dev/null || true
}

verify_independent_auth() {
    local state="${CONSUMER_DIR}/deploy/state/auth-owner.json" owner container network candidates owner_networks aliases auth_host running elapsed admin_token
    [ -f "${state}" ] || die 'Consumer 缺少独立 Auth owner 记录；请先完成 Auth 首装或原地迁移'
    owner="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ownerDir"])' "${state}")"
    container="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["container"])' "${state}")"
    [ -f "${owner}/deploy/state/ownership.json" ] \
        || die '独立 Auth owner 状态缺失'
    [ -f "${CONSUMER_DIR}/deploy/auth_external.py" ] \
        || die 'Consumer 安装目录缺少独立 Auth 校验工具'
    # 与独立 owner 的升级工具使用同一把锁；先确认归属和数据卷，再启动已存在的容器。
    exec 8>"${owner}/.consumer-deploy.lock"
    flock -n 8 || die '独立 Auth 正在维护中，请稍后重试'
    python3 "${CONSUMER_DIR}/deploy/auth_external.py" verify --root "${CONSUMER_DIR}" \
        || die '独立 Auth owner 或数据卷身份校验失败'
    [ "$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_DEPLOYMENT_MODE)" = external ] \
        || die 'Consumer 未配置 external Auth 模式'
    admin_token="$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_SERVER_ADMIN_TOKEN)"
    [ -n "${admin_token}" ] || die 'Consumer 缺少独立 Auth 管理令牌'
    running="$(docker container inspect "${container}" --format '{{.State.Running}}' 2>/dev/null)" \
        || die '独立 Auth 容器不存在；请用 owner 工具恢复'
    case "${running}" in
        true) ;;
        false)
            log '启动已停止的独立 Auth 容器'
            docker start "${container}" >/dev/null || die '独立 Auth 容器启动失败'
            ;;
        *) die '无法确认独立 Auth 容器运行状态' ;;
    esac
    elapsed=0
    until docker exec "${container}" python3 -c \
        'import json,urllib.request; base="http://127.0.0.1:5056"; assert urllib.request.urlopen(base+"/health",timeout=5).status==200; assert json.load(urllib.request.urlopen(base+"/jwks.json",timeout=5))["keys"]' \
        >/dev/null 2>&1; do
        [ "${elapsed}" -lt 60 ] || die '独立 Auth 60 秒内未就绪，请检查 owner 容器日志'
        sleep 2
        elapsed=$((elapsed + 2))
    done
    owner_networks="$(docker container inspect "${container}" \
        --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
        2>/dev/null)" || die '无法读取独立 Auth 容器网络'
    candidates="$(consumer_network)"
    if [ -z "${candidates}" ]; then
        candidates="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print("\n".join(s["consumerNetworks"].values()))' "${state}")"
    fi
    network=""
    while IFS= read -r candidate; do
        [ -n "${candidate}" ] || continue
        if printf '%s\n' "${owner_networks}" | grep -Fx "${candidate}" >/dev/null; then
            network="${candidate}"
            break
        fi
    done <<< "${candidates}"
    [ -n "${network}" ] || die '独立 Auth 容器未加入 Consumer 网络'
    AUTH_SHARED_URL="$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_SERVER_URL)"
    auth_host="$(python3 -c '
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
if url.scheme != "http" or url.port != 5056 or not url.hostname or url.path not in ("", "/") or url.query or url.fragment or url.username or url.password:
    raise SystemExit(1)
print(url.hostname)
' "${AUTH_SHARED_URL}")" || die 'Consumer 的 Auth URL 格式或端口不受支持'
    aliases="$(docker container inspect "${container}" --format \
        "{{range \$name, \$settings := .NetworkSettings.Networks}}{{if eq \$name \"${network}\"}}{{range \$settings.Aliases}}{{println .}}{{end}}{{end}}{{end}}" \
        2>/dev/null)" || die '无法读取独立 Auth 网络别名'
    printf '%s\n' "${aliases}" | grep -Fx "${auth_host}" >/dev/null \
        || die 'Consumer 的 Auth URL 主机名未由独立 Auth 容器提供'
    AUTH_SHARED_NETWORK="${network}"
    AUTH_SHARED_ISSUER="$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_SERVER_ISSUER)"
    AUTH_SHARED_AUDIENCE="$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_SERVER_AUDIENCE)"
    identity="$(docker exec "${container}" python3 -c \
        'from auth_server.config import AuthServerConfig; c=AuthServerConfig.from_env(); print(c.issuer); print(c.audience)')" \
        || die '独立 Auth 签发身份无法读取'
    # 旧 Consumer 配置可能只在 Auth 容器环境中保存签发身份；以 owner 容器实值补齐。
    [ -n "${AUTH_SHARED_ISSUER}" ] || AUTH_SHARED_ISSUER="$(printf '%s\n' "${identity}" | sed -n '1p')"
    [ -n "${AUTH_SHARED_AUDIENCE}" ] || AUTH_SHARED_AUDIENCE="$(printf '%s\n' "${identity}" | sed -n '2p')"
    [ "$(printf '%s\n' "${identity}" | sed -n '1p')" = "${AUTH_SHARED_ISSUER}" ] \
        && [ "$(printf '%s\n' "${identity}" | sed -n '2p')" = "${AUTH_SHARED_AUDIENCE}" ] \
        || die 'Consumer Auth 身份与独立 owner 不一致'
    printf '%s' "${admin_token}" | docker exec -i "${container}" python3 -c \
        'import hmac, sys; from auth_server.config import AuthServerConfig; expected = AuthServerConfig.from_env().admin_token or ""; sys.exit(0 if expected and hmac.compare_digest(sys.stdin.read(), expected) else 1)' \
        >/dev/null 2>&1 || die 'Consumer Auth 管理令牌与独立 owner 不一致'
    unset admin_token
    flock -u 8
    exec 8>&-
}

verify_consumer_auth_connection() {
    python3 "${CONSUMER_DIR}/deploy/auth_external.py" consumer-health --root "${CONSUMER_DIR}" \
        || die 'Consumer 无法连接独立 Auth；请检查 Consumer 容器和共享网络'
}

precheck_auth_references() {
    local file key actual expected admin_token
    admin_token="$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_SERVER_ADMIN_TOKEN)"
    for file in "${KNOWLEDGE_HUB_DIR}/.env" "${ONTO_MODELING_DIR}/.env" \
                "${EVAL_ENV_FILE:-${EVALUATION_DIR}/deploy/.env}"; do
        [ -f "${file}" ] || continue
        case "${file}" in
            "${KNOWLEDGE_HUB_DIR}/.env")
                for key in HUB_AUTH_URL HUB_AUTH_ISSUER; do
                    expected="${AUTH_SHARED_URL}"
                    [ "${key}" = HUB_AUTH_ISSUER ] && expected="${AUTH_SHARED_ISSUER}"
                    actual="$(env_get "${file}" "${key}")"
                    [ -z "${actual}" ] || [ "${actual}" = "${expected}" ] \
                        || die "Hub ${key} 与独立 Auth 不一致"
                done ;;
            "${ONTO_MODELING_DIR}/.env")
                for key in ONTO_MODELING_AUTH_URL ONTO_WEB_AUTH_ISSUER ONTO_WEB_AUTH_AUDIENCE ONTO_WEB_AUTH_ADMIN_TOKEN; do
                    case "${key}" in
                        ONTO_MODELING_AUTH_URL) expected="${AUTH_SHARED_URL}" ;;
                        ONTO_WEB_AUTH_ISSUER) expected="${AUTH_SHARED_ISSUER}" ;;
                        ONTO_WEB_AUTH_AUDIENCE) expected="${AUTH_SHARED_AUDIENCE}" ;;
                        *) expected="${admin_token}" ;;
                    esac
                    actual="$(env_get "${file}" "${key}")"
                    [ -z "${actual}" ] || [ "${actual}" = "${expected}" ] \
                        || die "Onto ${key} 与独立 Auth 不一致"
                done ;;
            *)
                for key in EVAL_AUTH_URL EVAL_AUTH_ISSUER; do
                    expected="${AUTH_SHARED_URL}"
                    [ "${key}" = EVAL_AUTH_ISSUER ] && expected="${AUTH_SHARED_ISSUER}"
                    actual="$(env_get "${file}" "${key}")"
                    [ -z "${actual}" ] || [ "${actual}" = "${expected}" ] \
                        || die "Evaluation ${key} 与独立 Auth 不一致"
                done ;;
        esac
    done
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
    verify_independent_auth
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
    network="${AUTH_SHARED_NETWORK}"
    [ -n "${network}" ] || die '无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行'
    hub_token="$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_EVALUATION_SERVICE_TOKEN)"
    consumer_token="$(env_get "${CONSUMER_DIR}/backend/.env" EVALUATION_SERVICE_TOKEN)"
    # Onto Modeling 的 deploy.sh 生成入站凭据；未部署时留空，评测只告警、不发完成通知
    modeling_token="$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_EVALUATION_SERVICE_TOKEN)"
    flywheel_token="$(env_get "${CONSUMER_DIR}/backend/.env" FLYWHEEL_SERVICE_TOKEN)"
    [ -n "${hub_token}" ] || [ -n "${EVAL_ENV_FILE:-}" ] || die 'Hub 缺少评测凭据，请先更新知识包 Hub'
    existing_auth_url="$(env_get "${EVAL_ENV_FILE:-${EVALUATION_DIR}/deploy/.env}" EVAL_AUTH_URL)"
    existing_auth_issuer="$(env_get "${EVAL_ENV_FILE:-${EVALUATION_DIR}/deploy/.env}" EVAL_AUTH_ISSUER)"
    [ -z "${existing_auth_url}" ] || [ "${existing_auth_url}" = "${AUTH_SHARED_URL}" ] \
        || die '评测 Auth URL 与独立 Auth 不一致'
    [ -z "${existing_auth_issuer}" ] || [ "${existing_auth_issuer}" = "${AUTH_SHARED_ISSUER}" ] \
        || die '评测 Auth issuer 与独立 Auth 不一致'
    log "部署独立评测服务：${EVALUATION_DIR}（网络 ${network}）"
    (cd "${EVALUATION_DIR}" \
        && CONSUMER_NETWORK="${network}" EVAL_AUTH_URL="${AUTH_SHARED_URL}" EVAL_AUTH_ISSUER="${AUTH_SHARED_ISSUER}" \
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
    verify_independent_auth
    existing_auth_url="$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_AUTH_URL)"
    existing_auth_issuer="$(env_get "${KNOWLEDGE_HUB_DIR}/.env" HUB_AUTH_ISSUER)"
    [ -z "${existing_auth_url}" ] || [ "${existing_auth_url}" = "${AUTH_SHARED_URL}" ] \
        || die 'Hub Auth URL 与独立 Auth 不一致'
    [ -z "${existing_auth_issuer}" ] || [ "${existing_auth_issuer}" = "${AUTH_SHARED_ISSUER}" ] \
        || die 'Hub Auth issuer 与独立 Auth 不一致'
    log "部署知识包 Hub：${KNOWLEDGE_HUB_DIR}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可升级
    tar -xzf "${KNOWLEDGE_HUB_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${KNOWLEDGE_HUB_DIR}/deploy.sh" ] || die "知识包 Hub 发布包缺少 deploy.sh"
    network="${AUTH_SHARED_NETWORK}"
    [ -n "${network}" ] || die "无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行"
    onto_token="$(env_get "${ONTO_MODELING_DIR}/.env" ONTO_HUB_SERVICE_TOKEN)"
    if ! (cd "${KNOWLEDGE_HUB_DIR}" \
            && CONSUMER_NETWORK="${network}" HUB_SERVICE_TOKEN="${onto_token}" \
               HUB_AUTH_URL="${AUTH_SHARED_URL}" HUB_AUTH_ISSUER="${AUTH_SHARED_ISSUER}" bash ./deploy.sh); then
        die "知识包 Hub 部署失败；Consumer 已完成，修复后可单独执行 bash ${KNOWLEDGE_HUB_DIR}/deploy.sh"
    fi
    unset onto_token
    log "Knowledge Hub deployment finished: ${KNOWLEDGE_HUB_DIR}"
}

deploy_onto_modeling() {
    local env_file="${ONTO_MODELING_DIR}/.env" admin_token existing_admin_token
    log "部署 Onto Modeling：${ONTO_MODELING_DIR}"
    # 数据在具名卷里、.env 不在发布包内，覆盖解压即可升级
    tar -xzf "${ONTO_MODELING_PACKAGE}" -C "${INSTALL_ROOT}"
    [ -f "${ONTO_MODELING_DIR}/deploy.sh" ] || die "Onto Modeling 发布包缺少 deploy.sh"
    verify_independent_auth
    network="${AUTH_SHARED_NETWORK}"
    [ -n "${network}" ] || die "无法探测 Consumer 容器网络，请确认 onto-agent-backend 已在运行"
    if [ ! -f "${env_file}" ]; then
        [ -f "${ONTO_MODELING_DIR}/.env.example" ] || die "Onto Modeling 发布包缺少 .env.example"
        cp "${ONTO_MODELING_DIR}/.env.example" "${env_file}"
    fi
    chmod 600 "${env_file}"
    write_env_value "${env_file}" ONTO_WEB_PORT "${ONTO_WEB_PORT}"
    write_env_value "${env_file}" ONTO_MODELING_EXTERNAL_AUTH_NETWORK "${network}"
    for key in ONTO_MODELING_AUTH_URL ONTO_WEB_AUTH_ISSUER ONTO_WEB_AUTH_AUDIENCE; do
        case "${key}" in
            ONTO_MODELING_AUTH_URL) expected="${AUTH_SHARED_URL}" ;;
            ONTO_WEB_AUTH_ISSUER) expected="${AUTH_SHARED_ISSUER}" ;;
            ONTO_WEB_AUTH_AUDIENCE) expected="${AUTH_SHARED_AUDIENCE}" ;;
        esac
        existing="$(env_get "${env_file}" "${key}")"
        [ -z "${existing}" ] || [ "${existing}" = "${expected}" ] \
            || die "Onto ${key} 与独立 Auth 身份不一致，拒绝覆盖"
        write_env_value "${env_file}" "${key}" "${expected}"
    done
    admin_token="$(env_get "${CONSUMER_DIR}/backend/.env" AUTH_SERVER_ADMIN_TOKEN)"
    [ -n "${admin_token}" ] || die 'Consumer 缺少独立 Auth 管理令牌'
    existing_admin_token="$(env_get "${env_file}" ONTO_WEB_AUTH_ADMIN_TOKEN)"
    [ -z "${existing_admin_token}" ] || [ "${existing_admin_token}" = "${admin_token}" ] \
        || die 'Onto Auth 管理令牌与独立 Auth 不一致'
    write_env_value "${env_file}" ONTO_WEB_AUTH_ADMIN_TOKEN "${admin_token}"
    unset admin_token existing_admin_token
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
    trap 'cleanup_core_stage; cleanup_auth_stage' EXIT
    tar -xzf "${CONSUMER_PACKAGE}" -C "${stage_dir}"
    if [ ! -f "${CONSUMER_DIR}/deploy/state/auth-owner.json" ] || \
            [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("step", ""))' "${CONSUMER_DIR}/deploy/state/auth-owner.json")" != completed ]; then
        migrate_legacy_auth_ownership "${stage_dir}/onto-agent"
    fi
    # A completed owner migration can be resumed after an interruption.
    # Restore every runtime import before verifying the Consumer client.
    if [ -n "${UPGRADE_FROM}" ]; then
        for target in auth_hooks.py auth_contract.py deploy_support.py; do
            [ -f "${CONSUMER_DIR}/deploy/${target}" ] && continue
            [ -f "${stage_dir}/onto-agent/deploy/${target}" ] \
                && [ ! -L "${stage_dir}/onto-agent/deploy/${target}" ] \
                && [ ! -L "${CONSUMER_DIR}/deploy/${target}" ] \
                || die "新版 Consumer 发布包缺少 Auth 续跑依赖：${target}"
            cp "${stage_dir}/onto-agent/deploy/${target}" "${CONSUMER_DIR}/deploy/${target}" \
                || die "无法准备 Auth 续跑依赖：${target}"
        done
    fi
    verify_independent_auth
    precheck_auth_references
    upgrade_independent_auth
    ensure_evaluation_service_token
    if ! run_consumer_upgrade "${stage_dir}/onto-agent" "${CONSUMER_DIR}"; then
        die "Consumer 升级失败"
    fi
    verify_consumer_auth_connection
    cleanup_core_stage
    cleanup_auth_stage
    trap - EXIT
    deploy_knowledge_hub
    deploy_onto_modeling
    deploy_evaluation
    log "Consumer、Onto Modeling、Hub 和评测升级完成"
}

upgrade_install() {
    legacy_stack_dir="$(cd "${UPGRADE_FROM}" 2>/dev/null && pwd)" \
        || die "旧合并部署根目录不存在：${UPGRADE_FROM}"
    UPGRADE_FROM="${legacy_stack_dir}"
    CONSUMER_DIR="${UPGRADE_FROM}"
    if [ "${AUTH_OWNER_DIR_EXPLICIT}" -eq 0 ]; then
        AUTH_OWNER_DIR="$(dirname "${CONSUMER_DIR}")/independent-auth"
    fi
    upgrade_core_services
}

validate_package "${CONSUMER_PACKAGE}" onto-agent
validate_package "${ONTO_MODELING_PACKAGE}" onto-modeling
validate_package "${KNOWLEDGE_HUB_PACKAGE}" knowledge-hub
validate_evaluation_package
prepare_auth_package
consumer_mode="$(tar -xzOf "${CONSUMER_PACKAGE}" onto-agent/deploy/manifest.json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("auth_deployment_mode", ""))')" \
    || die '无法读取 Consumer 发布包的 Auth 部署模式'
[ "${consumer_mode}" = external ] || die '联合安装仅接受不含 Auth 的 external Consumer 包'
tar -tzf "${CONSUMER_PACKAGE}" | python3 -c '
import sys
paths = sys.stdin.read().splitlines()
assert not any(path.startswith("onto-agent/images/") and "auth-server" in path for path in paths)
' || die 'Consumer 发布包仍含 Auth 镜像'
tar -xzOf "${CONSUMER_PACKAGE}" onto-agent/deploy/docker-compose.release.yml | python3 -c '
import json,sys
config=json.load(sys.stdin)
assert "auth-server" not in config.get("services", {})
assert "consumer-auth-data" not in config.get("volumes", {})
' || die 'Consumer 发布包仍含 Auth Compose 服务或账号卷'

if [ "${COMMAND}" = "upgrade" ]; then
    upgrade_core_services
    exit 0
fi

if [ -n "${UPGRADE_FROM}" ]; then
    CONSUMER_DIR="${UPGRADE_FROM}"
    upgrade_install
    exit 0
fi

[ ! -f "${CONSUMER_DIR}/deploy/state/current.env" ] \
    || die 'Consumer 已安装；请使用 upgrade'
if [ ! -f "${CONSUMER_DIR}/deploy/state/auth-owner.json" ]; then
    prepare_auth_owner_env
fi
precheck_onto_modeling ""
precheck_knowledge_hub
precheck_evaluation
mkdir -p "${INSTALL_ROOT}"
log "解压 Consumer 发布包"
tar -xzf "${CONSUMER_PACKAGE}" -C "${INSTALL_ROOT}"
if [ -f "${CONSUMER_DIR}/deploy/state/auth-owner.json" ]; then
    verify_independent_auth
    precheck_auth_references
    upgrade_independent_auth
else
    install_independent_auth
fi
verify_independent_auth

[ -f "${CONSUMER_DIR}/backend/.env.example" ] \
    || die "Consumer 发布包缺少 backend/.env.example"


[ -f "${CONSUMER_DIR}/backend/.env" ] \
    || cp "${CONSUMER_DIR}/backend/.env.example" "${CONSUMER_DIR}/backend/.env"
chmod 600 "${CONSUMER_DIR}/backend/.env"

MODELING_SERVICE_TOKEN_VALUE="$(env_get "${CONSUMER_DIR}/backend/.env" MODELING_SERVICE_TOKEN)"
[ -n "${MODELING_SERVICE_TOKEN_VALUE}" ] || MODELING_SERVICE_TOKEN_VALUE="$(generate_hex_secret 32)"
[ "${#MODELING_SERVICE_TOKEN_VALUE}" -ge 32 ] || die 'MODELING_SERVICE_TOKEN 至少需要 32 个字符'
trap 'unset MODELING_SERVICE_TOKEN_VALUE; cleanup_auth_stage' EXIT

write_env_value \
    "${CONSUMER_DIR}/backend/.env" \
    MODELING_SERVICE_TOKEN \
    "${MODELING_SERVICE_TOKEN_VALUE}"
log "共享 Modeling Token 已写入配置，已有值保持不变"
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
verify_consumer_auth_connection

deploy_knowledge_hub
deploy_onto_modeling
deploy_evaluation

log "Consumer、Onto Modeling、Hub 和评测安装完成"
printf '[full-install] Consumer：%s\n' "${CONSUMER_DIR}"
[ -z "${KNOWLEDGE_HUB_PACKAGE}" ] \
    || printf '[full-install] 知识包 Hub：%s\n' "${KNOWLEDGE_HUB_DIR}"
[ -z "${ONTO_MODELING_PACKAGE}" ] \
    || printf '[full-install] Onto Modeling：%s\n' "${ONTO_MODELING_DIR}"
