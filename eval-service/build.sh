#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:?Usage: build.sh <version> [amd|arm]}"
[[ "$VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo 'Invalid version' >&2; exit 1; }
case "${2:-amd}" in amd) ARCH=amd64;; arm) ARCH=arm64;; *) echo 'Expected amd or arm' >&2; exit 1;; esac
OUT="${RELEASE_BUNDLE_DIR:+$RELEASE_BUNDLE_DIR/eval-service}"
OUT="${OUT:-$ROOT/dist/evaluation-$VERSION-$ARCH}"
[ ! -e "$OUT" ] || { echo 'Output already exists' >&2; exit 1; }
mkdir -p "$OUT/deploy"
docker build --platform "linux/$ARCH" -t "knowledge-evaluation:$VERSION" "$ROOT"
docker save -o "$OUT/evaluation-image.tar" "knowledge-evaluation:$VERSION"
cp "$ROOT/deploy/deploy.sh" "$ROOT/deploy/compose.yml" "$ROOT/deploy/nginx.conf.example" "$OUT/deploy/"
printf 'knowledge-evaluation:%s\n' "$VERSION" > "$OUT/IMAGE"
cp "$ROOT/README.md" "$ROOT/.env.example" "$OUT/"
printf 'Export EVAL_IMAGE=knowledge-evaluation:%s before deployment.\n' "$VERSION" > "$OUT/VERSION"
printf 'Release prepared in %s\n' "$OUT"
