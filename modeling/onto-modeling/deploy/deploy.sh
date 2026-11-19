#!/bin/bash
# 在目标机部署 Onto Modeling：准备共享网络与配置 → compose 拉起。
# 在解包后的交付目录（含 images/、images.env、docker-compose.release.yml）内运行。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
VERIFY_ONLY=0
if [ "${1:-}" = "--verify-dependencies" ] && [ "$#" -eq 1 ]; then
    VERIFY_ONLY=1
elif [ "$#" -ne 0 ]; then
    echo '用法：bash deploy.sh [--verify-dependencies]' >&2
    exit 2
fi

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"

# 外接账号中心：首次安装可准备共享网络，不创建或接管 Auth 容器。
COMPOSE_FILES=(-f docker-compose.release.yml)
if docker container inspect onto-modeling-auth-server >/dev/null 2>&1; then
    echo '检测到旧 Onto 自带 Auth；须先核对并迁移账号归属，拒绝删除旧服务' >&2
    exit 1
fi
AUTH_CONFIG_FILE=.env
[ -f "${AUTH_CONFIG_FILE}" ] || AUTH_CONFIG_FILE=.env.example
[ -f "${AUTH_CONFIG_FILE}" ] || { echo '缺少 .env 和 .env.example' >&2; exit 1; }
EXTERNAL_AUTH_NETWORK="$(sed -n 's/^ONTO_MODELING_EXTERNAL_AUTH_NETWORK=//p' "${AUTH_CONFIG_FILE}" | head -1)"
AUTH_URL="$(sed -n 's/^ONTO_MODELING_AUTH_URL=//p' "${AUTH_CONFIG_FILE}" | tail -1)"
AUTH_ISSUER="$(sed -n 's/^ONTO_WEB_AUTH_ISSUER=//p' "${AUTH_CONFIG_FILE}" | tail -1)"
AUTH_AUDIENCE="$(sed -n 's/^ONTO_WEB_AUTH_AUDIENCE=//p' "${AUTH_CONFIG_FILE}" | tail -1)"
CONSUMER_URL="$(sed -n 's/^ONTO_MODELING_CONSUMER_URL=//p' "${AUTH_CONFIG_FILE}" | tail -1)"
EXTERNAL_AUTH_NETWORK="${EXTERNAL_AUTH_NETWORK:-onto-auth-network}"
AUTH_URL="${AUTH_URL:-http://auth-server:5056}"
AUTH_ISSUER="${AUTH_ISSUER:-http://auth-server:5056}"
AUTH_AUDIENCE="${AUTH_AUDIENCE:-knowledge-scaffold}"
[[ "${EXTERNAL_AUTH_NETWORK}" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] \
    || { echo '共享 Docker 网络名不符合独立 Auth 网络规则' >&2; exit 1; }
CONSUMER_URL="${CONSUMER_URL:-http://backend:5055}"
if ! docker network inspect "${EXTERNAL_AUTH_NETWORK}" >/dev/null 2>&1; then
    if [ "${VERIFY_ONLY}" -eq 1 ] || docker container inspect onto-modeling >/dev/null 2>&1 || \
            docker volume inspect onto-web_onto-web-data >/dev/null 2>&1; then
        echo '共享 Docker 网络尚未创建' >&2
        exit 1
    fi
    docker network create --label auth.shared=true "${EXTERNAL_AUTH_NETWORK}" >/dev/null || \
        docker network inspect "${EXTERNAL_AUTH_NETWORK}" >/dev/null 2>&1
fi
NETWORK_LABEL="$(docker network inspect "${EXTERNAL_AUTH_NETWORK}" \
    --format '{{index .Labels "auth.shared"}}' 2>/dev/null || true)"
if [ "${NETWORK_LABEL}" != true ] && \
        ! docker container inspect onto-agent-backend \
            --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' \
            2>/dev/null | grep -Fx "${EXTERNAL_AUTH_NETWORK}" >/dev/null; then
    echo '共享 Docker 网络不是独立 Auth 网络，也未连接已安装的 Consumer' >&2
    exit 1
fi
if [ "${VERIFY_ONLY}" -eq 0 ]; then
    bash ./preflight.sh
fi
EXISTING_INSTALL=0
if docker container inspect onto-modeling >/dev/null 2>&1 || \
        docker volume inspect onto-web_onto-web-data >/dev/null 2>&1 || \
        [ -f "${SCRIPT_DIR}/.dependencies.json" ]; then
    EXISTING_INSTALL=1
    if [ -f "${SCRIPT_DIR}/.dependencies.json" ] && \
            python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["phase"] in ("waiting-dependencies", "verifying-service") else 1)' \
                "${SCRIPT_DIR}/.dependencies.json"; then
        EXISTING_INSTALL=0
    fi
fi
if [ "${VERIFY_ONLY}" -eq 0 ] && [ -d images ]; then
    RELEASE_VERSION="$(sed -n 's/^RELEASE_VERSION=//p' images.env 2>/dev/null | head -1)"
    for tarball in images/*${RELEASE_VERSION:+-${RELEASE_VERSION}}.tar; do
        [ -f "${tarball}" ] || continue
        echo "Loading image: ${tarball}"
        docker load -i "${tarball}"
    done
fi
ONTO_IMAGE="$(sed -n 's/^ONTO_WEB_IMAGE=//p' images.env | tail -1)"
[ -n "${ONTO_IMAGE}" ] || { echo 'Onto 镜像清单缺失' >&2; exit 1; }
docker image inspect "${ONTO_IMAGE}" >/dev/null 2>&1 \
    || { echo 'Onto 镜像尚未导入' >&2; exit 1; }
PROBE_ARGS=(--image "${ONTO_IMAGE}" --network "${EXTERNAL_AUTH_NETWORK}"
    --auth-url "${AUTH_URL}" --issuer "${AUTH_ISSUER}" --audience "${AUTH_AUDIENCE}"
    --consumer-url "${CONSUMER_URL}" --state-file "${SCRIPT_DIR}/.dependencies.json")
if [ "${VERIFY_ONLY}" -eq 1 ]; then
    [ -f "${SCRIPT_DIR}/.dependencies.json" ] \
        || { echo '未发现本次 Modeling 安装的依赖记录' >&2; exit 1; }
    python3 ./dependency_probe.py --verify "${PROBE_ARGS[@]}"
    exit 0
fi
if [ "${EXISTING_INSTALL}" -eq 1 ]; then
    PROBE_ARGS+=(--require-ready)
fi
PROBE_MODE=()
if [ "${EXISTING_INSTALL}" -eq 1 ]; then
    PROBE_MODE+=(--check-only)
fi
python3 ./dependency_probe.py "${PROBE_MODE[@]}" "${PROBE_ARGS[@]}" \
    || { echo 'Auth 或 Consumer 依赖配置无效' >&2; exit 1; }

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "已从 .env.example 生成 .env（模型网关可稍后在「配置中心」里填写）。"
fi
# 旧 .env 可能缺少这些键或保留空值；写回探测时实际采用的默认契约。
python3 - .env "${EXTERNAL_AUTH_NETWORK}" "${AUTH_URL}" "${AUTH_ISSUER}" "${AUTH_AUDIENCE}" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
defaults = dict(zip(("ONTO_MODELING_EXTERNAL_AUTH_NETWORK", "ONTO_MODELING_AUTH_URL",
                     "ONTO_WEB_AUTH_ISSUER", "ONTO_WEB_AUTH_AUDIENCE"), sys.argv[2:]))
lines = path.read_text().splitlines()
seen = set()
for index, line in enumerate(lines):
    key, separator, value = line.partition("=")
    if separator and key in defaults:
        seen.add(key)
        if not value:
            lines[index] = key + "=" + defaults[key]
lines.extend(key + "=" + value for key, value in defaults.items() if key not in seen)
with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".env-", delete=False) as stream:
    os.fchmod(stream.fileno(), 0o600)
    stream.write("\n".join(lines) + "\n")
    temporary = Path(stream.name)
temporary.replace(path)
PY
if ! grep -q '^ONTO_HUB_SERVICE_TOKEN=..*' .env; then
    HUB_GENERATED="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    printf 'ONTO_HUB_SERVICE_TOKEN=%s\n' "${HUB_GENERATED}" >> .env
    chmod 600 .env
    unset HUB_GENERATED
fi
if ! grep -q '^ONTO_EVALUATION_SERVICE_TOKEN=..*' .env; then
    EVALUATION_GENERATED="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    printf '\nONTO_EVALUATION_SERVICE_TOKEN=%s\n' "${EVALUATION_GENERATED}" >> .env
    chmod 600 .env
    unset EVALUATION_GENERATED
fi
if grep -q "replace-with-real-token-at-runtime" .env; then
    echo "提示：.env 未填模型网关令牌；部署后可在管理员「配置中心」里配置 Base URL / API Key。"
fi
cat > docker-compose.external-auth.yml <<EOF2
# 由 deploy.sh 生成：接入外部账号中心（勿手改，改 .env 后重跑 deploy.sh）
services:
  onto-modeling:
    networks:
      - default
      - external-auth
networks:
  external-auth:
    external: true
    name: ${EXTERNAL_AUTH_NETWORK}
EOF2
COMPOSE_FILES+=(-f docker-compose.external-auth.yml)

# 脚本运行验证的沙箱：.env 记着 consumer 的 jiuwenbox socket 卷（整栈部署脚本探测写入）时
# 只读挂给脚本验证网关 sandbox-gateway（不挂进跑建模 agent 的 onto-modeling 容器），代码生成的
# 脚本经网关在沙箱中验证；没有该卷则网关报不可用，验证在 onto-modeling 容器内执行。
JIUWENBOX_SOCKET_VOLUME="$(sed -n 's/^ONTO_MODELING_JIUWENBOX_SOCKET_VOLUME=//p' .env | head -1)"
if [ -n "${JIUWENBOX_SOCKET_VOLUME}" ] && docker volume inspect "${JIUWENBOX_SOCKET_VOLUME}" >/dev/null 2>&1; then
    cat > docker-compose.jiuwenbox.yml <<EOF2
# 由 deploy.sh 生成：把 consumer 的 jiuwenbox socket 只读挂给脚本验证网关（勿手改，改 .env 后重跑 deploy.sh）
services:
  sandbox-gateway:
    volumes:
      - jiuwenbox-sock:/run/jiuwenbox:ro
volumes:
  jiuwenbox-sock:
    external: true
    name: ${JIUWENBOX_SOCKET_VOLUME}
EOF2
    COMPOSE_FILES+=(-f docker-compose.jiuwenbox.yml)
    echo "已把 jiuwenbox 沙箱 socket 挂给脚本验证网关：卷 ${JIUWENBOX_SOCKET_VOLUME}"
else
    rm -f docker-compose.jiuwenbox.yml
    echo "未挂载 jiuwenbox 沙箱：代码生成的脚本运行验证将在 onto-modeling 容器内执行"
fi

# 镜像 tag 经 --env-file 注入 compose 插值：不依赖 shell 进程环境，
# WSL 下 docker 为 Windows 进程（interop 不透传 export 变量）时同样生效
${COMPOSE} --env-file .env --env-file images.env "${COMPOSE_FILES[@]}" up -d --remove-orphans

if [ "${EXISTING_INSTALL}" -eq 1 ]; then
    echo 'Onto 升级命令已执行；请按现有升级验收流程检查服务'
elif python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["phase"]=="verifying-service" else 1)' \
        "${SCRIPT_DIR}/.dependencies.json"; then
    echo '依赖已就绪；Onto、Hub 和评测均启动后执行 bash deploy.sh --verify-dependencies'
else
    echo '依赖尚未就绪：Onto 将等待 Auth；Auth 和 Consumer 就绪后执行 bash deploy.sh --verify-dependencies'
fi

PORT="$(sed -n 's/^ONTO_WEB_PORT=//p' .env | head -1)"
echo
echo "部署完成。访问 http://<部署机地址>:${PORT:-5100}"
echo "查看状态：${COMPOSE} -f docker-compose.release.yml ps"

# 检测原 modeling 服务：有数据则自动迁移（幂等，已迁移的项目自动跳过）
MODELING_CONTAINER="${ONTO_MODELING_MIGRATE_FROM:-mw-modeling}"
if [ -f "${SCRIPT_DIR}/migrate-from-modeling.sh" ] \
    && docker ps --format '{{.Names}}' | grep -qx "${MODELING_CONTAINER}"; then
    echo
    echo "检测到 modeling 服务（${MODELING_CONTAINER}）正在运行，开始迁移其项目数据…"
    bash "${SCRIPT_DIR}/migrate-from-modeling.sh" "${MODELING_CONTAINER}" onto-modeling \
        || echo "[WARN] 项目迁移失败：原项目仍在 ${MODELING_CONTAINER}，尚未迁入 Onto Modeling（Onto Modeling 本身已部署）。" \
            "修复后执行：bash ${SCRIPT_DIR}/migrate-from-modeling.sh ${MODELING_CONTAINER} onto-modeling" >&2
fi
