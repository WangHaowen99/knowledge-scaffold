#!/usr/bin/env bash
# Manage the kb-suite stack with Docker Compose.
#
#   ./kbsuite.sh build      build kb-service, plugin frontend, docreader and Swarm (develop; SWARM_TAG=0.2.7 for PyPI)
#   ./kbsuite.sh build-kbs         kb-service image only
#   ./kbsuite.sh build-plugin      plugin pages only (frontend/dist, picked up on the next swarm restart)
#   ./kbsuite.sh build-docreader   docreader overlay from $WEKNORA_REPO@$WEKNORA_REF
#   ./kbsuite.sh build-da          DA-java from core/runtime develop + solution common
#   ./kbsuite.sh build-lf          Loop Forest service (needs build-da's Maven artifacts)
#   ./kbsuite.sh build-swarm       plugin pages + Swarm 0.2.7 image (with the DA harness provider)
#   ./kbsuite.sh build-swarm-dev   plugin pages + Swarm image from $SWARM_REPO@$SWARM_REF (develop)
#   ./kbsuite.sh up         start everything (bootstraps keys on first run)
#   ./kbsuite.sh status | logs [service] | restart [service] | down
#
# Secrets live in $KBS_SECRETS_DIR (default ~/.config/kb-suite), never in git:
#   secrets.env  KBS_PG_PASSWORD, KBS_LLM_BASE_URL, KBS_LLM_API_KEY, KBS_RERANK_URL,
#                SWARM_API_BASE, SWARM_API_KEY, DA_ENGINE_SECRET
#   keys.env     written by `bootstrap`: KBS_PLUGIN_KEY, KBS_MCP_KEY, KBS_DA_KEY
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export KBS_SECRETS_DIR="${KBS_SECRETS_DIR:-$HOME/.config/kb-suite}"
SECRETS="$KBS_SECRETS_DIR/secrets.env"
KEYS="$KBS_SECRETS_DIR/keys.env"

compose() { docker compose -p kb-suite --env-file "$SECRETS" -f "$HERE/compose.yaml" "$@"; }

require_secrets() {
  [ -f "$SECRETS" ] || { echo "missing $SECRETS (see deploy/compose/README.md)" >&2; exit 1; }
}

wait_healthy() {
  local svc="$1" i
  for i in $(seq 1 60); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "kb-suite-$svc-1" 2>/dev/null)" = "healthy" ]; then
      return 0
    fi
    sleep 2
  done
  echo "$svc did not become healthy" >&2
  return 1
}

build_plugin() {
  # 一份前端工程出两个插件页面：kb-manager（知识库管理）和 da-analyst（知识求索）。
  docker run --rm -v "$ROOT/swarm-kb-plugin:/src" docker.1ms.run/library/node:22-slim sh -c "
    cp -r /src/frontend /work && cd /work &&
    npm ci --registry=https://registry.npmmirror.com --no-audit --no-fund --loglevel=error &&
    npx tsc --noEmit &&
    KB_APP=kb npx vite build --outDir /src/plugin/kb-manager/frontend/dist --emptyOutDir &&
    KB_APP=da npx vite build --outDir /src/plugin/da-analyst/frontend/dist --emptyOutDir &&
    chown -R $(id -u):$(id -g) /src/plugin/kb-manager/frontend /src/plugin/da-analyst/frontend"
}

build_docreader() {
  local repo="${WEKNORA_REPO:?set WEKNORA_REPO to a WeKnora clone}" ref="${WEKNORA_REF:-HEAD}"
  local base="${DOCREADER_BASE:-wechatopenai/weknora-docreader:latest}" ctx commit
  ctx="$(mktemp -d)"
  commit="$(git -C "$repo" rev-parse --short "$ref")"
  git -C "$repo" archive "$ref" docreader | tar -x -C "$ctx"
  # The overlay keeps the base image's virtualenv, so dependencies must match.
  docker run --rm --entrypoint cat "$base" /app/uv.lock | cmp -s - "$ctx/docreader/uv.lock" || {
    echo "uv.lock differs between $base and WeKnora $commit; rebuild docreader from source instead" >&2
    rm -rf "$ctx"; return 1; }
  cp "$HERE/docreader/Dockerfile" "$ctx/Dockerfile"
  docker build -t kb-suite/docreader:"$commit" -t kb-suite/docreader:current \
    --build-arg BASE_IMAGE="$base" --build-arg WEKNORA_COMMIT="$commit" "$ctx"
  rm -rf "$ctx"
}

bootstrap() {
  touch "$KEYS" && chmod 600 "$KEYS"
  compose run --rm --no-deps --user "$(id -u):$(id -g)" -v "$KBS_SECRETS_DIR:/secrets" kbs-api \
    python -m kb_service.cli bootstrap --tenant default --out /secrets/keys.env
}

cmd="${1:-}"
shift || true
build_swarm() {
  docker build -t kb-suite/swarm:0.2.7 \
    --build-context da-harness="$ROOT/harness/da-harness" "$HERE/swarm"
}

# 开发版 Swarm：只 fetch 再 git archive 导出 $SWARM_REPO@$SWARM_REF，不碰工作副本。
build_swarm_dev() {
  local repo="${SWARM_REPO:?set SWARM_REPO to a jiuwenswarm clone}" ref="${SWARM_REF:-origin/develop}" commit src
  git -C "$repo" fetch -q origin develop
  commit="$(git -C "$repo" rev-parse --short=9 "$ref")"
  src="$HERE/swarm/build/src-$commit"
  if [ ! -d "$src" ]; then
    mkdir -p "$src.tmp" && git -C "$repo" archive "$commit" | tar -x -C "$src.tmp" && mv "$src.tmp" "$src"
  fi
  docker build -f "$HERE/swarm/Dockerfile.dev" \
    --build-context swarm-src="$src" --build-context da-harness="$ROOT/harness/da-harness" \
    --build-context lf-harness="$ROOT/harness/lf-harness" \
    --label kb-suite.swarm-commit="$commit" \
    -t kb-suite/swarm:"dev-$commit" -t kb-suite/swarm:dev "$HERE/swarm"
}

case "$cmd" in
  build)
    docker build -t kb-suite/kb-service:"${KBS_TAG:-dev}" "$ROOT/service/kb-service"
    build_plugin
    build_docreader
    # compose defaults to the develop Swarm; SWARM_TAG=0.2.7 builds the PyPI one.
    if [ "${SWARM_TAG:-dev}" = 0.2.7 ]; then build_swarm; else build_swarm_dev; fi
    ;;
  build-kbs) docker build -t kb-suite/kb-service:"${KBS_TAG:-dev}" "$ROOT/service/kb-service" ;;
  build-plugin) build_plugin ;;
  build-swarm) build_plugin; build_swarm ;;
  build-swarm-dev) build_plugin; build_swarm_dev ;;
  up)
    require_secrets
    compose up -d kbs-postgres kbs-docreader kbs-api kbs-worker
    wait_healthy kbs-api
    if ! grep -q '^KBS_MCP_KEY=' "$KEYS" 2>/dev/null; then bootstrap; fi
    compose up -d swarm
    if docker image inspect kb-suite/deepanalyze-java:"${DA_TAG:-current}" >/dev/null 2>&1; then
      compose up -d kbs-redis da-engine
    else
      echo "DA image missing, skipped da-engine (run ./kbsuite.sh build-da)"
    fi
    if docker image inspect kb-suite/loop-forest:"${LF_TAG:-current}" >/dev/null 2>&1; then
      compose up -d lf-engine
    else
      echo "Loop Forest image missing, skipped lf-engine (run ./kbsuite.sh build-lf)"
    fi
    compose ps
    ;;
  bootstrap) require_secrets; bootstrap ;;
  build-docreader) build_docreader ;;
  build-da) "$HERE/da/build.sh" ;;
  build-lf) "$HERE/lf/build.sh" ;;
  status) compose ps ;;
  logs) compose logs -f --tail=200 "$@" ;;
  restart) compose restart "$@" ;;
  down) compose down ;;
  *) sed -n '2,16p' "$0"; exit 1 ;;
esac
