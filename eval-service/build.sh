#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:?Usage: build.sh <version> [amd|arm]}"
[[ "$VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo 'Invalid version' >&2; exit 1; }
case "${2:-amd}" in amd) ARCH=amd64;; arm) ARCH=arm64;; *) echo 'Expected amd or arm' >&2; exit 1;; esac
# 完整离线包内是 eval-service 目录；单独构建打成 evaluation-<版本>-linux-<架构>.tar.gz，顶层目录同为 eval-service
PACKAGE=''
if [ -n "${RELEASE_BUNDLE_DIR:-}" ]; then
    OUT="$RELEASE_BUNDLE_DIR/eval-service"
else
    PACKAGE="$ROOT/dist/evaluation-$VERSION-linux-$ARCH.tar.gz"
    [ ! -e "$PACKAGE" ] || { echo 'Output already exists' >&2; exit 1; }
    STAGE="$ROOT/dist/release-$VERSION-$ARCH"
    rm -rf "$STAGE"
    trap 'rm -rf "$STAGE"' EXIT
    OUT="$STAGE/eval-service"
fi
[ ! -e "$OUT" ] || { echo 'Output already exists' >&2; exit 1; }
mkdir -p "$OUT/deploy"
# 镜像源默认见 Dockerfile（国内镜像）；显式配置时覆盖，企业内网可指向自有镜像
BUILD_ARGS=()
PIP_INDEX="${EVAL_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
[ -z "$PIP_INDEX" ] || BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX")
NPM_MIRROR="${EVAL_NPM_REGISTRY:-${NPM_REGISTRY:-}}"
[ -z "$NPM_MIRROR" ] || BUILD_ARGS+=(--build-arg "NPM_REGISTRY=$NPM_MIRROR")
docker build --platform "linux/$ARCH" ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "knowledge-evaluation:$VERSION" "$ROOT"
docker save -o "$OUT/evaluation-image.tar" "knowledge-evaluation:$VERSION"
cp "$ROOT/deploy/deploy.sh" "$ROOT/deploy/docker-compose.eval-service.yml" "$OUT/deploy/"
printf 'knowledge-evaluation:%s\n' "$VERSION" > "$OUT/IMAGE"
cp "$ROOT/README.md" "$ROOT/.env.example" "$OUT/"
printf 'Export EVAL_IMAGE=knowledge-evaluation:%s before deployment.\n' "$VERSION" > "$OUT/VERSION"
if [ -n "$PACKAGE" ]; then
    tar -C "$STAGE" -czf "$PACKAGE" eval-service
    printf 'Release package created: %s\n' "$PACKAGE"
else
    printf 'Release prepared in %s\n' "$OUT"
fi
