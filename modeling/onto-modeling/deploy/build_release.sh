#!/bin/bash
# 构建 Onto Web 离线交付包：镜像 tar + compose + 部署脚本，amd/arm 双架构（方案第八节）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"          # modeling/onto-modeling
REPOSITORY_ROOT="$(cd "${PROJECT_ROOT}/../.." && pwd)"  # 仓库根

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
ARCH="${2:-$(detect_arch)}"

case "${ARCH}" in
    amd) PLATFORM="linux/amd64"; RELEASE_ARCH="amd64" ;;
    arm) PLATFORM="linux/arm64"; RELEASE_ARCH="arm64" ;;
    *) echo "Error: Invalid arch '${ARCH}'. Use 'amd' or 'arm'." >&2; exit 1 ;;
esac

STAGE_ROOT="${PROJECT_ROOT}/dist/release-${VERSION}-${RELEASE_ARCH}"
BUNDLE_DIR="${STAGE_ROOT}/onto-modeling"
IMAGES_DIR="${BUNDLE_DIR}/images"
OUTPUT_FILE="${PROJECT_ROOT}/dist/onto-modeling-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"

cleanup() {
    [ -d "${STAGE_ROOT}" ] && rm -rf "${STAGE_ROOT}"
}
trap cleanup EXIT

rm -rf "${STAGE_ROOT}"
mkdir -p "${IMAGES_DIR}"

echo "Building Onto Web release ${VERSION} for ${PLATFORM}"

# pip 索引源：默认华为云 PyPI 镜像，企业内网等场景可用 ONTO_MODELING_PIP_INDEX_URL 或 PIP_INDEX_URL 覆盖
PIP_INDEX_URL="${ONTO_MODELING_PIP_INDEX_URL:-${PIP_INDEX_URL:-https://repo.huaweicloud.com/repository/pypi/simple}}"
TARGET_PYTHON_VERSION="3.11"  # 与 Dockerfile 基础镜像 python:3.11-slim 一致

# $1 >= $2，版本形如 3.10 / 3.11；无法解析时视为不满足
python_version_at_least() {
    local have_major have_minor want_major want_minor
    IFS=. read -r have_major have_minor <<< "$1"
    IFS=. read -r want_major want_minor <<< "$2"
    [ -n "${have_major}" ] && [ -n "${have_minor}" ] || return 1
    [ "${have_major}" -gt "${want_major}" ] \
        || { [ "${have_major}" -eq "${want_major}" ] && [ "${have_minor}" -ge "${want_minor}" ]; }
}

# 用宿主 pip 预下载目标平台 wheel 到 vendor/wheels（命中宿主缓存则零下载），
# 镜像构建经 bind mount 作为 find-links，索引不可达时兜底；失败不阻断，Dockerfile 会回落到镜像源。
# 目录跨构建累积旧版本无害（pip 按 requirements 选取），离线场景下不清空更稳妥。
# pip download 的 --python-version 只影响 wheel 兼容性，环境标记仍按宿主解释器求值：宿主 Python 低于
# 目标版本时 openjiuwen 等带 python_version 标记的依赖整棵被跳过，供应结果不完整、镜像构建照样联网，
# 预下载只会让大包（约 100MB 的 claude-agent-sdk）多下一遍，因此直接跳过。
VENDOR_DIR="${PROJECT_ROOT}/vendor/wheels"
mkdir -p "${VENDOR_DIR}"
case "${ARCH}" in
    amd) PIP_PLATFORMS=(--platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64) ;;
    arm) PIP_PLATFORMS=(--platform manylinux_2_17_aarch64 --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64) ;;
esac
if command -v pip3 >/dev/null 2>&1; then
    HOST_PIP_PYTHON="$(pip3 --version 2>/dev/null \
        | sed -n 's/.*(python \([0-9][0-9]*\.[0-9][0-9]*\)).*/\1/p' || true)"
    if python_version_at_least "${HOST_PIP_PYTHON}" "${TARGET_PYTHON_VERSION}"; then
        echo "Vendoring Python wheels for ${PLATFORM} into vendor/wheels"
        pip3 download -r "${PROJECT_ROOT}/backend/requirements.txt" -d "${VENDOR_DIR}" \
            --only-binary=:all: "${PIP_PLATFORMS[@]}" \
            --python-version "${TARGET_PYTHON_VERSION}" --implementation cp \
            --abi "cp${TARGET_PYTHON_VERSION//./}" --abi abi3 --abi none \
            -q -i "${PIP_INDEX_URL}" \
            || echo "Warning: wheel vendoring failed; image build will download from index"
    else
        echo "Note: host pip runs on Python ${HOST_PIP_PYTHON:-unknown} (< ${TARGET_PYTHON_VERSION}); skip wheel vendoring, image build will download from index"
    fi
else
    echo "Note: pip3 not found on host; image build will download from index"
fi

# drawio 官方 webapp（离线编辑器）：本机没有则从 npm 镜像获取一次，之后复用
DRAWIO_VERSION="2.7.0"  # @yank-note/extension-drawio，内含官方 drawio 31.4.5 webapp
DRAWIO_DIR="${PROJECT_ROOT}/vendor/drawio/webapp"
mkdir -p "${DRAWIO_DIR}"
if [ ! -f "${DRAWIO_DIR}/index.html" ]; then
    echo "Vendoring drawio webapp into vendor/drawio/webapp"
    DRAWIO_TMP="$(mktemp -d)"
    if (cd "${DRAWIO_TMP}" \
        && npm pack "@yank-note/extension-drawio@${DRAWIO_VERSION}" \
            --registry=https://registry.npmmirror.com >/dev/null 2>&1 \
        && tar -xzf yank-note-extension-drawio-*.tgz package/drawio/src/main/webapp \
        && cp -r package/drawio/src/main/webapp/. "${DRAWIO_DIR}/"); then
        echo "drawio webapp vendored."
    else
        echo "Warning: drawio webapp vendoring failed; 该发布包中 drawio 编辑功能不可用（预览不受影响的 mermaid/markdown 除外）"
    fi
    rm -rf "${DRAWIO_TMP}"
fi

if docker buildx version >/dev/null 2>&1; then
    BUILD_CMD="docker buildx build --platform ${PLATFORM} --load"
else
    echo "Note: docker buildx not available, falling back to docker build"
    BUILD_CMD="docker build"
fi

# docker 一律在目标目录内用相对路径调用：WSL 下 docker 可能是 Windows 侧
# docker.exe（interop），不认 /mnt/* 绝对路径，相对路径会被正确映射到盘符路径
(cd "${PROJECT_ROOT}" && ${BUILD_CMD} --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}" \
    -t "onto-modeling:${VERSION}" -f Dockerfile .)
(cd "${IMAGES_DIR}" && docker save -o "onto-modeling-${VERSION}.tar" "onto-modeling:${VERSION}")

AUTH_SERVER_SRC="${REPOSITORY_ROOT}/auth-server"
[ -d "${AUTH_SERVER_SRC}" ] || {
    echo "Error: auth-server source directory not found: ${AUTH_SERVER_SRC}" >&2
    exit 1
}
(cd "${AUTH_SERVER_SRC}" && ${BUILD_CMD} -t "onto-modeling-auth-server:${VERSION}" .)
(cd "${IMAGES_DIR}" && docker save -o "onto-modeling-auth-server-${VERSION}.tar" "onto-modeling-auth-server:${VERSION}")

cp "${SCRIPT_DIR}/docker-compose.release.yml" "${BUNDLE_DIR}/"
cp "${SCRIPT_DIR}/deploy.sh" "${SCRIPT_DIR}/preflight.sh" \
   "${SCRIPT_DIR}/configure-modeling-association.sh" \
   "${SCRIPT_DIR}/migrate-from-modeling.sh" "${BUNDLE_DIR}/"
cp "${SCRIPT_DIR}/README.md" "${BUNDLE_DIR}/README.md"
cp "${PROJECT_ROOT}/.env.example" "${BUNDLE_DIR}/.env.example"
chmod +x "${BUNDLE_DIR}/deploy.sh" "${BUNDLE_DIR}/preflight.sh" \
    "${BUNDLE_DIR}/configure-modeling-association.sh" \
    "${BUNDLE_DIR}/migrate-from-modeling.sh"

# 镜像清单：deploy.sh / compose 从这里取镜像 tag，目标机不需要再传参数
cat > "${BUNDLE_DIR}/images.env" <<EOF
ONTO_WEB_IMAGE=onto-modeling:${VERSION}
AUTH_SERVER_IMAGE=onto-modeling-auth-server:${VERSION}
RELEASE_VERSION=${VERSION}
RELEASE_ARCH=${RELEASE_ARCH}
EOF

mkdir -p "$(dirname "${OUTPUT_FILE}")"
tar -czf "${OUTPUT_FILE}" -C "${STAGE_ROOT}" onto-modeling

echo "Release package: ${OUTPUT_FILE}"
