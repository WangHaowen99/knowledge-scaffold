# Onto Web 运行镜像：FastAPI 后端 + 前端静态产物 + 捆绑 Claude Code CLI 的 Agent SDK。
# 捆绑 CLI 是 Bun 编译的 glibc 原生二进制，基础镜像必须用 debian 系，不用 Alpine（方案第八节）。
# 需要 BuildKit（docker buildx / Docker Desktop 默认）：cache/bind mount 用于依赖复用。

FROM node:22-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
# npm 缓存挂载：同机重复构建不再重新下载依赖
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline --registry=https://registry.npmmirror.com
COPY frontend/ ./
RUN npm run build

# python:3.11-slim 与 auth-server / consumer backend 同基础镜像，复用已拉取的层
FROM python:3.11-slim
WORKDIR /app

# git：每个项目 workspace 是独立 git 仓库；procps：运维排查用
RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
# vendor/wheels 由 build_release.sh 用宿主 pip 预下载（含约 100MB 的 claude-agent-sdk），
# bind mount 不产生镜像层；本地缺的包经 pip 缓存挂载回落到镜像源
RUN --mount=type=bind,source=vendor/wheels,target=/tmp/wheels \
    --mount=type=cache,target=/root/.cache/pip \
    pip install \
    --find-links=/tmp/wheels \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt

COPY backend/app ./app
COPY backend/run.py ./
COPY assets /opt/onto-modeling/assets
COPY --from=frontend-build /build/dist /opt/onto-modeling/static
# drawio 官方 webapp（build_release.sh 供应到 vendor/drawio/webapp；缺席时编辑功能降级为只读）
COPY vendor/drawio/webapp /opt/onto-modeling/drawio

# HOME 指向数据卷：CLI 会话 transcript（resume 续接依赖）与 ~/.claude 配置随卷持久化
# IS_SANDBOX=1：容器内以 root 运行时 CLI 才允许 bypassPermissions（否则启动即拒绝）
ENV ONTO_WEB_HOST=0.0.0.0 \
    ONTO_WEB_PORT=8100 \
    # 数据卷内路径保持历史值 /data/onto-web：存量部署的数据在该子目录下
    ONTO_WEB_DATA_DIR=/data/onto-web \
    ONTO_WEB_ASSETS_DIR=/opt/onto-modeling/assets \
    ONTO_WEB_STATIC_DIR=/opt/onto-modeling/static \
    ONTO_WEB_DRAWIO_DIR=/opt/onto-modeling/drawio \
    ONTO_WEB_RELOAD=0 \
    HOME=/data/home \
    DISABLE_AUTOUPDATER=1 \
    PYTHONUNBUFFERED=1 \
    IS_SANDBOX=1

VOLUME /data
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=3).status == 200 else 1)"]

CMD ["python", "run.py"]
