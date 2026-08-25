#!/usr/bin/env bash
# Meta Wise 全栈编排脚本：consumer + 必备 auth/JiuwenBox + Modeling Workbench + 可选评测库。
# 面向自动化执行设计：每步输出机器可判读的 [OK]/[FAIL]/[WARN]，失败即退出并给出修复指令。
# 用法见 deploy/README.md；快速参考：./deploy-stack.sh help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONSUMER_DIR="$ROOT/consumer"
DEPLOY_DIR="$ROOT/deploy"
MODELING_PROJECT="mw-modeling"          # modeling compose 的项目名（卷名前缀）
TAG="${TAG:-latest}"

log()  { echo "[deploy-stack] $*"; }
ok()   { echo "[deploy-stack] [OK] $*"; }
warn() { echo "[deploy-stack] [WARN] $*"; }
fail() { echo "[deploy-stack] [FAIL] $*" >&2; exit "${2:-1}"; }

# ---- docker compose 探测（v2 插件优先，回退 docker-compose） ----
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    fail "未找到 docker compose 或 docker-compose，请先安装 Docker Compose" 2
  fi
}

consumer_compose() {
  compose -f "$CONSUMER_DIR/docker-compose.yml" --project-directory "$CONSUMER_DIR" "$@"
}

consumer_container() {
  if docker inspect onto-agent-backend >/dev/null 2>&1; then
    printf 'onto-agent-backend\n'
    return 0
  fi
  docker ps --format '{{.Names}}' | grep -E '^onto-agent-backend($|-)|^backend$' | head -1 || true
}

container_network() {
  docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$1" \
    2>/dev/null | head -1 || true
}

container_mount_volume() {
  local container="$1" destination="$2"
  docker inspect --format '{{range .Mounts}}{{if eq .Destination "'"${destination}"'"}}{{println .Name}}{{end}}{{end}}' \
    "$container" 2>/dev/null | head -1 || true
}

detect_consumer_network() {
  local container network
  container="$(consumer_container)"
  if [ -n "$container" ]; then
    network="$(container_network "$container")"
    [ -z "$network" ] || { printf '%s\n' "$network"; return 0; }
  fi
  docker network ls --format '{{.Name}}' | grep -E '(^|_|-)onto-agent-network$|^onto-agent_default$' | head -1 || true
}

detect_jiuwenbox_socket_volume() {
  local container volume
  container="$(consumer_container)"
  if [ -n "$container" ]; then
    volume="$(container_mount_volume "$container" /run/jiuwenbox)"
    [ -z "$volume" ] || { printf '%s\n' "$volume"; return 0; }
  fi
  if docker inspect mw-modeling >/dev/null 2>&1; then
    volume="$(container_mount_volume mw-modeling /run/jiuwenbox)"
    [ -z "$volume" ] || { printf '%s\n' "$volume"; return 0; }
  fi
  docker volume ls --format '{{.Name}}' | grep -E '(^|_)onto-jiuwenbox-sock$' | head -1 || true
}

modeling_compose() {
  local socket_volume
  # logs/down 也必须先通过 Compose 插值；启动路径会单独校验该卷真实存在。
  socket_volume="${JIUWENBOX_SOCKET_VOLUME:-$(detect_jiuwenbox_socket_volume)}"
  socket_volume="${socket_volume:-unavailable-jiuwenbox-socket}"
  JIUWENBOX_SOCKET_VOLUME="$socket_volume" compose -p "$MODELING_PROJECT" \
    -f "$DEPLOY_DIR/docker-compose.modeling.yml" --project-directory "$DEPLOY_DIR" "$@"
}

# ---- HTTP 健康等待：wait_http <名称> <url> <超时秒> <logs服务名> ----
wait_http() {
  local name="$1" url="$2" timeout="${3:-120}" svc="${4:-}" waited=0
  while ! curl -fsS --max-time 5 "$url" >/dev/null 2>&1; do
    waited=$((waited + 5))
    if [ "$waited" -ge "$timeout" ]; then
      fail "$name 健康检查超时（${timeout}s）：curl $url 不通。排查：$DEPLOY_DIR/deploy-stack.sh logs ${svc:-$name}"
    fi
    sleep 5
  done
  ok "$name 健康：$url"
}

port_free() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    ! lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null
  fi
}

env_get() { # env_get <file> <key> → 值（无则空）；按 compose env_file 语义清洗 CRLF/引号/行内注释
  local v
  v="$([ -f "$1" ] && grep -E "^${2}=" "$1" | tail -1 | cut -d= -f2- || true)"
  v="${v%$'\r'}"
  printf '%s' "$v" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

load_env_if_unset() {
  local file="$1" key="$2" value
  [ -z "${!key:-}" ] || return 0
  value="$(env_get "$file" "$key")"
  [ -z "$value" ] || { printf -v "$key" '%s' "$value"; export "$key"; }
}

ensure_mcp_token_secret() {
  local env_file="$CONSUMER_DIR/backend/.env" secret temp_file
  secret="$(env_get "$env_file" MCP_TOKEN_SECRET)"
  if [ -n "$secret" ]; then
    [ "${#secret}" -ge 32 ] || fail \
      "backend/.env 中 MCP_TOKEN_SECRET 少于 32 个字符；请改为独立强随机值" 2
    chmod 600 "$env_file"
    ok "MCP_TOKEN_SECRET 已配置"
    return 0
  fi
  if command -v openssl >/dev/null 2>&1; then
    secret="$(openssl rand -hex 32)"
  elif command -v python3 >/dev/null 2>&1; then
    secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  else
    fail "无法生成 MCP_TOKEN_SECRET：需要 openssl 或 Python 3" 2
  fi
  [ "${#secret}" -eq 64 ] || fail "生成 MCP_TOKEN_SECRET 失败" 2
  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")" || fail "无法更新 $env_file" 2
  chmod 600 "$temp_file"
  if ! awk -v secret="$secret" '
    BEGIN { updated = 0 }
    /^MCP_TOKEN_SECRET=/ {
      if (!updated) { print "MCP_TOKEN_SECRET=" secret; updated = 1 }
      next
    }
    { print }
    END { if (!updated) print "MCP_TOKEN_SECRET=" secret }
  ' "$env_file" > "$temp_file"; then
    command rm -f -- "$temp_file"
    fail "无法更新 $env_file" 2
  fi
  mv "$temp_file" "$env_file"
  chmod 600 "$env_file"
  ok "已生成独立 MCP_TOKEN_SECRET 并安全写入 backend/.env"
}

ensure_jiuwenbox_image() {
  local image="onto-agent-jiuwenbox:${TAG}"
  if docker image inspect "$image" >/dev/null 2>&1; then
    ok "JiuwenBox 镜像已存在：$image"
    return 0
  fi
  [ -f "$CONSUMER_DIR/deploy/build_jiuwenbox.sh" ] || fail \
    "JiuwenBox 构建脚本不存在：$CONSUMER_DIR/deploy/build_jiuwenbox.sh" 2
  log "首次部署需要构建固定版本 JiuwenBox 镜像：$image"
  bash "$CONSUMER_DIR/deploy/build_jiuwenbox.sh" "$image"
  docker image inspect "$image" >/dev/null 2>&1 || fail "JiuwenBox 镜像构建后仍不可用：$image" 2
  ok "JiuwenBox 镜像已就绪：$image"
}

wait_for_container() {
  local name="$1" timeout="${2:-180}" logs_service="${3:-$1}" waited=0 status
  while [ "$waited" -lt "$timeout" ]; do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$name" 2>/dev/null || true)"
    case "$status" in
      healthy|running) ok "$name 容器状态：$status"; return 0 ;;
      unhealthy|exited|dead) fail "$name 容器状态：$status。排查：$DEPLOY_DIR/deploy-stack.sh logs $logs_service" ;;
    esac
    sleep 2
    waited=$((waited + 2))
  done
  fail "$name 健康检查超时（${timeout}s）。排查：$DEPLOY_DIR/deploy-stack.sh logs $logs_service"
}

# ================= 子命令 =================

cmd_preflight() {
  local rc=0
  command -v docker >/dev/null 2>&1 && ok "docker 可用：$(docker --version)" \
    || { echo "[deploy-stack] [FAIL] docker 未安装"; rc=2; }
  docker info >/dev/null 2>&1 && ok "docker daemon 运行中" \
    || { echo "[deploy-stack] [FAIL] docker daemon 未运行或无权限（尝试 sudo / 加入 docker 组）"; rc=2; }
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose 可用（v2）"
  elif command -v docker-compose >/dev/null 2>&1; then
    # 本脚本使用 compose profiles（--profile），docker-compose v1 需 ≥ 1.28
    local v1; v1="$(docker-compose version --short 2>/dev/null || echo 0)"
    case "$v1" in
      1.2[0-7].*|1.1*.*|1.[0-9].*|0*) echo "[deploy-stack] [FAIL] docker-compose $v1 过旧（profiles 需 ≥1.28），请升级到 Compose v2"; rc=2 ;;
      *) ok "docker-compose 可用（$v1）" ;;
    esac
  else
    echo "[deploy-stack] [FAIL] 缺少 docker compose"; rc=2
  fi
  for p in "${BACKEND_PORT:-5055}" "${FRONTEND_PORT:-8888}" "${MODELING_PORT:-5100}"; do
    if port_free "$p"; then ok "端口 $p 空闲"; else
      if curl -fsS --max-time 3 "http://127.0.0.1:$p/health" >/dev/null 2>&1; then
        warn "端口 $p 已被占用，且像是本系统已在运行（/health 可达）——重复 up 是安全的"
      else
        echo "[deploy-stack] [FAIL] 端口 $p 被其他进程占用（lsof -iTCP:$p -sTCP:LISTEN 查看）"; rc=2
      fi
    fi
  done
  port_free "${EVAL_MYSQL_PORT:-3306}" && ok "端口 ${EVAL_MYSQL_PORT:-3306} 空闲（--with-eval 需要）" \
    || warn "端口 ${EVAL_MYSQL_PORT:-3306} 被占用；仅在使用 --with-eval 时需要处理"
  if [ -f "$CONSUMER_DIR/backend/.env" ]; then
    ok "consumer/backend/.env 已存在"
    local modeling_base_url modeling_model
    modeling_base_url="${MODELING_LLM_BASE_URL:-$(env_get "$CONSUMER_DIR/backend/.env" MODELING_LLM_BASE_URL)}"
    modeling_model="${MODELING_LLM_MODEL:-$(env_get "$CONSUMER_DIR/backend/.env" MODELING_LLM_MODEL)}"
    if { [[ "$modeling_base_url" == http://* ]] || [[ "$modeling_base_url" == https://* ]]; } && \
       [ -n "$modeling_model" ]; then
      ok "Modeling Workbench Agent 模型已配置"
    else
      echo "[deploy-stack] [FAIL] backend/.env 缺少 MODELING_LLM_BASE_URL 或 MODELING_LLM_MODEL"
      rc=2
    fi
  else
    warn "consumer/backend/.env 不存在（up 时会从 .env.example 自动创建）"
  fi
  [ "$rc" -eq 0 ] && ok "preflight 全部通过" || echo "[deploy-stack] preflight 存在 FAIL 项，请先处理" >&2
  return "$rc"
}

cmd_up() {
  local with_eval=0 consumer_only=0 modeling_only=0
  command -v curl >/dev/null 2>&1 || fail "缺少 curl，无法执行部署健康检查" 2
  while [ $# -gt 0 ]; do
    case "$1" in
      --with-auth) ;; # 兼容旧命令；auth-server 现在是必备组件并始终部署。
      --with-eval) with_eval=1 ;;
      --consumer-only) consumer_only=1 ;;
      --modeling-only) modeling_only=1 ;;
      --tag) [ $# -ge 2 ] && [ "${2#--}" = "$2" ] || fail "--tag 需要一个值（例：--tag latest）" 2; TAG="$2"; shift ;;
      *) fail "未知参数：$1（可用：--with-auth --with-eval --consumer-only --modeling-only --tag <tag>）" 2 ;;
    esac
    shift
  done
  export TAG

  if [ ! -f "$CONSUMER_DIR/backend/.env" ]; then
    cp "$CONSUMER_DIR/backend/.env.example" "$CONSUMER_DIR/backend/.env"
    chmod 600 "$CONSUMER_DIR/backend/.env"
    ok "已从 .env.example 创建 consumer/backend/.env，请按报错提示补齐必填配置后重跑"
  fi
  local url="" token="" bootstrap_password="" config_errors=0
  if [ "$modeling_only" -eq 0 ]; then
    url="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_SERVER_URL)"
    token="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_SERVER_ADMIN_TOKEN)"
    bootstrap_password="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_BOOTSTRAP_ADMIN_PASSWORD)"
    if [ -z "$url" ] || [ "${#token}" -lt 16 ] || \
       { [ ! -f "$CONSUMER_DIR/.onto-auth/users.json" ] && [ -z "$bootstrap_password" ]; }; then
      echo "[deploy-stack] [FAIL] auth-server 配置不完整：AUTH_SERVER_URL、至少 16 字符的 AUTH_SERVER_ADMIN_TOKEN，以及首次部署的 AUTH_BOOTSTRAP_ADMIN_PASSWORD" >&2
      config_errors=1
    fi
  fi
  if [ "$consumer_only" -eq 0 ]; then
    for key in MODELING_LLM_BASE_URL MODELING_LLM_MODEL MODELING_LLM_API_KEY \
               MODELING_LLM_MAX_TOKENS MODELING_LLM_CONTEXT_WINDOW \
               MODELING_AGENT_LLM_MAX_TOKENS MODELING_AGENT_MAX_ITERATIONS; do
      load_env_if_unset "$CONSUMER_DIR/backend/.env" "$key"
    done
    case "${MODELING_LLM_BASE_URL:-}" in
      http://*|https://*) ;;
      *)
        echo "[deploy-stack] [FAIL] Workbench Agent 配置不完整：MODELING_LLM_BASE_URL 必须是真实的 http(s) OpenAI-compatible 地址，且 MODELING_LLM_MODEL 不能为空" >&2
        config_errors=1
        ;;
    esac
    if [ -z "${MODELING_LLM_MODEL:-}" ]; then
      echo "[deploy-stack] [FAIL] Workbench Agent 配置不完整：MODELING_LLM_MODEL 不能为空" >&2
      config_errors=1
    fi
  fi
  [ "$config_errors" -eq 0 ] || fail "请补齐 consumer/backend/.env 后重新执行 ./deploy/deploy.sh" 2

  # ---- consumer ----
  if [ "$modeling_only" -eq 0 ]; then
    mkdir -p "$CONSUMER_DIR/backend/config" "$CONSUMER_DIR/.onto-runtime/config" \
             "$CONSUMER_DIR/backend/logs" "$CONSUMER_DIR/frontend/logs/nginx"

    export AUTH_SERVER_ADMIN_TOKEN="$token"
    ok "auth-server 已启用（敏感配置从 backend/.env 注入，值不写入日志）"

    ensure_mcp_token_secret
    ensure_jiuwenbox_image
    log "启动 consumer（TAG=$TAG）…"
    consumer_compose up -d --build
    wait_for_container "onto-agent-auth-server-${TAG}" 180 auth-server
    wait_for_container "onto-agent-jiuwenbox-${TAG}" 180 jiuwenbox
    wait_for_container "onto-agent-backend-${TAG}" 180 backend
    wait_for_container "onto-agent-mcp-${TAG}" 180 mcp
    wait_for_container "onto-agent-frontend-${TAG}" 180 frontend
    wait_http "consumer-backend" "http://127.0.0.1:${BACKEND_PORT:-5055}/health" 180 backend
    wait_http "consumer-frontend" "http://127.0.0.1:${FRONTEND_PORT:-8888}/" 120 frontend
  fi

  # ---- modeling ----
  if [ "$consumer_only" -eq 0 ]; then
    if [ -n "${CONSUMER_NETWORK:-}" ]; then
      ok "使用用户指定的 consumer 网络：$CONSUMER_NETWORK"
    else
      local net; net="$(detect_consumer_network)"
      [ -n "$net" ] || fail "未找到 consumer 网络（*onto-agent-network）。请先启动 consumer：./deploy-stack.sh up --consumer-only；或 export CONSUMER_NETWORK=<实际网络名> 后重跑"
      export CONSUMER_NETWORK="$net"
      ok "使用 consumer 网络：$net"
    fi

    local socket_volume
    socket_volume="${JIUWENBOX_SOCKET_VOLUME:-$(detect_jiuwenbox_socket_volume)}"
    [ -n "$socket_volume" ] || fail \
      "未找到 JiuwenBox socket 卷。请先部署 consumer，或设置 JIUWENBOX_SOCKET_VOLUME=<卷名>" 2
    export JIUWENBOX_SOCKET_VOLUME="$socket_volume"
    ok "使用 JiuwenBox socket 卷：$socket_volume"

    local eval_args=()
    if [ "$with_eval" -eq 1 ]; then
      [ -n "${EVAL_MYSQL_ROOT_PASSWORD:-}" ] || fail \
        "--with-eval 要求显式设置 EVAL_MYSQL_ROOT_PASSWORD；请使用独立强随机值，禁止沿用示例或生产数据库密码" 2
      eval_args=(--profile eval)
      export EVAL_DB_URL="${EVAL_DB_URL:-mysql://root:${EVAL_MYSQL_ROOT_PASSWORD}@eval-mysql:3306}"
      export MODELING_EVAL_BINDING_URL="${MODELING_EVAL_BINDING_URL:-http://modeling-nl2sql:5600}"
      # 打印时掩码密码段，避免凭据进入终端/CI 日志
      ok "评测库已启用：EVAL_DB_URL=$(printf '%s' "$EVAL_DB_URL" | sed -E 's#://([^:/@]+):[^@]*@#://\1:***@#')"
    fi

    log "启动 Modeling Workbench（core + Agent）…"
    modeling_compose "${eval_args[@]+"${eval_args[@]}"}" up -d --build
    wait_for_container "mw-modeling-agent" 180
    wait_http "modeling" "http://127.0.0.1:${MODELING_PORT:-5100}/health" 120 modeling
    wait_http "modeling-workbench" "http://127.0.0.1:${MODELING_PORT:-5100}/workbench.html" 60 modeling
  fi

  echo
  ok "部署完成。访问入口："
  [ "$modeling_only" -eq 0 ] && log "  控制台（consumer）  http://localhost:${FRONTEND_PORT:-8888}   初始账号 admin（密码取 AUTH_BOOTSTRAP_ADMIN_PASSWORD）"
  [ "$consumer_only" -eq 0 ] && log "  建模工作台          http://localhost:${MODELING_PORT:-5100}/workbench.html#/create   用 consumer 账号登录"
  log "后续初始化步骤见 deploy/README.md 第 4 节。"
}

cmd_status() {
  log "容器状态："
  docker ps -a --filter name=onto-agent --filter name=mw-modeling --filter name=mw-eval-mysql \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo
  for pair in "consumer-backend|http://127.0.0.1:${BACKEND_PORT:-5055}/health" \
              "consumer-frontend|http://127.0.0.1:${FRONTEND_PORT:-8888}/" \
              "modeling|http://127.0.0.1:${MODELING_PORT:-5100}/health" \
              "modeling-workbench|http://127.0.0.1:${MODELING_PORT:-5100}/workbench.html"; do
    local_name="${pair%%|*}"; local_url="${pair##*|}"
    if curl -fsS --max-time 5 "$local_url" >/dev/null 2>&1; then
      ok "$local_name 可达：$local_url"
    else
      warn "$local_name 不可达：$local_url"
    fi
  done
}

cmd_down() {
  local vol_args=()
  [ "${1:-}" = "--volumes" ] && vol_args=(--volumes) && warn "将同时删除数据卷（建模项目数据、评测库数据不可恢复）"
  modeling_compose --profile eval down "${vol_args[@]+"${vol_args[@]}"}" 2>/dev/null || true
  consumer_compose --profile auth down "${vol_args[@]+"${vol_args[@]}"}"
  ok "已停止"
}

cmd_logs() {
  local svc="${1:-}"
  case "$svc" in
    backend|frontend|mcp|auth-server|jiuwenbox) consumer_compose logs -f --tail 200 "$svc" ;;
    modeling|modeling-agent|modeling-nl2sql|eval-mysql) modeling_compose logs -f --tail 200 "$svc" ;;
    *) fail "用法：./deploy-stack.sh logs <backend|frontend|mcp|auth-server|jiuwenbox|modeling|modeling-agent|modeling-nl2sql|eval-mysql>" 2 ;;
  esac
}

cmd_help() {
  cat <<'EOF'
用法：./deploy-stack.sh <command> [options]

  preflight                     环境检查（docker/compose/端口），全部 [OK] 才继续
  up [options]                  构建并启动全栈（幂等，可重复执行）
      --with-auth               兼容旧命令；auth-server 现在默认且始终启用
      --with-eval               附带评测库 MySQL（建模评测闭环）
      --consumer-only           只部署运行期 consumer
      --modeling-only           只部署建模工作台（要求 consumer 已在运行）
      --tag <tag>               consumer 镜像 tag（默认 latest）
  status                        容器状态 + 健康检查
  down [--volumes]              停止（--volumes 同时删数据卷，慎用）
  logs <service>                跟踪日志：backend|frontend|mcp|auth-server|jiuwenbox|modeling|modeling-agent|eval-mysql

环境变量（可选覆盖）：BACKEND_PORT=5055 FRONTEND_PORT=8888 MODELING_PORT=5100
  EVAL_MYSQL_PORT=3306 EVAL_MYSQL_ROOT_PASSWORD=<启用评测库时必填的强随机值>
  MODELING_LLM_BASE_URL/MODEL（Workbench Agent 必填；优先读取 shell，其次 backend/.env）
  MODELING_LLM_API_KEY（端点需要鉴权时必填）
完整手册：deploy/README.md
EOF
}

case "${1:-help}" in
  preflight) shift; cmd_preflight "$@" ;;
  up)        shift; cmd_up "$@" ;;
  status)    shift; cmd_status "$@" ;;
  down)      shift; cmd_down "$@" ;;
  logs)      shift; cmd_logs "$@" ;;
  help|-h|--help) cmd_help ;;
  *) fail "未知命令：$1（./deploy-stack.sh help 查看用法）" 2 ;;
esac
