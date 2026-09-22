#!/bin/bash
# 在目标机部署 Onto Modeling：预检 → 加载镜像 → 准备 .env → compose 拉起。
# 在解包后的交付目录（含 images/、images.env、docker-compose.release.yml）内运行。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

bash ./preflight.sh

if [ -d images ]; then
    # 只加载本发布版本的镜像：同一目录反复解包会累积历史版本 tar，
    # 全量加载会把部署拖到分钟级（每个 tar 数百 MB 起）
    RELEASE_VERSION="$(sed -n 's/^RELEASE_VERSION=//p' images.env 2>/dev/null | head -1)"
    for tarball in images/*${RELEASE_VERSION:+-${RELEASE_VERSION}}.tar; do
        [ -f "${tarball}" ] || continue
        echo "Loading image: ${tarball}"
        docker load -i "${tarball}"
    done
fi

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "已从 .env.example 生成 .env（模型网关可稍后在「配置中心」里填写）。"
fi

# auth-server 零配置直连：首次部署自动生成初始管理员密码，写入 .env 并在结尾提示。
# 已有用户数据的部署（卷非空）该变量不再生效，保留无害。
BOOTSTRAP_GENERATED=""
if ! grep -q "^AUTH_BOOTSTRAP_ADMIN_PASSWORD=..*" .env; then
    BOOTSTRAP_GENERATED="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
    sed -i '/^AUTH_BOOTSTRAP_ADMIN_PASSWORD=/d' .env
    printf 'AUTH_BOOTSTRAP_ADMIN_PASSWORD=%s\n' "${BOOTSTRAP_GENERATED}" >> .env
    chmod 600 .env
fi

if grep -q "replace-with-real-token-at-runtime" .env; then
    echo "提示：.env 未填模型网关令牌；部署后可在管理员「配置中心」里配置 Base URL / API Key。"
fi

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"

# 外接账号中心：.env 声明外部 auth 网络时，停用自带 auth-server 并加入该网络
# （consumer 栈的服务名同为 auth-server，容器网络内地址无需变化）
COMPOSE_FILES=(-f docker-compose.release.yml)
EXTERNAL_AUTH_NETWORK="$(sed -n 's/^ONTO_MODELING_EXTERNAL_AUTH_NETWORK=//p' .env | head -1)"
if [ -n "${EXTERNAL_AUTH_NETWORK}" ]; then
    cat > docker-compose.external-auth.yml <<EOF2
# 由 deploy.sh 生成：接入外部账号中心（勿手改，改 .env 后重跑 deploy.sh）
services:
  auth-server:
    profiles: ["disabled"]
  onto-modeling:
    depends_on: !override []
    networks:
      - default
      - external-auth
networks:
  external-auth:
    external: true
    name: ${EXTERNAL_AUTH_NETWORK}
EOF2
    COMPOSE_FILES+=(-f docker-compose.external-auth.yml)
    echo "已启用外接账号中心：网络 ${EXTERNAL_AUTH_NETWORK}（自带 auth-server 停用）"
fi

# 镜像 tag 经 --env-file 注入 compose 插值：不依赖 shell 进程环境，
# WSL 下 docker 为 Windows 进程（interop 不透传 export 变量）时同样生效
${COMPOSE} --env-file .env --env-file images.env "${COMPOSE_FILES[@]}" up -d --remove-orphans

PORT="$(sed -n 's/^ONTO_WEB_PORT=//p' .env | head -1)"
echo
echo "部署完成。访问 http://<部署机地址>:${PORT:-5100}"
if [ -n "${BOOTSTRAP_GENERATED}" ]; then
    echo "初始管理员账号：admin / ${BOOTSTRAP_GENERATED}（已写入本目录 .env，仅首次部署生效，登录后请尽快修改）"
fi
echo "查看状态：${COMPOSE} -f docker-compose.release.yml ps"

# 检测原 modeling 服务：有数据则自动迁移（幂等，已迁移的项目自动跳过）
MODELING_CONTAINER="${ONTO_MODELING_MIGRATE_FROM:-mw-modeling}"
if [ -f "${SCRIPT_DIR}/migrate-from-modeling.sh" ] \
    && docker ps --format '{{.Names}}' | grep -qx "${MODELING_CONTAINER}"; then
    echo
    echo "检测到 modeling 服务（${MODELING_CONTAINER}）正在运行，开始迁移其项目数据…"
    bash "${SCRIPT_DIR}/migrate-from-modeling.sh" "${MODELING_CONTAINER}" onto-modeling \
        || echo "迁移未完成（不影响本次部署），可稍后手动执行 migrate-from-modeling.sh"
fi
