#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF_USAGE
Usage: $(basename "$0") --version <version> [--arch <amd|arm>]
       $(basename "$0") <version> [amd|arm]

Examples:
  $(basename "$0") --version v1.2.0 --arch amd
  $(basename "$0") v1.2.0 amd
EOF_USAGE
}

detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) printf 'amd\n' ;;
        aarch64|arm64) printf 'arm\n' ;;
        *) printf 'amd\n' ;;
    esac
}

VERSION=""
ARCH=""
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
elif [ "${1:-}" = "--version" ] || [ "${1:-}" = "--arch" ]; then
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version)
                [ "$#" -ge 2 ] || { usage >&2; exit 1; }
                VERSION="$2"
                shift 2
                ;;
            --arch)
                [ "$#" -ge 2 ] || { usage >&2; exit 1; }
                ARCH="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                usage >&2
                exit 1
                ;;
        esac
    done
else
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || { usage >&2; exit 1; }
    VERSION="$1"
    ARCH="${2:-}"
fi

[ -n "${VERSION}" ] || { usage >&2; exit 1; }
ARCH="${ARCH:-$(detect_arch)}"
case "${ARCH}" in
    amd) PLATFORM="linux/amd64"; RELEASE_ARCH="amd64" ;;
    arm) PLATFORM="linux/arm64"; RELEASE_ARCH="arm64" ;;
    *) echo "Error: Invalid arch '${ARCH}'. Use 'amd' or 'arm'." >&2; exit 1 ;;
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

AGENT_PIP_INDEX_URL="${MODELING_AGENT_PIP_INDEX_URL:-${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}}"
AGENT_PIP_TIMEOUT="${MODELING_AGENT_PIP_TIMEOUT:-${PIP_DEFAULT_TIMEOUT:-120}}"
AGENT_PIP_RETRIES="${MODELING_AGENT_PIP_RETRIES:-${PIP_RETRIES:-10}}"

"${BUILD_CMD[@]}" -t "mw-modeling:${VERSION}" "${SCRIPT_DIR}"
docker save -o "${IMAGES_DIR}/mw-modeling-${VERSION}.tar" "mw-modeling:${VERSION}"

"${BUILD_CMD[@]}" \
    --target runtime \
    --build-arg "PIP_INDEX_URL=${AGENT_PIP_INDEX_URL}" \
    --build-arg "PIP_DEFAULT_TIMEOUT=${AGENT_PIP_TIMEOUT}" \
    --build-arg "PIP_RETRIES=${AGENT_PIP_RETRIES}" \
    -t "mw-modeling-agent:${VERSION}" \
    "${SCRIPT_DIR}/modeling-agent"
docker save -o "${IMAGES_DIR}/mw-modeling-agent-${VERSION}.tar" "mw-modeling-agent:${VERSION}"

cp -r "${SCRIPT_DIR}/deploy/"* "${DEPLOY_DIR}/"

if [ "${STANDALONE}" = "1" ]; then
    mkdir -p "${DEPLOY_DIR}/bin"
    COMPOSE_PLUGIN_PATH="${DOCKER_COMPOSE_PLUGIN_PATH:-$(command -v docker-compose 2>/dev/null || true)}"
    if [ -z "${COMPOSE_PLUGIN_PATH}" ] && [ -x /usr/libexec/docker/cli-plugins/docker-compose ]; then
        COMPOSE_PLUGIN_PATH=/usr/libexec/docker/cli-plugins/docker-compose
    fi
    if [ -z "${COMPOSE_PLUGIN_PATH}" ] && [ -x /usr/local/lib/docker/cli-plugins/docker-compose ]; then
        COMPOSE_PLUGIN_PATH=/usr/local/lib/docker/cli-plugins/docker-compose
    fi
    [ -n "${COMPOSE_PLUGIN_PATH}" ] && [ -x "${COMPOSE_PLUGIN_PATH}" ] || {
        echo "Error: Docker Compose v2 binary is required to package the modeling release" >&2
        exit 1
    }
    cp "${COMPOSE_PLUGIN_PATH}" "${DEPLOY_DIR}/bin/docker-compose"
    chmod 0755 "${DEPLOY_DIR}/bin/docker-compose"

    cat > "${BUNDLE_DIR}/.env.example" <<'EOF_ENV'
MODELING_SERVICE_TOKEN=
MODELING_MCP_TOKEN_SECRET=
MODELING_PORT=5100
MODELING_MCP_PORT=5188
MODELING_LLM_BASE_URL=
MODELING_LLM_MODEL=
MODELING_LLM_API_KEY=
MODELING_TASK_LLM_BASE_URL=
MODELING_TASK_LLM_MODEL=
MODELING_TASK_LLM_API_KEY=
AUTH_ISSUER_URL=http://auth-server:5056
AUTH_JWT_ISSUER=http://auth-server:5056
AUTH_JWT_AUDIENCE=meta-wise
CONSUMER_API_URL=http://backend:5055
EOF_ENV

    cat > "${DEPLOY_DIR}/manifest.json" <<EOF_MANIFEST
{
  "schema_version": "2",
  "app_version": "${VERSION}",
  "release_arch": "${RELEASE_ARCH}",
  "bash_min_version": "3.2",
  "docker_min_version": "20.10",
  "modeling_image": "mw-modeling:${VERSION}",
  "modeling_image_tar": "images/mw-modeling-${VERSION}.tar",
  "modeling_agent_image": "mw-modeling-agent:${VERSION}",
  "modeling_agent_image_tar": "images/mw-modeling-agent-${VERSION}.tar"
}
EOF_MANIFEST
    printf '%s\n' "${VERSION}" > "${BUNDLE_DIR}/VERSION"

    find "${BUNDLE_DIR}" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
    find "${BUNDLE_DIR}" -type f -name '*.sh' -exec chmod +x {} +
    (
        cd "${BUNDLE_DIR}"
        find VERSION .env.example deploy images -type f -print0 \
            | sort -z | xargs -0 sha256sum > checksums.txt
    )

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
