#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DOCKERFILE="${REPOSITORY_ROOT}/solvers/ortools/Dockerfile"

VERSION="${1:-latest}"
ARCH="${2:-}"
IMAGE="${ORTOOLS_SOLVER_IMAGE:-solver-ortools-service:${VERSION}}"

if [ -n "${ARCH}" ]; then
    case "${ARCH}" in
        amd) PLATFORM="linux/amd64" ;;
        arm) PLATFORM="linux/arm64" ;;
        *) echo "invalid architecture: ${ARCH} (expected amd or arm)" >&2; exit 2 ;;
    esac
fi

if docker buildx version >/dev/null 2>&1; then
    if [ -n "${PLATFORM:-}" ]; then
        docker buildx build --platform "${PLATFORM}" --load \
            -t "${IMAGE}" -f "${DOCKERFILE}" "${REPOSITORY_ROOT}"
    else
        docker buildx build --load -t "${IMAGE}" \
            -f "${DOCKERFILE}" "${REPOSITORY_ROOT}"
    fi
else
    [ -z "${PLATFORM:-}" ] || {
        echo "docker buildx is required when an explicit architecture is requested" >&2
        exit 2
    }
    docker build -t "${IMAGE}" -f "${DOCKERFILE}" "${REPOSITORY_ROOT}"
fi

echo "Built independent OR-Tools Solver Service image: ${IMAGE}"
