#!/usr/bin/env bash
# 部署独立评测服务（compose 项目 knowledge-evaluation，见 docker-compose.eval-service.yml）。
# 运行配置：EVAL_ENV_FILE 指定受保护文件；部署前补齐并校验集成配置，否则读写本目录 .env。
# 保存凭据、显式配置和选定的网络及 Auth 默认值（进程环境 > .env 已有值 > 默认值），首次部署生成加密密钥。其他非敏感的整栈默认地址
# 写在 compose 中；运行配置以 --env-file 传给 compose，其中的同名值优先于默认值。
# 整栈 deploy.sh 注入 Hub 与 consumer 实际生效的评测凭据（HUB_EVALUATION_SERVICE_TOKEN、
# EVALUATION_SERVICE_TOKEN）、onto-modeling 接收完成通知的凭据（EVAL_MODELING_SERVICE_TOKEN）、consumer 推送
# 用户反馈的凭据（EVAL_FLYWHEEL_SERVICE_TOKEN，EVAL_FEEDBACK_ENABLED=true 时才接收）和 CONSUMER_NETWORK；
# 网络必填，本地 .env 会记住它，供本目录手动执行 compose。
# Judge 模型配置 EVAL_JUDGE_* 由部署方提供，也可由管理员在评测页面“评测判分模型配置”中保存（页面配置优先）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.eval-service.yml"
# compose 项目名固定为 knowledge-evaluation，数据卷全名随之固定
DATA_VOLUME=knowledge-evaluation_evaluation-data

log() { printf '[evaluation] %s\n' "$*"; }
warn() { printf '[evaluation][warn] %s\n' "$*" >&2; }
die() { printf '[evaluation][error] %s\n' "$*" >&2; exit 1; }

env_get() { # <文件> <键>：按 compose env_file 语义清洗 CRLF、引号和行内注释
    local value
    value="$([ -f "$1" ] && grep -E "^$2=" "$1" | tail -n 1 | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    printf '%s' "${value}" | sed -E "s/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^\"(.*)\"$/\1/; s/^'(.*)'$/\1/"
}

env_set() { # <文件> <键> <值>：替换或追加，值不输出
    local temp_file
    temp_file="$(mktemp "$1.tmp.XXXXXX")" || die "Cannot update $1"
    chmod 600 "$temp_file"
    awk -v key="$2" -v value="$3" '
        BEGIN { updated = 0 }
        index($0, key "=") == 1 { if (!updated) { print key "=" value; updated = 1 } next }
        { print }
        END { if (!updated) print key "=" value }
    ' "$1" > "$temp_file" || { rm -f "$temp_file"; die "Cannot update $1"; }
    mv "$temp_file" "$1"
    chmod 600 "$1"
}

settle() { # <键>：进程环境 > .env 已有值，结果为空时不写入
    local value="${!1:-}"
    [ -n "$value" ] || value="$(env_get "$ENV_FILE" "$1")"
    [ -z "$value" ] || env_set "$ENV_FILE" "$1" "$value"
}

command -v docker >/dev/null 2>&1 || die 'docker is required'

prepare_feedback_config() {
    local key value answer backup token
    local -A pending
    for key in EVAL_FLYWHEEL_SERVICE_TOKEN EVAL_MODELING_SERVICE_TOKEN EVAL_MODELING_URL; do
        value="$(env_get "$ENV_FILE" "$key")"
        pending[$key]="${!key:-${value}}"
    done
    pending[EVAL_MODELING_URL]="${pending[EVAL_MODELING_URL]:-http://onto-modeling:8100}"
    for key in EVAL_FEEDBACK_ENABLED EVAL_NOTIFY_FEEDBACK_RUNS; do
        value="${!key:-$(env_get "$ENV_FILE" "$key")}"
        case "${value,,}" in
            ''|true|1|yes|on) pending[$key]=true ;;
            false|0|no|off)
                pending[$key]=false
                if [ -z "${!key:-}" ]; then
                    [ -t 0 ] || die "$key 已显式关闭，请确认后在调用环境中显式设置该开关为 true 或 false"
                    read -r -p "$key 已关闭，是否开启？[y/N] " answer
                    [ "${answer,,}" != y ] || pending[$key]=true
                fi
                ;;
            *) die "$key 必须是布尔值" ;;
        esac
    done
    if [ "${pending[EVAL_FEEDBACK_ENABLED]}" = true ]; then
        token="${pending[EVAL_FLYWHEEL_SERVICE_TOKEN]}"
        [ -n "$token" ] || die '开启反馈接收需要 EVAL_FLYWHEEL_SERVICE_TOKEN'
    fi
    if [ "${pending[EVAL_NOTIFY_FEEDBACK_RUNS]}" = true ]; then
        token="${pending[EVAL_MODELING_SERVICE_TOKEN]}"
        [ -n "$token" ] || die '开启反馈通知需要 EVAL_MODELING_SERVICE_TOKEN'
        printf '%s' "${pending[EVAL_MODELING_URL]}" | python3 -c '
import sys
from urllib.parse import urlsplit
value = sys.stdin.read()
try:
    parsed = urlsplit(value)
    valid = parsed.scheme in {"http", "https"} and parsed.hostname and not any(
        (parsed.username, parsed.password, parsed.query, parsed.fragment))
    valid = valid and not any(character.isspace() for character in value)
    parsed.port
except ValueError:
    valid = False
sys.exit(0 if valid else 1)
' || die 'EVAL_MODELING_URL 必须是有效的 HTTP(S) 地址'
    fi
    for key in "${!pending[@]}"; do
        [ -n "${pending[$key]}" ] || continue
        [ "$(env_get "$ENV_FILE" "$key")" != "${pending[$key]}" ] || continue
        if [ -f "$ENV_FILE" ] && [ -z "${backup:-}" ]; then
            backup="$(mktemp "$ENV_FILE.integration-backup.XXXXXX")"
            chmod 600 "$backup"
            cp "$ENV_FILE" "$backup"
            chmod 600 "$backup"
        fi
        [ -f "$ENV_FILE" ] || (umask 077; : > "$ENV_FILE")
        env_set "$ENV_FILE" "$key" "${pending[$key]}"
        export "$key=${pending[$key]}"
    done
}

if [ -n "${EVAL_ENV_FILE:-}" ]; then
    [ -f "$EVAL_ENV_FILE" ] && [ -r "$EVAL_ENV_FILE" ] || die \
        "Evaluation runtime configuration not found: EVAL_ENV_FILE=$EVAL_ENV_FILE (relative paths resolve from $(pwd)); fix the path, or unset EVAL_ENV_FILE to use $SCRIPT_DIR/.env"
    ENV_FILE="$(cd "$(dirname "$EVAL_ENV_FILE")" && pwd)/$(basename "$EVAL_ENV_FILE")"
else
    ENV_FILE="$SCRIPT_DIR/.env"
fi

# 网络先于配置校验：缺失网络时不生成密钥、不改写配置
CONSUMER_NETWORK="${CONSUMER_NETWORK:-$(env_get "$ENV_FILE" CONSUMER_NETWORK)}"
CONSUMER_NETWORK="${CONSUMER_NETWORK:-onto-auth-network}"
docker network inspect "$CONSUMER_NETWORK" >/dev/null 2>&1 \
    || die "Docker network not found: $CONSUMER_NETWORK; deploy onto-modeling first or set CONSUMER_NETWORK to the actual shared network"
export CONSUMER_NETWORK

EVAL_AUTH_URL="${EVAL_AUTH_URL:-$(env_get "$ENV_FILE" EVAL_AUTH_URL)}"
EVAL_AUTH_ISSUER="${EVAL_AUTH_ISSUER:-$(env_get "$ENV_FILE" EVAL_AUTH_ISSUER)}"
EVAL_AUTH_URL="${EVAL_AUTH_URL:-http://auth-server:5056}"
EVAL_AUTH_ISSUER="${EVAL_AUTH_ISSUER:-http://auth-server:5056}"
export EVAL_AUTH_URL EVAL_AUTH_ISSUER
: "${EVAL_HUB_SERVICE_TOKEN:=${HUB_EVALUATION_SERVICE_TOKEN:-}}"
: "${EVAL_CONSUMER_SERVICE_TOKEN:=${EVALUATION_SERVICE_TOKEN:-}}"
prepare_feedback_config

if [ -n "${EVAL_ENV_FILE:-}" ]; then
    log 'Using EVAL_ENV_FILE with verified integration configuration'
else
    [ -f "$ENV_FILE" ] || (umask 077 && : > "$ENV_FILE")
    chmod 600 "$ENV_FILE"
    if [ -z "${EVAL_ENCRYPTION_KEY:-}" ] && [ -z "$(env_get "$ENV_FILE" EVAL_ENCRYPTION_KEY)" ]; then
        # 已有数据卷时其中可能有加密数据，换新密钥会让它们无法解密，必须找回原配置
        if docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1; then
            die "Data volume $DATA_VOLUME exists but $ENV_FILE has no EVAL_ENCRYPTION_KEY; restore the original configuration or set EVAL_ENV_FILE instead of generating a new key"
        fi
        EVAL_ENCRYPTION_KEY="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n')"
        log 'Generated EVAL_ENCRYPTION_KEY (value is not printed); back it up separately from the data volume'
    fi
    : "${EVAL_HUB_SERVICE_TOKEN:=${HUB_EVALUATION_SERVICE_TOKEN:-}}"
    : "${EVAL_CONSUMER_SERVICE_TOKEN:=${EVALUATION_SERVICE_TOKEN:-}}"
    for key in EVAL_ENCRYPTION_KEY EVAL_HUB_SERVICE_TOKEN EVAL_CONSUMER_SERVICE_TOKEN EVAL_MODELING_SERVICE_TOKEN \
            EVAL_AUTH_URL EVAL_AUTH_ISSUER EVAL_HUB_URL EVAL_CONSUMER_API_URL \
            EVAL_JUDGE_BASE_URL EVAL_JUDGE_API_KEY EVAL_JUDGE_MODEL EVAL_JUDGE_CONFIG_REVISION \
            EVAL_SQL_CONFIG EVAL_NL2SQL_URL EVAL_TRUSTED_ORIGINS EVAL_BIND_ADDRESS EVAL_PORT EVAL_MODELING_URL CONSUMER_NETWORK \
            EVAL_NOTIFICATION_MAX_ATTEMPTS EVAL_JUDGE_ALLOWED_HOSTS EVAL_FLYWHEEL_SERVICE_TOKEN EVAL_FEEDBACK_ENABLED \
            EVAL_NOTIFY_FEEDBACK_RUNS EVAL_FEEDBACK_RETENTION_DAYS EVAL_FEEDBACK_WINDOW_DAYS EVAL_FEEDBACK_L1_MIN_USERS \
            EVAL_FEEDBACK_L1_MIN_DOWNVOTES EVAL_FEEDBACK_SEAL_CLUSTERS EVAL_FEEDBACK_SEAL_IDLE_MINUTES; do
        settle "$key"
    done
    log "Runtime configuration is ready: $ENV_FILE (values are not printed)"
fi

[ -n "$(env_get "$ENV_FILE" EVAL_HUB_SERVICE_TOKEN)" ] \
    || warn 'EVAL_HUB_SERVICE_TOKEN is empty: deploy the Knowledge Hub first (bash deploy/deploy.sh --knowledge-hub-only), then redeploy evaluation'
[ -n "$(env_get "$ENV_FILE" EVAL_CONSUMER_SERVICE_TOKEN)" ] \
    || warn 'EVAL_CONSUMER_SERVICE_TOKEN is empty: redeploy consumer so it generates EVALUATION_SERVICE_TOKEN, then redeploy evaluation'
[ -n "$(env_get "$ENV_FILE" EVAL_MODELING_SERVICE_TOKEN)" ] \
    || warn 'EVAL_MODELING_SERVICE_TOKEN is empty: completion notifications to Onto Modeling are disabled; deploy Onto Modeling so it generates ONTO_EVALUATION_SERVICE_TOKEN, then redeploy evaluation'
for key in EVAL_JUDGE_BASE_URL EVAL_JUDGE_API_KEY EVAL_JUDGE_MODEL EVAL_JUDGE_CONFIG_REVISION; do
    [ -n "$(env_get "$ENV_FILE" "$key")" ] || warn "$key is not configured: judging will fail until it is set in $ENV_FILE or an administrator saves the Judge model on the evaluation page"
done

EVAL_ENV_FILE="$ENV_FILE"
export EVAL_ENV_FILE
# 同一份运行配置既注入容器（env_file），也参与 compose 插值，使其中的地址覆盖 compose 默认值
COMPOSE_ARGS=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")
if [ -n "${EVAL_COMPOSE_OVERRIDE:-}" ]; then
    [ -f "$EVAL_COMPOSE_OVERRIDE" ] || { echo 'Evaluation Compose override not found' >&2; exit 1; }
    COMPOSE_ARGS+=(-f "$EVAL_COMPOSE_OVERRIDE")
fi
if [ -f "$SCRIPT_DIR/../evaluation-image.tar" ]; then
    if [ -f "$SCRIPT_DIR/../IMAGE" ]; then
        EVAL_IMAGE="$(cat "$SCRIPT_DIR/../IMAGE")"
        export EVAL_IMAGE
    fi
    docker load -i "$SCRIPT_DIR/../evaluation-image.tar"
    docker compose "${COMPOSE_ARGS[@]}" up -d --no-build
else
    # 镜像源默认见 Dockerfile（国内镜像）；显式配置时经 compose 构建参数覆盖，空值不传以免覆盖默认值
    PIP_INDEX_URL="${EVAL_PIP_INDEX_URL:-${PIP_INDEX_URL:-}}"
    NPM_REGISTRY="${EVAL_NPM_REGISTRY:-${NPM_REGISTRY:-}}"
    if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; else unset PIP_INDEX_URL; fi
    if [ -n "$NPM_REGISTRY" ]; then export NPM_REGISTRY; else unset NPM_REGISTRY; fi
    docker compose "${COMPOSE_ARGS[@]}" up -d --build
fi
