#!/usr/bin/env bash
# Manage the kb-suite stack with Docker Compose.
#
#   ./kbsuite.sh build      build kb-service, kb-gateway + adapters, plugin frontend, docreader and Swarm (develop; SWARM_TAG=0.2.7 for PyPI)
#   ./kbsuite.sh build-kbs         kb-service image only
#   ./kbsuite.sh build-plugin      plugin pages only (frontend/dist, picked up on the next swarm restart)
#   ./kbsuite.sh build-docreader   docreader overlay from $WEKNORA_REPO@$WEKNORA_REF
#   ./kbsuite.sh build-da          DA-java from core/runtime develop + solution common
#   ./kbsuite.sh build-lf          Loop Forest service (needs build-da's Maven artifacts)
#   ./kbsuite.sh build-swarm       plugin pages + Swarm 0.2.7 image (with the DA harness provider)
#   ./kbsuite.sh build-swarm-dev   plugin pages + Swarm image from $SWARM_REPO@$SWARM_REF (develop)
#   ./kbsuite.sh build-gateway     kb-gateway and the WeKnora / DA knowledge-pipeline adapters
#   ./kbsuite.sh build-da-kb       DA knowledge pipeline image from $DA_TS_SRC (a DeepAnalyze source tree)
#   ./kbsuite.sh build-weknora-config   WeKnora config.yaml from $WEKNORA_REPO@$WEKNORA_REF
#   ./kbsuite.sh up         start everything (bootstraps keys on first run)
#   ./kbsuite.sh up-weknora        start the WeKnora backend and set it up for the adapter
#   ./kbsuite.sh up-da-kb          start the DA knowledge-pipeline backend and point it at the models
#   ./kbsuite.sh status | logs [service] | restart [service] | down
#
# KBS_PROJECT (default kb-suite) and the *_PORT variables in compose.yaml run a
# second stack next to the first; give it its own KBS_SECRETS_DIR as well.
#
# Secrets live in $KBS_SECRETS_DIR (default ~/.config/kb-suite), never in git:
#   secrets.env  KBS_PG_PASSWORD, KBS_LLM_BASE_URL, KBS_LLM_API_KEY, KBS_RERANK_URL,
#                SWARM_API_BASE, SWARM_API_KEY, DA_ENGINE_SECRET
#   keys.env     written by `bootstrap`: KBS_PLUGIN_KEY, KBS_MCP_KEY, KBS_DA_KEY
#   gateway.env  generated: KBGW_WEKNORA_ADAPTER_KEY, KBGW_DA_ADAPTER_KEY (gateway -> adapters)
#   weknora.env  generated passwords; `up-weknora` adds WEKNORA_API_KEY and the model ids
#   da-kb.env    generated: DA_KB_DB_PASSWORD
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export KBS_SECRETS_DIR="${KBS_SECRETS_DIR:-$HOME/.config/kb-suite}"
SECRETS="$KBS_SECRETS_DIR/secrets.env"
KEYS="$KBS_SECRETS_DIR/keys.env"

PROJECT="${KBS_PROJECT:-kb-suite}"
export KBS_PROJECT="$PROJECT"
ENV_FILES=(secrets.env gateway.env weknora.env da-kb.env)

compose() {
  local args=() name
  for name in "${ENV_FILES[@]}"; do
    [ -f "$KBS_SECRETS_DIR/$name" ] && args+=(--env-file "$KBS_SECRETS_DIR/$name")
  done
  docker compose -p "$PROJECT" "${args[@]}" -f "$HERE/compose.yaml" "$@"
}
compose_all() { compose --profile weknora --profile da-kb "$@"; }

# gen_env FILE VAR... : add a random value for each VAR not yet in FILE (mode 600)
gen_env() {
  local file="$KBS_SECRETS_DIR/$1" var
  shift
  touch "$file" && chmod 600 "$file"
  for var in "$@"; do
    grep -q "^$var=" "$file" || echo "$var=$(openssl rand -hex 16)" >>"$file"
  done
}

# env_export FILE VAR... : export each VAR that FILE sets, read the way Compose reads an
# env file rather than run as shell (values may contain $, &, ; or spaces): optional
# "export " prefix, quoted values taken inside the quotes, " #" starts a comment otherwise.
env_export() {
  local file="$1" var line
  shift
  for var in "$@"; do
    line="$(tr -d '\r' <"$file" | grep -E "^(export[[:space:]]+)?$var=" | tail -n 1)" || continue
    line="${line#*=}"
    case "$line" in
      \"*) line="${line#\"}"; line="${line%%\"*}" ;;
      \'*) line="${line#\'}"; line="${line%%\'*}" ;;
      *) line="${line%% #*}"; line="${line%"${line##*[![:space:]]}"}" ;;
    esac
    export "$var=$line"
  done
}

require_secrets() {
  [ -f "$SECRETS" ] || { echo "missing $SECRETS (see deploy/compose/README.md)" >&2; exit 1; }
}

wait_healthy() {
  local svc="$1" i
  for i in $(seq 1 60); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "$PROJECT-$svc-1" 2>/dev/null)" = "healthy" ]; then
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
    python -m kb_service.cli bootstrap --tenant "${DA_TENANT_ID:-default}" --out /secrets/keys.env
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

build_gateway() {
  docker build -t kb-suite/kb-gateway:"${KBGW_TAG:-dev}" "$ROOT/service/kb-gateway"
  local adapter
  for adapter in weknora-adapter da-kb-adapter; do
    docker build -t kb-suite/"$adapter":"${KBGW_TAG:-dev}" --build-context provider-kit="$ROOT/service/provider-kit" \
      --build-context kb-service="$ROOT/service/kb-service" "$ROOT/service/$adapter"
  done
}

# DeepAnalyze's source is not in this repository; build from a checkout given by DA_TS_SRC.
build_da_kb() {
  local src="${DA_TS_SRC:?set DA_TS_SRC to a DeepAnalyze source tree}"
  docker build -f "$HERE/da-kb/Dockerfile" -t kb-suite/da-kb:"${DA_KB_TAG:-current}" "$src"
}

build_weknora_config() {
  local repo="${WEKNORA_REPO:?set WEKNORA_REPO to a WeKnora clone}" ref="${WEKNORA_REF:-HEAD}"
  mkdir -p "$HERE/weknora/build"
  git -C "$repo" show "$ref:config/config.yaml" >"$HERE/weknora/build/config.yaml"
}

up_weknora() {
  require_secrets
  [ -f "$HERE/weknora/build/config.yaml" ] || { echo "run ./kbsuite.sh build-weknora-config first" >&2; exit 1; }
  gen_env gateway.env KBGW_WEKNORA_ADAPTER_KEY KBGW_DA_ADAPTER_KEY
  gen_env weknora.env WEKNORA_DB_PASSWORD WEKNORA_REDIS_PASSWORD WEKNORA_AES_KEY WEKNORA_JWT_SECRET WEKNORA_ADMIN_PASSWORD
  compose --profile weknora up -d weknora-postgres weknora-redis weknora-docreader weknora-app
  wait_healthy weknora-app
  (
    env_export "$SECRETS" KBS_LLM_BASE_URL KBS_LLM_API_KEY
    env_export "$KBS_SECRETS_DIR/weknora.env" WEKNORA_ADMIN_PASSWORD
    compose run --rm --no-deps --user "$(id -u):$(id -g)" --entrypoint python \
      -v "$HERE/weknora/bootstrap.py:/bootstrap.py:ro" -v "$KBS_SECRETS_DIR:/secrets" \
      -e WEKNORA_URL=http://weknora-app:8080 -e WEKNORA_ADMIN_EMAIL=kbgw-owner@example.com \
      -e WEKNORA_ADMIN_PASSWORD -e KBS_LLM_BASE_URL -e KBS_LLM_API_KEY \
      kb-gateway /bootstrap.py /secrets/weknora.env
  )
  # the bootstrap account exists now; close self-registration
  grep -q '^WEKNORA_DISABLE_REGISTRATION=' "$KBS_SECRETS_DIR/weknora.env" ||
    echo "WEKNORA_DISABLE_REGISTRATION=true" >>"$KBS_SECRETS_DIR/weknora.env"
  compose --profile weknora up -d weknora-app weknora-adapter
  compose up -d --force-recreate kb-gateway
}

up_da_kb() {
  require_secrets
  docker image inspect kb-suite/da-kb:"${DA_KB_TAG:-current}" >/dev/null 2>&1 ||
    { echo "DA knowledge pipeline image missing (run ./kbsuite.sh build-da-kb)" >&2; exit 1; }
  gen_env gateway.env KBGW_WEKNORA_ADAPTER_KEY KBGW_DA_ADAPTER_KEY
  gen_env da-kb.env DA_KB_DB_PASSWORD
  compose --profile da-kb up -d da-kb-postgres da-kb
  local psql=(compose --profile da-kb exec -T da-kb-postgres psql -U deepanalyze -d deepanalyze -v ON_ERROR_STOP=1 -tA)
  local i
  for i in $(seq 1 60); do
    [ "$("${psql[@]}" -c "SELECT 1 FROM settings WHERE key = 'providers'" 2>/dev/null)" = 1 ] && break
    sleep 2
  done
  # DA reads its models only at start-up: write them, then restart
  (env_export "$SECRETS" KBS_LLM_BASE_URL KBS_LLM_API_KEY DA_KB_CHAT_MODEL DA_KB_EMBED_MODEL
   python3 "$HERE/da-kb/providers.py") | "${psql[@]}" >/dev/null
  compose --profile da-kb restart da-kb
  compose --profile da-kb up -d da-kb-adapter
  compose up -d --force-recreate kb-gateway
}

case "$cmd" in
  build)
    docker build -t kb-suite/kb-service:"${KBS_TAG:-dev}" "$ROOT/service/kb-service"
    build_gateway
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
    gen_env gateway.env KBGW_WEKNORA_ADAPTER_KEY KBGW_DA_ADAPTER_KEY
    compose up -d kb-gateway
    wait_healthy kb-gateway
    compose up -d swarm edge
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
  build-gateway) build_gateway ;;
  build-da-kb) build_da_kb ;;
  build-weknora-config) build_weknora_config ;;
  up-weknora) up_weknora ;;
  up-da-kb) up_da_kb ;;
  status) compose_all ps ;;
  logs) compose_all logs -f --tail=200 "$@" ;;
  restart) compose_all restart "$@" ;;
  down) compose_all down ;;
  *) sed -n '2,24p' "$0"; exit 1 ;;
esac
