#!/usr/bin/env bash
# Meta Wise 全栈编排脚本：consumer（运行期）+ modeling（建模工作台）+ 可选 auth-server / 评测库。
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

modeling_compose() {
  compose -p "$MODELING_PROJECT" -f "$DEPLOY_DIR/docker-compose.modeling.yml" \
    --project-directory "$DEPLOY_DIR" "$@"
}

# ---- 探测 consumer 网络实际名（compose 项目名前缀不固定；无匹配返回空而非非零，避免 pipefail 静默退出） ----
detect_consumer_network() {
  docker network ls --format '{{.Name}}' | grep -E '(^|_|-)onto-agent-network$' | head -1 || true
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
  [ -f "$CONSUMER_DIR/backend/.env" ] && ok "consumer/backend/.env 已存在" \
    || warn "consumer/backend/.env 不存在（up 时会从 .env.example 自动创建）"
  [ "$rc" -eq 0 ] && ok "preflight 全部通过" || echo "[deploy-stack] preflight 存在 FAIL 项，请先处理" >&2
  return "$rc"
}

cmd_up() {
  local with_auth=0 with_eval=0 consumer_only=0 modeling_only=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --with-auth) with_auth=1 ;;
      --with-eval) with_eval=1 ;;
      --consumer-only) consumer_only=1 ;;
      --modeling-only) modeling_only=1 ;;
      --tag) [ $# -ge 2 ] && [ "${2#--}" = "$2" ] || fail "--tag 需要一个值（例：--tag latest）" 2; TAG="$2"; shift ;;
      *) fail "未知参数：$1（可用：--with-auth --with-eval --consumer-only --modeling-only --tag <tag>）" 2 ;;
    esac
    shift
  done
  export TAG

  # ---- consumer ----
  if [ "$modeling_only" -eq 0 ]; then
    if [ ! -f "$CONSUMER_DIR/backend/.env" ]; then
      cp "$CONSUMER_DIR/backend/.env.example" "$CONSUMER_DIR/backend/.env"
      ok "已从 .env.example 创建 consumer/backend/.env（LLM 可稍后在控制台系统设置页配置）"
    fi
    mkdir -p "$CONSUMER_DIR/backend/config" "$CONSUMER_DIR/.onto-runtime/config" \
             "$CONSUMER_DIR/backend/logs" "$CONSUMER_DIR/frontend/logs/nginx"

    local auth_args=()
    if [ "$with_auth" -eq 1 ]; then
      local mode url token bootstrap_password
      mode="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_MODE)"
      url="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_SERVER_URL)"
      token="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_SERVER_ADMIN_TOKEN)"
      bootstrap_password="$(env_get "$CONSUMER_DIR/backend/.env" AUTH_BOOTSTRAP_ADMIN_PASSWORD)"
      if [ "$mode" != "remote" ] || [ -z "$url" ] || [ -z "$token" ] || \
         { [ ! -f "$CONSUMER_DIR/.onto-auth/users.json" ] && [ -z "$bootstrap_password" ]; }; then
        fail "--with-auth 需要先在 consumer/backend/.env 配好以下参数（然后重跑本命令；已有 .onto-auth/users.json 时可省略最后一项）：
  AUTH_MODE=remote
  AUTH_SERVER_URL=http://auth-server:5056
  AUTH_SERVER_ADMIN_TOKEN=<自定义共享密钥>
  AUTH_BOOTSTRAP_ADMIN_PASSWORD=<首次启动使用的独立强密码>" 2
      fi
      # auth-server 容器经 compose 插值取该密钥（backend 经 env_file 各取各的），必须导出保证两边一致
      export AUTH_SERVER_ADMIN_TOKEN="$token"
      auth_args=(--profile auth)
      ok "auth-server 已启用（管理令牌与首次启动密码均从 backend/.env 注入，值不写入日志）"
    fi

    log "启动 consumer（TAG=$TAG）…"
    consumer_compose "${auth_args[@]+"${auth_args[@]}"}" up -d --build
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

    local eval_args=()
    if [ "$with_eval" -eq 1 ]; then
      [ -n "${EVAL_MYSQL_ROOT_PASSWORD:-}" ] || fail \
        "--with-eval 要求显式设置 EVAL_MYSQL_ROOT_PASSWORD；请使用独立强随机值，禁止沿用示例或生产数据库密码" 2
      eval_args=(--profile eval)
      export EVAL_DB_URL="${EVAL_DB_URL:-mysql://root:${EVAL_MYSQL_ROOT_PASSWORD}@eval-mysql:3306}"
      # 打印时掩码密码段，避免凭据进入终端/CI 日志
      ok "评测库已启用：EVAL_DB_URL=$(printf '%s' "$EVAL_DB_URL" | sed -E 's#://([^:/@]+):[^@]*@#://\1:***@#')"
    fi

    log "启动 modeling…"
    modeling_compose "${eval_args[@]+"${eval_args[@]}"}" up -d --build
    wait_http "modeling" "http://127.0.0.1:${MODELING_PORT:-5100}/health" 120 modeling
  fi

  echo
  ok "部署完成。访问入口："
  [ "$modeling_only" -eq 0 ] && log "  控制台（consumer）  http://localhost:${FRONTEND_PORT:-8888}   初始账号 admin（密码取 AUTH_BOOTSTRAP_ADMIN_PASSWORD，首次登录强制改密）"
  [ "$consumer_only" -eq 0 ] && log "  建模工作台          http://localhost:${MODELING_PORT:-5100}   用 consumer 账号登录"
  log "后续初始化步骤见 deploy/README.md 第 4 节。"
}

cmd_status() {
  log "容器状态："
  docker ps -a --filter name=onto-agent --filter name=mw-modeling --filter name=mw-eval-mysql \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo
  for pair in "consumer-backend|http://127.0.0.1:${BACKEND_PORT:-5055}/health" \
              "consumer-frontend|http://127.0.0.1:${FRONTEND_PORT:-8888}/" \
              "modeling|http://127.0.0.1:${MODELING_PORT:-5100}/health"; do
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
    backend|frontend|auth-server) consumer_compose logs -f --tail 200 "$svc" ;;
    modeling|eval-mysql)          modeling_compose logs -f --tail 200 "$svc" ;;
    *) fail "用法：./deploy-stack.sh logs <backend|frontend|auth-server|modeling|eval-mysql>" 2 ;;
  esac
}

cmd_help() {
  cat <<'EOF'
用法：./deploy-stack.sh <command> [options]

  preflight                     环境检查（docker/compose/端口），全部 [OK] 才继续
  up [options]                  构建并启动全栈（幂等，可重复执行）
      --with-auth               启用独立鉴权 auth-server（需先配 backend/.env，见 README 第 3.3）
      --with-eval               附带评测库 MySQL（建模评测闭环）
      --consumer-only           只部署运行期 consumer
      --modeling-only           只部署建模工作台（要求 consumer 已在运行）
      --tag <tag>               consumer 镜像 tag（默认 latest）
  status                        容器状态 + 健康检查
  down [--volumes]              停止（--volumes 同时删数据卷，慎用）
  logs <service>                跟踪日志：backend|frontend|auth-server|modeling|eval-mysql

环境变量（可选覆盖）：BACKEND_PORT=5055 FRONTEND_PORT=8888 MODELING_PORT=5100
  EVAL_MYSQL_PORT=3306 EVAL_MYSQL_ROOT_PASSWORD=<启用评测库时必填的强随机值>
  MODELING_LLM_BASE_URL/MODEL/API_KEY（默认 faux 演示模式）
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
