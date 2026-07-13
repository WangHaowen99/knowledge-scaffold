#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") <version> [amd|arm]

Examples:
  $(basename "$0") v1.2.0
  $(basename "$0") v1.2.0 amd
EOF
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64)
            printf 'amd\n'
            ;;
        aarch64|arm64)
            printf 'arm\n'
            ;;
        *)
            printf 'amd\n'
            ;;
    esac
}

if [ -z "${1:-}" ]; then
    usage
    exit 1
fi

if [ "$#" -gt 2 ]; then
    usage
    exit 1
fi

VERSION="$1"
ARCH="${2:-$(detect_arch)}"

case "${ARCH}" in
    amd)
        PLATFORM="linux/amd64"
        RELEASE_ARCH="amd64"
        ;;
    arm)
        PLATFORM="linux/arm64"
        RELEASE_ARCH="arm64"
        ;;
    *)
        echo "Error: Invalid arch '${ARCH}'. Use 'amd' or 'arm'." >&2
        exit 1
        ;;
esac

STAGE_ROOT="${PROJECT_ROOT}/dist/release-${VERSION}-${RELEASE_ARCH}"
BUNDLE_DIR="${STAGE_ROOT}/onto-agent"
IMAGES_DIR="${BUNDLE_DIR}/images"
OUTPUT_FILE="${PROJECT_ROOT}/dist/onto-agent-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"

cleanup() {
    if [ -n "${STAGE_ROOT:-}" ] && [ -d "${STAGE_ROOT}" ]; then
        rm -rf "${STAGE_ROOT}"
    fi
}

trap cleanup EXIT

rm -rf "${STAGE_ROOT}"
mkdir -p "${IMAGES_DIR}" "${BUNDLE_DIR}/backend" "${BUNDLE_DIR}/frontend" "${BUNDLE_DIR}/deploy/state"

echo "Building release ${VERSION} for ${PLATFORM}"

if docker buildx version >/dev/null 2>&1; then
    BUILD_CMD="docker buildx build --platform ${PLATFORM} --load"
else
    echo "Note: docker buildx not available, falling back to docker build"
    BUILD_CMD="docker build"
fi

${BUILD_CMD} \
    -t "onto-agent-backend:${VERSION}" \
    "${PROJECT_ROOT}/backend"
docker save -o "${IMAGES_DIR}/onto-agent-backend-${VERSION}.tar" "onto-agent-backend:${VERSION}"

${BUILD_CMD} \
    -t "onto-agent-frontend:${VERSION}" \
    "${PROJECT_ROOT}/frontend"
docker save -o "${IMAGES_DIR}/onto-agent-frontend-${VERSION}.tar" "onto-agent-frontend:${VERSION}"

# auth-server 与 consumer 平级（架构决策 D4），构建上下文在 consumer/ 的上一级
AUTH_SERVER_SRC="${PROJECT_ROOT}/../auth-server"
[ -d "${AUTH_SERVER_SRC}" ] || {
    echo "Error: auth-server source directory not found: ${AUTH_SERVER_SRC}" >&2
    exit 1
}
${BUILD_CMD} \
    -t "onto-agent-auth-server:${VERSION}" \
    "${AUTH_SERVER_SRC}"
docker save -o "${IMAGES_DIR}/onto-agent-auth-server-${VERSION}.tar" "onto-agent-auth-server:${VERSION}"

cp "${PROJECT_ROOT}/backend/.env.example" "${BUNDLE_DIR}/backend/.env.example"
cp "${PROJECT_ROOT}/frontend/.env.example" "${BUNDLE_DIR}/frontend/.env.example"
cp -r "${PROJECT_ROOT}/deploy/"* "${BUNDLE_DIR}/deploy/"
rm -rf "${BUNDLE_DIR}/deploy/state" "${BUNDLE_DIR}/deploy/backups" "${BUNDLE_DIR}/deploy/tests"
find "${BUNDLE_DIR}/deploy" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "${BUNDLE_DIR}/deploy" -type f -name '*.pyc' -delete
find "${BUNDLE_DIR}/deploy" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
chmod +x "${BUNDLE_DIR}/deploy/"*.sh

mkdir -p \
    "${BUNDLE_DIR}/backend/config" \
    "${BUNDLE_DIR}/backend/logs" \
    "${BUNDLE_DIR}/.onto-runtime/config" \
    "${BUNDLE_DIR}/.onto-auth" \
    "${BUNDLE_DIR}/frontend/logs/nginx" \
    "${BUNDLE_DIR}/deploy/backups"

cat > "${BUNDLE_DIR}/deploy/manifest.json" <<EOF
{
  "app_version": "${VERSION}",
  "release_arch": "${RELEASE_ARCH}",
  "backend_image": "onto-agent-backend:${VERSION}",
  "frontend_image": "onto-agent-frontend:${VERSION}",
  "auth_server_image": "onto-agent-auth-server:${VERSION}",
  "backend_image_tar": "images/onto-agent-backend-${VERSION}.tar",
  "frontend_image_tar": "images/onto-agent-frontend-${VERSION}.tar",
  "auth_server_image_tar": "images/onto-agent-auth-server-${VERSION}.tar"
}
EOF

printf '%s\n' "${VERSION}" > "${BUNDLE_DIR}/VERSION"

if command -v sha256sum >/dev/null 2>&1; then
    (
        cd "${BUNDLE_DIR}"
        sha256sum VERSION deploy/manifest.json images/*.tar > checksums.txt
    )
fi

mkdir -p "${PROJECT_ROOT}/dist"
rm -f "${OUTPUT_FILE}"
tar -C "${STAGE_ROOT}" -czf "${OUTPUT_FILE}" onto-agent

echo "Release package created: ${OUTPUT_FILE}"
