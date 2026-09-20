#!/bin/bash
# 在目标机部署 Onto Modeling：预检 → 加载镜像 → 准备 .env → compose 拉起。
# 在解包后的交付目录（含 images/、images.env、docker-compose.release.yml）内运行。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

bash ./preflight.sh

if [ -d images ]; then
    for tarball in images/*.tar; do
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

# 镜像 tag 经 --env-file 注入 compose 插值：不依赖 shell 进程环境，
# WSL 下 docker 为 Windows 进程（interop 不透传 export 变量）时同样生效
${COMPOSE} --env-file .env --env-file images.env -f docker-compose.release.yml up -d

PORT="$(sed -n 's/^ONTO_WEB_PORT=//p' .env | head -1)"
echo
echo "部署完成。访问 http://<部署机地址>:${PORT:-8100}"
if [ -n "${BOOTSTRAP_GENERATED}" ]; then
    echo "初始管理员账号：admin / ${BOOTSTRAP_GENERATED}（已写入本目录 .env，仅首次部署生效，登录后请尽快修改）"
fi
echo "查看状态：${COMPOSE} -f docker-compose.release.yml ps"
