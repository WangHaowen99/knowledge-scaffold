#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

JIUWENSWARM_REPOSITORY="https://gitcode.com/openJiuwen/jiuwenswarm.git"
JIUWENSWARM_COMMIT="d7c2ab2f299cd2c73201afb41ebb8b9c00cc7015"

usage() {
    cat <<EOF
Usage: $(basename "$0") <image-tag> [amd|arm]

Build the Onto Agent JiuwenBox image from the pinned JiuwenSwarm commit.

Examples:
  $(basename "$0") onto-agent-jiuwenbox:v1.2.0
  $(basename "$0") onto-agent-jiuwenbox:v1.2.0 amd
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

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    usage
    exit 1
fi

TARGET_IMAGE="$1"
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

COMMIT_SHORT="${JIUWENSWARM_COMMIT:0:12}"
UPSTREAM_IMAGE="onto-agent-jiuwenbox-base:${COMMIT_SHORT}-${RELEASE_ARCH}"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/onto-agent-jiuwenbox.XXXXXX")"
SOURCE_ROOT="${BUILD_ROOT}/jiuwenswarm"
OVERLAY_ROOT="${BUILD_ROOT}/overlay"

cleanup() {
    rm -rf "${BUILD_ROOT}"
}
trap cleanup EXIT

command -v git >/dev/null 2>&1 || {
    echo "Error: git is required" >&2
    exit 1
}
command -v docker >/dev/null 2>&1 || {
    echo "Error: docker is required" >&2
    exit 1
}

if docker buildx version >/dev/null 2>&1; then
    BUILD_CMD="docker buildx build --platform ${PLATFORM} --load"
else
    echo "Note: docker buildx not available, falling back to docker build"
    BUILD_CMD="docker build"
fi

echo "Fetching JiuwenSwarm commit ${JIUWENSWARM_COMMIT}"
git init -q "${SOURCE_ROOT}"
git -C "${SOURCE_ROOT}" remote add origin "${JIUWENSWARM_REPOSITORY}"
git -C "${SOURCE_ROOT}" sparse-checkout init --cone
git -C "${SOURCE_ROOT}" sparse-checkout set jiuwenbox
git -C "${SOURCE_ROOT}" fetch --depth=1 origin "${JIUWENSWARM_COMMIT}"

ACTUAL_COMMIT="$(git -C "${SOURCE_ROOT}" rev-parse FETCH_HEAD)"
if [ "${ACTUAL_COMMIT}" != "${JIUWENSWARM_COMMIT}" ]; then
    echo "Error: expected JiuwenSwarm commit ${JIUWENSWARM_COMMIT}, got ${ACTUAL_COMMIT}" >&2
    exit 1
fi
git -C "${SOURCE_ROOT}" checkout -q --detach "${JIUWENSWARM_COMMIT}"

echo "Building upstream JiuwenBox image ${UPSTREAM_IMAGE} for ${PLATFORM}"
${BUILD_CMD} \
    -f "${SOURCE_ROOT}/jiuwenbox/docker/Dockerfile" \
    -t "${UPSTREAM_IMAGE}" \
    "${SOURCE_ROOT}/jiuwenbox"

mkdir -p "${OVERLAY_ROOT}/runtime/shared_capabilities"
cp "${SCRIPT_DIR}/jiuwenbox/requirements.txt" "${OVERLAY_ROOT}/requirements.txt"
cp "${PROJECT_ROOT}/backend/shared_capabilities/__init__.py" \
    "${OVERLAY_ROOT}/runtime/shared_capabilities/__init__.py"
cp -R "${PROJECT_ROOT}/backend/shared_capabilities/business_data" \
    "${OVERLAY_ROOT}/runtime/shared_capabilities/business_data"
cp -R "${PROJECT_ROOT}/backend/shared_capabilities/ontology_script_sdk" \
    "${OVERLAY_ROOT}/runtime/shared_capabilities/ontology_script_sdk"

echo "Building Onto Agent JiuwenBox image ${TARGET_IMAGE} for ${PLATFORM}"
${BUILD_CMD} \
    --build-arg "JIUWENBOX_BASE=${UPSTREAM_IMAGE}" \
    --build-arg "JIUWENSWARM_COMMIT=${JIUWENSWARM_COMMIT}" \
    -f "${SCRIPT_DIR}/jiuwenbox/Dockerfile" \
    -t "${TARGET_IMAGE}" \
    "${OVERLAY_ROOT}"

echo "Built ${TARGET_IMAGE} from JiuwenSwarm commit ${JIUWENSWARM_COMMIT}"
