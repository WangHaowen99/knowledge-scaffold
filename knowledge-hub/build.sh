#!/bin/bash
# 构建知识包 Hub 离线交付包：镜像 tar + compose + 部署脚本，与 onto-modeling 包相互独立。
# compose 与部署脚本的源文件在 knowledge-hub/deploy/ 下，与源码部署共用同一份；包内布局不变。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SOURCE="${SCRIPT_DIR}/deploy"

usage() {
    cat <<EOF
Usage: $(basename "$0") <version> [amd|arm]

Examples:
  $(basename "$0") v0.1.0
  $(basename "$0") v0.1.0 arm
EOF
}

detect_arch() {
    case "$(uname -m)" in
        aarch64|arm64) printf 'arm\n' ;;
        *) printf 'amd\n' ;;
    esac
}

[ -n "${1:-}" ] || { usage; exit 1; }
[ "$#" -le 2 ] || { usage; exit 1; }

VERSION="$1"
[[ "${VERSION}" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo "Error: Invalid version '${VERSION}'." >&2; exit 1; }
ARCH="${2:-$(detect_arch)}"

case "${ARCH}" in
    amd) PLATFORM="linux/amd64"; RELEASE_ARCH="amd64" ;;
    arm) PLATFORM="linux/arm64"; RELEASE_ARCH="arm64" ;;
    *) echo "Error: Invalid arch '${ARCH}'. Use 'amd' or 'arm'." >&2; exit 1 ;;
esac

STAGE_ROOT="${SCRIPT_DIR}/dist/release-${VERSION}-${RELEASE_ARCH}"
BUNDLE_DIR="${STAGE_ROOT}/knowledge-hub"
IMAGES_DIR="${BUNDLE_DIR}/images"
OUTPUT_FILE="${SCRIPT_DIR}/dist/knowledge-hub-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"

cleanup() {
    [ ! -d "${STAGE_ROOT}" ] || rm -rf "${STAGE_ROOT}"
}
trap cleanup EXIT

rm -rf "${STAGE_ROOT}"
mkdir -p "${IMAGES_DIR}"

echo "Building Knowledge Package Hub release ${VERSION} for ${PLATFORM}"

if docker buildx version >/dev/null 2>&1; then
    BUILD_CMD=(docker buildx build --platform "${PLATFORM}" --load)
else
    echo "Note: docker buildx not available, falling back to docker build"
    BUILD_CMD=(docker build)
fi

# 镜像源默认见 Dockerfile（国内镜像）；显式配置时覆盖，企业内网可指向自有镜像
BUILD_ARGS=()
[ -z "${KNOWLEDGE_HUB_APT_MIRROR:-}" ] || BUILD_ARGS+=(--build-arg "APT_MIRROR=${KNOWLEDGE_HUB_APT_MIRROR}")
PIP_INDEX="${KNOWLEDGE_HUB_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
[ -z "${PIP_INDEX}" ] || BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX}")

# docker 一律在目标目录内用相对路径调用：WSL 下 docker 可能是 Windows 侧
# docker.exe（interop），不认 /mnt/* 绝对路径
(cd "${SCRIPT_DIR}" && "${BUILD_CMD[@]}" ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "knowledge-hub:${VERSION}" .)
(cd "${IMAGES_DIR}" && docker save -o "knowledge-hub-${VERSION}.tar" "knowledge-hub:${VERSION}")

cp "${DEPLOY_SOURCE}/docker-compose.knowledge-hub.yml" "${BUNDLE_DIR}/"
cp "${DEPLOY_SOURCE}/deploy.sh" "${BUNDLE_DIR}/deploy.sh"
cp "${SCRIPT_DIR}/README.md" "${BUNDLE_DIR}/README.md"
chmod +x "${BUNDLE_DIR}/deploy.sh"

# 镜像清单：deploy.sh / compose 从这里取镜像 tag，目标机不需要再传参数
cat > "${BUNDLE_DIR}/images.env" <<EOF
KNOWLEDGE_HUB_IMAGE=knowledge-hub:${VERSION}
RELEASE_VERSION=${VERSION}
RELEASE_ARCH=${RELEASE_ARCH}
EOF

mkdir -p "$(dirname "${OUTPUT_FILE}")"
tar -czf "${OUTPUT_FILE}" -C "${STAGE_ROOT}" knowledge-hub

echo "Release package: ${OUTPUT_FILE}"
