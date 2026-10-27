#!/bin/bash
# 部署知识包 Hub（独立 compose 项目，见 docker-compose.knowledge-hub.yml），与 onto-modeling 解耦。
# 源码 knowledge-hub/deploy/ 下运行时从 knowledge-hub/ 构建镜像；离线包内（含 images.env、images/）运行时加载包内镜像。
# 运行配置读写本目录 .env（权限 600，不入库）：HUB_SERVICE_TOKEN、HUB_EVALUATION_SERVICE_TOKEN 缺失时生成。
# 进程环境中的 HUB_SERVICE_TOKEN、CONSUMER_NETWORK 优先，整栈 deploy.sh 与安装脚本据此与 onto-modeling 对齐；
# 评测服务部署时沿用这里的 HUB_EVALUATION_SERVICE_TOKEN。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
ENV_FILE=.env
COMPOSE_FILE=docker-compose.knowledge-hub.yml
CONTAINER=knowledge-hub
# 源码部署的运行配置曾放在仓库 deploy/.env；首次在新位置部署时迁移，避免丢失手工配置
LEGACY_SOURCE_ENV_FILE=../../deploy/.env

log() { printf '[knowledge-hub] %s\n' "$*"; }
die() { printf '[knowledge-hub][error] %s\n' "$*" >&2; exit 1; }

env_get() { # <文件> <键>：按 compose env_file 语义清洗 CRLF、引号和行内注释
    local value
    value="$([ -f "$1" ] && grep -E "^$2=" "$1" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

env_set() { # <文件> <键> <值>：替换或追加，值不输出
    local temp_file
    temp_file="$(mktemp "$1.tmp.XXXXXX")" || die "无法更新 $1"
    chmod 600 "${temp_file}"
    awk -v key="$2" -v value="$3" '
        BEGIN { updated = 0 }
        index($0, key "=") == 1 { if (!updated) { print key "=" value; updated = 1 } next }
        { print }
        END { if (!updated) print key "=" value }
    ' "$1" > "${temp_file}" || { rm -f "${temp_file}"; die "无法更新 $1"; }
    mv "${temp_file}" "$1"
    chmod 600 "$1"
}

random_token() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }

[ -f "${COMPOSE_FILE}" ] || die "缺少 ${SCRIPT_DIR}/${COMPOSE_FILE}"
command -v docker >/dev/null 2>&1 || die "缺少 docker"
COMPOSE=(docker compose)
docker compose version >/dev/null 2>&1 || COMPOSE=(docker-compose)

SOURCE_MODE=0
[ -f images.env ] || SOURCE_MODE=1

if [ ! -f "${ENV_FILE}" ] && [ "${SOURCE_MODE}" = "1" ] \
        && grep -q '^HUB_SERVICE_TOKEN=' "${LEGACY_SOURCE_ENV_FILE}" 2>/dev/null; then
    (umask 077 && cp "${LEGACY_SOURCE_ENV_FILE}" "${ENV_FILE}")
    log "已从旧位置 deploy/.env 迁移 Hub 运行配置；确认无误后可删除旧文件"
fi
[ -f "${ENV_FILE}" ] || (umask 077 && : > "${ENV_FILE}")
chmod 600 "${ENV_FILE}"

HUB_SERVICE_TOKEN="${HUB_SERVICE_TOKEN:-$(env_get "${ENV_FILE}" HUB_SERVICE_TOKEN)}"
[ -n "${HUB_SERVICE_TOKEN}" ] || HUB_SERVICE_TOKEN="$(random_token)"
[ "${#HUB_SERVICE_TOKEN}" -ge 32 ] || die "HUB_SERVICE_TOKEN 至少需要 32 个字符"
env_set "${ENV_FILE}" HUB_SERVICE_TOKEN "${HUB_SERVICE_TOKEN}"
export HUB_SERVICE_TOKEN

# 评测服务身份：Hub 要求各服务凭据互不相同
HUB_EVALUATION_SERVICE_TOKEN="${HUB_EVALUATION_SERVICE_TOKEN:-$(env_get "${ENV_FILE}" HUB_EVALUATION_SERVICE_TOKEN)}"
[ -n "${HUB_EVALUATION_SERVICE_TOKEN}" ] || HUB_EVALUATION_SERVICE_TOKEN="$(random_token)"
[ "${#HUB_EVALUATION_SERVICE_TOKEN}" -ge 32 ] || die "HUB_EVALUATION_SERVICE_TOKEN 至少需要 32 个字符"
[ "${HUB_EVALUATION_SERVICE_TOKEN}" != "${HUB_SERVICE_TOKEN}" ] \
    || die "HUB_EVALUATION_SERVICE_TOKEN 不能与 HUB_SERVICE_TOKEN 相同"
env_set "${ENV_FILE}" HUB_EVALUATION_SERVICE_TOKEN "${HUB_EVALUATION_SERVICE_TOKEN}"
export HUB_EVALUATION_SERVICE_TOKEN
log "Hub 服务凭据已就绪（值不输出）"

# 网络以调用方注入或 .env 显式配置为准，不按命名规律猜：猜错 compose 会报 external network not found
CONSUMER_NETWORK="${CONSUMER_NETWORK:-$(env_get "${ENV_FILE}" CONSUMER_NETWORK)}"
[ -n "${CONSUMER_NETWORK}" ] || die \
    "未指定共享 Docker 网络：设置 CONSUMER_NETWORK（整栈为 consumer 网络；单独部署 onto-modeling 时为其 compose 网络）"
docker network inspect "${CONSUMER_NETWORK}" >/dev/null 2>&1 \
    || die "Docker 网络不存在：${CONSUMER_NETWORK}；请先部署 consumer 或 onto-modeling"
export CONSUMER_NETWORK

ENV_ARGS=(--env-file "${ENV_FILE}")
if [ "${SOURCE_MODE}" = "0" ]; then
    # 只加载本发布版本的镜像：同一目录反复解包会累积历史版本 tar
    RELEASE_VERSION="$(env_get images.env RELEASE_VERSION)"
    for tarball in images/*${RELEASE_VERSION:+-${RELEASE_VERSION}}.tar; do
        [ -f "${tarball}" ] || continue
        log "加载镜像：${tarball}"
        docker load -i "${tarball}"
    done
    ENV_ARGS+=(--env-file images.env)
else
    [ -f ../Dockerfile ] || die "未找到 Hub 源码目录：$(cd .. && pwd)"
    KNOWLEDGE_HUB_IMAGE="${KNOWLEDGE_HUB_IMAGE:-knowledge-hub:latest}"
    export KNOWLEDGE_HUB_IMAGE
    log "从源码构建镜像 ${KNOWLEDGE_HUB_IMAGE}"
    # 镜像源默认见 Dockerfile（国内镜像）；显式配置时覆盖，企业内网可指向自有镜像
    BUILD_ARGS=()
    [ -z "${KNOWLEDGE_HUB_APT_MIRROR:-}" ] || BUILD_ARGS+=(--build-arg "APT_MIRROR=${KNOWLEDGE_HUB_APT_MIRROR}")
    PIP_INDEX="${KNOWLEDGE_HUB_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
    [ -z "${PIP_INDEX}" ] || BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX}")
    # 在目标目录内用相对路径调用：WSL 下 docker 可能是不认 /mnt/* 路径的 Windows 侧 docker.exe
    (cd .. && docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "${KNOWLEDGE_HUB_IMAGE}" .)
fi

"${COMPOSE[@]}" "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" up -d

# Hub 不发布宿主端口，就绪检查在容器内进行；失败时让调用方在部署 onto-modeling 前停下
elapsed=0
until docker exec "${CONTAINER}" python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/api/health', timeout=3)" \
    >/dev/null 2>&1; do
    [ "${elapsed}" -lt 60 ] || die "知识包 Hub 60 秒内未就绪；请查看 docker logs ${CONTAINER}"
    sleep 2
    elapsed=$((elapsed + 2))
done
log "知识包 Hub 已就绪：容器 ${CONTAINER}，网络 ${CONSUMER_NETWORK}"
