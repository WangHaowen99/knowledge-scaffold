#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

STANDALONE=0
if [ -z "${RELEASE_BUNDLE_DIR:-}" ]; then
    STANDALONE=1
    STAGE_ROOT="${SCRIPT_DIR}/dist/release-${VERSION}-${RELEASE_ARCH}"
    BUNDLE_DIR="${STAGE_ROOT}/modeling"
    DEPLOY_DIR="${BUNDLE_DIR}/deploy"
    OUTPUT_FILE="${SCRIPT_DIR}/dist/modeling-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"
    rm -rf "${STAGE_ROOT}"
else
    BUNDLE_DIR="${RELEASE_BUNDLE_DIR}"
    DEPLOY_DIR="${BUNDLE_DIR}/deploy/modeling"
fi

IMAGES_DIR="${BUNDLE_DIR}/images"
mkdir -p "${IMAGES_DIR}" "${DEPLOY_DIR}"

echo "Building modeling release ${VERSION} for ${PLATFORM}"

if docker buildx version >/dev/null 2>&1; then
    BUILD_CMD=(docker buildx build --platform "${PLATFORM}" --load)
else
    echo "Note: docker buildx not available, falling back to docker build"
    BUILD_CMD=(docker build)
fi

"${BUILD_CMD[@]}" \
    -t "mw-modeling:${VERSION}" \
    "${SCRIPT_DIR}"
docker save -o "${IMAGES_DIR}/mw-modeling-${VERSION}.tar" "mw-modeling:${VERSION}"

"${BUILD_CMD[@]}" \
    --target runtime \
    -t "mw-modeling-agent:${VERSION}" \
    "${SCRIPT_DIR}/modeling-agent"
docker save -o "${IMAGES_DIR}/mw-modeling-agent-${VERSION}.tar" "mw-modeling-agent:${VERSION}"

cp -r "${SCRIPT_DIR}/deploy/"* "${DEPLOY_DIR}/"

if [ "${STANDALONE}" = "1" ]; then
    cat > "${DEPLOY_DIR}/manifest.json" <<EOF
{
  "app_version": "${VERSION}",
  "release_arch": "${RELEASE_ARCH}",
  "modeling_image": "mw-modeling:${VERSION}",
  "modeling_image_tar": "images/mw-modeling-${VERSION}.tar",
  "modeling_agent_image": "mw-modeling-agent:${VERSION}",
  "modeling_agent_image_tar": "images/mw-modeling-agent-${VERSION}.tar"
}
EOF
    printf '%s\n' "${VERSION}" > "${BUNDLE_DIR}/VERSION"

    find "${BUNDLE_DIR}" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
    find "${BUNDLE_DIR}" -type f -name '*.sh' -exec chmod +x {} +

    if command -v sha256sum >/dev/null 2>&1; then
        (
            cd "${BUNDLE_DIR}"
            sha256sum VERSION deploy/manifest.json images/*.tar > checksums.txt
        )
    fi

    mkdir -p "$(dirname "${OUTPUT_FILE}")"
    rm -f "${OUTPUT_FILE}"
    tar -C "${STAGE_ROOT}" -czf "${OUTPUT_FILE}" modeling
    rm -rf "${STAGE_ROOT}"
    echo "Modeling release package created: ${OUTPUT_FILE}"
else
    find "${DEPLOY_DIR}" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
    find "${DEPLOY_DIR}" -type f -name '*.sh' -exec chmod +x {} +
    echo "Modeling release bundle staged: ${BUNDLE_DIR}"
fi
