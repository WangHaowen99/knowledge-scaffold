#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") <version> [amd|arm]

Build a full release package by calling each service build script.

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
        RELEASE_ARCH="amd64"
        ;;
    arm)
        RELEASE_ARCH="arm64"
        ;;
    *)
        echo "Error: Invalid arch '${ARCH}'. Use 'amd' or 'arm'." >&2
        exit 1
        ;;
esac

STAGE_ROOT="${REPO_ROOT}/dist/release-${VERSION}-${RELEASE_ARCH}"
BUNDLE_DIR="${STAGE_ROOT}/onto-agent"
OUTPUT_FILE="${REPO_ROOT}/dist/onto-agent-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"
CONSUMER_PACKAGE="${REPO_ROOT}/consumer/dist/onto-agent-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"

cleanup() {
    if [ -n "${STAGE_ROOT:-}" ] && [ -d "${STAGE_ROOT}" ]; then
        rm -rf "${STAGE_ROOT}"
    fi
}

trap cleanup EXIT

rm -rf "${STAGE_ROOT}"
mkdir -p "${REPO_ROOT}/dist" "${STAGE_ROOT}"

echo "Building full release ${VERSION} (${RELEASE_ARCH})"

chmod +x "${REPO_ROOT}/consumer/build.sh" "${REPO_ROOT}/consumer/deploy/"*.sh
bash "${REPO_ROOT}/consumer/build.sh" "${VERSION}" "${ARCH}"

[ -f "${CONSUMER_PACKAGE}" ] || {
    echo "Error: consumer package not found: ${CONSUMER_PACKAGE}" >&2
    exit 1
}

tar -C "${STAGE_ROOT}" -xzf "${CONSUMER_PACKAGE}"
cp "${BUNDLE_DIR}/deploy/deploy.sh" "${BUNDLE_DIR}/deploy/deploy-consumer.sh"

# Onto Modeling（引导式建模工作台）：独立子包并入 bundle，部署脚本自带零配置 auth
chmod +x "${REPO_ROOT}/modeling/onto-modeling/build.sh" "${REPO_ROOT}/modeling/onto-modeling/deploy/"*.sh
bash "${REPO_ROOT}/modeling/onto-modeling/build.sh" "${VERSION}" "${ARCH}"
ONTO_MODELING_PACKAGE="${REPO_ROOT}/modeling/onto-modeling/dist/onto-modeling-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"
[ -f "${ONTO_MODELING_PACKAGE}" ] || {
    echo "Error: onto-modeling package not found: ${ONTO_MODELING_PACKAGE}" >&2
    exit 1
}
tar -C "${BUNDLE_DIR}" -xzf "${ONTO_MODELING_PACKAGE}"

# 知识包 Hub：独立子包并入 bundle，与 Onto Modeling 分开部署
chmod +x "${REPO_ROOT}/knowledge-hub/build.sh" "${REPO_ROOT}/knowledge-hub/deploy/deploy.sh"
bash "${REPO_ROOT}/knowledge-hub/build.sh" "${VERSION}" "${ARCH}"
KNOWLEDGE_HUB_PACKAGE="${REPO_ROOT}/knowledge-hub/dist/knowledge-hub-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"
[ -f "${KNOWLEDGE_HUB_PACKAGE}" ] || {
    echo "Error: knowledge hub package not found: ${KNOWLEDGE_HUB_PACKAGE}" >&2
    exit 1
}
tar -C "${BUNDLE_DIR}" -xzf "${KNOWLEDGE_HUB_PACKAGE}"

# 独立评测服务：同 Hub，独立子包留在 eval-service/dist 并并入 bundle
EVALUATION_PACKAGE="${REPO_ROOT}/eval-service/dist/evaluation-${VERSION}-linux-${RELEASE_ARCH}.tar.gz"
# eval-service/build.sh 拒绝覆盖已有产物；完整构建与其他子包一样覆盖同版本旧包
rm -f "${EVALUATION_PACKAGE}"
bash "${REPO_ROOT}/eval-service/build.sh" "${VERSION}" "${ARCH}"
[ -f "${EVALUATION_PACKAGE}" ] || {
    echo "Error: evaluation package not found: ${EVALUATION_PACKAGE}" >&2
    exit 1
}
tar -C "${BUNDLE_DIR}" -xzf "${EVALUATION_PACKAGE}"

cp "${SCRIPT_DIR}/deploy.sh" "${BUNDLE_DIR}/deploy/deploy.sh"

cat > "${BUNDLE_DIR}/deploy/manifest.json" <<EOF
{
  "app_version": "${VERSION}",
  "release_arch": "${RELEASE_ARCH}",
  "backend_image": "onto-agent-backend:${VERSION}",
  "storage_utility_image": "onto-agent-backend:${VERSION}",
  "frontend_image": "onto-agent-frontend:${VERSION}",
  "auth_server_image": "onto-agent-auth-server:${VERSION}",
  "jiuwenbox_image": "onto-agent-jiuwenbox:${VERSION}",
  "backend_image_tar": "images/onto-agent-backend-${VERSION}.tar",
  "frontend_image_tar": "images/onto-agent-frontend-${VERSION}.tar",
  "auth_server_image_tar": "images/onto-agent-auth-server-${VERSION}.tar",
  "jiuwenbox_image_tar": "images/onto-agent-jiuwenbox-${VERSION}.tar",
  "onto_modeling_image": "onto-modeling:${VERSION}",
  "onto_modeling_auth_server_image": "onto-modeling-auth-server:${VERSION}",
  "onto_modeling_bundle_dir": "onto-modeling",
  "knowledge_hub_image": "knowledge-hub:${VERSION}",
  "knowledge_hub_image_tar": "knowledge-hub/images/knowledge-hub-${VERSION}.tar",
  "knowledge_hub_bundle_dir": "knowledge-hub",
  "evaluation_image": "knowledge-evaluation:${VERSION}",
  "evaluation_image_tar": "eval-service/evaluation-image.tar",
  "evaluation_bundle_dir": "eval-service",
  "jiuwenswarm_commit": "d7c2ab2f299cd2c73201afb41ebb8b9c00cc7015"
}
EOF

printf '%s\n' "${VERSION}" > "${BUNDLE_DIR}/VERSION"

find "${BUNDLE_DIR}/deploy" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
find "${BUNDLE_DIR}/deploy" -type f -name '*.sh' -exec chmod +x {} +

if command -v sha256sum >/dev/null 2>&1; then
    (
        cd "${BUNDLE_DIR}"
        sha256sum VERSION deploy/manifest.json images/*.tar knowledge-hub/images/*.tar \
            eval-service/evaluation-image.tar > checksums.txt
    )
fi

rm -f "${OUTPUT_FILE}"
tar -C "${STAGE_ROOT}" -czf "${OUTPUT_FILE}" onto-agent

echo "Full release package created: ${OUTPUT_FILE}"
