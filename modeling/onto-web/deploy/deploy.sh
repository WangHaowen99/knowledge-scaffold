#!/bin/bash
# 在目标机部署 Onto Web：预检 → 加载镜像 → 准备 .env → compose 拉起。
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
    echo
    echo "已从 .env.example 生成 .env，请先编辑填入模型网关配置："
    echo "  ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN"
    echo "然后重新运行本脚本。"
    exit 1
fi

if grep -q "replace-with-real-token-at-runtime" .env; then
    echo "错误：.env 中的 ANTHROPIC_AUTH_TOKEN 仍是占位符，请填入真实网关令牌后重试。" >&2
    exit 1
fi

if ! grep -q "^AUTH_BOOTSTRAP_ADMIN_PASSWORD=..*" .env; then
    echo "提示：.env 未设置 AUTH_BOOTSTRAP_ADMIN_PASSWORD；首次部署（auth 数据卷为空）会启动失败，已有用户数据的部署可忽略。"
fi

COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"

# 镜像 tag 经 --env-file 注入 compose 插值：不依赖 shell 进程环境，
# WSL 下 docker 为 Windows 进程（interop 不透传 export 变量）时同样生效
${COMPOSE} --env-file .env --env-file images.env -f docker-compose.release.yml up -d

echo
echo "部署完成。访问 http://<部署机地址>:${ONTO_WEB_PORT:-8100}"
echo "查看状态：${COMPOSE} -f docker-compose.release.yml ps"
