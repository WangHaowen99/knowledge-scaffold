#!/bin/bash
# 本机快速迭代：只重建并替换 onto-modeling 应用容器。
# 跳过发布包全流程（wheel 供应 / docker save / tar.gz 打包 / 解包 / docker load）——
# 本机构建的镜像就在 docker daemon 里，直接 compose 替换容器即可。
# 离线交付仍走 ./build.sh + deploy.sh（目标机没有本地镜像，必须走 tar 通道）。
#
# 用法：
#   deploy/redeploy-app.sh v0.13.4              # 构建 onto-modeling:v0.13.4 并替换容器
#   deploy/redeploy-app.sh v0.13.3 --no-build   # 镜像已存在，只替换容器

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# 默认指向本机解包出的部署目录；其他位置经环境变量覆盖
DEPLOY_DIR="${ONTO_MODELING_DEPLOY_DIR:-${PROJECT_ROOT}/dist/deploy/onto-modeling}"

[ -n "${1:-}" ] || { echo "Usage: $(basename "$0") <version-tag> [--no-build]" >&2; exit 1; }
VERSION="$1"; shift
NO_BUILD=0
for arg in "$@"; do
    case "${arg}" in
        --no-build) NO_BUILD=1 ;;
        *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
    esac
done

[ -f "${DEPLOY_DIR}/.env" ] || {
    echo "Error: 部署目录未初始化（先完整跑一次 deploy.sh）: ${DEPLOY_DIR}" >&2
    exit 1
}

if docker buildx version >/dev/null 2>&1; then
    BUILD_CMD="docker buildx build --load"
else
    BUILD_CMD="docker build"
fi

if [ "${NO_BUILD}" -eq 0 ]; then
    # docker 用相对路径调用：WSL 下 docker 可能是 Windows 侧 docker.exe（interop），
    # 不认 /mnt/* 绝对路径（与 build_release.sh 同约定）
    (cd "${PROJECT_ROOT}" && ${BUILD_CMD} -t "onto-modeling:${VERSION}" -f Dockerfile .)
    # 连接器网关（ADR-0073）：同一个 Dockerfile 的另一个目标，随主服务一起换
    (cd "${PROJECT_ROOT}" && ${BUILD_CMD} --target connector-gateway \
        -t "onto-modeling-connector-gateway:${VERSION}" -f Dockerfile .)
    if [ "${WITH_AUTH}" -eq 1 ]; then
        (cd "${REPOSITORY_ROOT}/auth-server" && ${BUILD_CMD} -t "onto-modeling-auth-server:${VERSION}" .)
    fi
else
    docker image inspect "onto-modeling:${VERSION}" >/dev/null 2>&1 || {
        echo "Error: 镜像 onto-modeling:${VERSION} 不在本机，去掉 --no-build 或先构建" >&2
        exit 1
    }
fi

# 更新部署目录镜像清单
sed -i "s|^ONTO_WEB_IMAGE=.*|ONTO_WEB_IMAGE=onto-modeling:${VERSION}|" "${DEPLOY_DIR}/images.env"
sed -i "s|^RELEASE_VERSION=.*|RELEASE_VERSION=${VERSION}|" "${DEPLOY_DIR}/images.env"
if grep -q '^CONNECTOR_GATEWAY_IMAGE=' "${DEPLOY_DIR}/images.env"; then
    sed -i "s|^CONNECTOR_GATEWAY_IMAGE=.*|CONNECTOR_GATEWAY_IMAGE=onto-modeling-connector-gateway:${VERSION}|" "${DEPLOY_DIR}/images.env"
else
    echo "CONNECTOR_GATEWAY_IMAGE=onto-modeling-connector-gateway:${VERSION}" >> "${DEPLOY_DIR}/images.env"
fi
if [ "${WITH_AUTH}" -eq 1 ]; then
    sed -i "s|^AUTH_SERVER_IMAGE=.*|AUTH_SERVER_IMAGE=onto-modeling-auth-server:${VERSION}|" "${DEPLOY_DIR}/images.env"
fi

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"
COMPOSE_FILES=(-f docker-compose.release.yml)
# 外接账号中心、jiuwenbox socket 的 override 由 deploy.sh 生成；存在即沿用，
# 否则替换容器后网络接入或网关的 socket 挂载会丢失
[ -f "${DEPLOY_DIR}/docker-compose.external-auth.yml" ] \
    && COMPOSE_FILES+=(-f docker-compose.external-auth.yml)
[ -f "${DEPLOY_DIR}/docker-compose.jiuwenbox.yml" ] \
    && COMPOSE_FILES+=(-f docker-compose.jiuwenbox.yml)

# 脚本验证网关与 onto-modeling 同一镜像，一起替换（旧部署目录的 compose 没有网关时跳过）
SERVICES=(onto-modeling)
grep -q '^  sandbox-gateway:' "${DEPLOY_DIR}/docker-compose.release.yml" && SERVICES+=(sandbox-gateway)
# 连接器网关：部署目录的 compose 里有它（新版 compose）才一起起
grep -q '^  connector-gateway:' "${DEPLOY_DIR}/docker-compose.release.yml" && SERVICES+=(connector-gateway)
[ "${WITH_AUTH}" -eq 1 ] && SERVICES+=(auth-server)

(cd "${DEPLOY_DIR}" && ${COMPOSE} --env-file .env --env-file images.env \
    "${COMPOSE_FILES[@]}" up -d --no-deps "${SERVICES[@]}")

echo "容器已替换：${SERVICES[*]} -> ${VERSION}"
