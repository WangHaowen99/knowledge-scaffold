#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RUNTIME_CONTRACT="${PROJECT_ROOT}/backend/integration/sandbox/runtime-contract.json"
RUNTIME_REQUIREMENTS="${PROJECT_ROOT}/backend/integration/sandbox/runtime-requirements.txt"

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

command -v git >/dev/null 2>&1 || {
    echo "Error: git is required" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    echo "Error: python3 is required" >&2
    exit 1
}

JIUWENSWARM_REPOSITORY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["upstream"]["repository"])' "${RUNTIME_CONTRACT}")"
JIUWENSWARM_COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["upstream"]["commit"])' "${RUNTIME_CONTRACT}")"
COMMIT_SHORT="${JIUWENSWARM_COMMIT:0:12}"
UPSTREAM_IMAGE="onto-agent-jiuwenbox-base:${COMMIT_SHORT}-${RELEASE_ARCH}"
BUILD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/onto-agent-jiuwenbox.XXXXXX")"
SOURCE_ROOT="${BUILD_ROOT}/jiuwenswarm"
OVERLAY_ROOT="${BUILD_ROOT}/overlay"
PAYLOAD_IMAGE="onto-agent-jiuwenbox-payload:${COMMIT_SHORT}-${RELEASE_ARCH}-$$"
PAYLOAD_IMAGE_CREATED=0

cleanup() {
    if [ "${PAYLOAD_IMAGE_CREATED}" = "1" ]; then
        docker image rm "${PAYLOAD_IMAGE}" >/dev/null 2>&1 || true
    fi
    rm -rf "${BUILD_ROOT}"
}
trap cleanup EXIT

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
cp "${RUNTIME_REQUIREMENTS}" "${OVERLAY_ROOT}/requirements.txt"
cp "${RUNTIME_CONTRACT}" "${OVERLAY_ROOT}/runtime-contract.json"
cp "${SCRIPT_DIR}/jiuwenbox/runtime-entrypoint.sh" \
    "${OVERLAY_ROOT}/runtime-entrypoint.sh"
cp -R "${PROJECT_ROOT}/backend/onto_sandbox_runtime" \
    "${OVERLAY_ROOT}/runtime/onto_sandbox_runtime"
cp "${PROJECT_ROOT}/backend/shared_capabilities/__init__.py" \
    "${OVERLAY_ROOT}/runtime/shared_capabilities/__init__.py"
cp -R "${PROJECT_ROOT}/backend/shared_capabilities/business_data" \
    "${OVERLAY_ROOT}/runtime/shared_capabilities/business_data"
cp -R "${PROJECT_ROOT}/backend/shared_capabilities/ontology_script_sdk" \
    "${OVERLAY_ROOT}/runtime/shared_capabilities/ontology_script_sdk"
find "${OVERLAY_ROOT}/runtime" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "${OVERLAY_ROOT}/runtime" -type f -name '*.pyc' -delete

echo "Building behavior-bearing JiuwenBox payload ${PAYLOAD_IMAGE} for ${PLATFORM}"
${BUILD_CMD} \
    --target runtime-payload \
    --build-arg "JIUWENBOX_BASE=${UPSTREAM_IMAGE}" \
    --build-arg "RUNTIME_PAYLOAD_IMAGE=${UPSTREAM_IMAGE}" \
    -f "${SCRIPT_DIR}/jiuwenbox/Dockerfile" \
    -t "${PAYLOAD_IMAGE}" \
    "${OVERLAY_ROOT}"
PAYLOAD_IMAGE_CREATED=1

PAYLOAD_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${PAYLOAD_IMAGE}")"
case "${PAYLOAD_IMAGE_ID}" in
    sha256:*) ;;
    *)
        echo "Error: invalid JiuwenBox payload image id '${PAYLOAD_IMAGE_ID}'" >&2
        exit 1
        ;;
esac

SANDBOX_RUNTIME_REVISION="$(
    PYTHONPATH="${PROJECT_ROOT}/backend" python3 -c \
        'from integration.sandbox.runtime_contract import sandbox_runtime_revision; import sys; print(sandbox_runtime_revision(sys.argv[1], sys.argv[2]))' \
        "${RELEASE_ARCH}" "${PAYLOAD_IMAGE_ID}"
)"

echo "Building Onto Agent JiuwenBox image ${TARGET_IMAGE} for ${PLATFORM}"
${BUILD_CMD} \
    --target runtime-attested \
    --build-arg "JIUWENBOX_BASE=${UPSTREAM_IMAGE}" \
    --build-arg "RUNTIME_PAYLOAD_IMAGE=${PAYLOAD_IMAGE}" \
    --build-arg "JIUWENSWARM_COMMIT=${JIUWENSWARM_COMMIT}" \
    --build-arg "SANDBOX_RUNTIME_REVISION=${SANDBOX_RUNTIME_REVISION}" \
    -f "${SCRIPT_DIR}/jiuwenbox/Dockerfile" \
    -t "${TARGET_IMAGE}" \
    "${OVERLAY_ROOT}"

echo "Built ${TARGET_IMAGE} from JiuwenSwarm commit ${JIUWENSWARM_COMMIT}"
