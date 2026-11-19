# Onto Web 运行镜像：FastAPI 后端 + 前端静态产物 + 捆绑 Claude Code CLI 的 Agent SDK。
# 捆绑 CLI 是 Bun 编译的 glibc 原生二进制，基础镜像必须用 debian 系，不用 Alpine（方案第八节）。
# 需要 BuildKit（docker buildx / Docker Desktop 默认）：cache/bind mount 用于依赖复用。

FROM node:22-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
# npm 缓存挂载：同机重复构建不再重新下载依赖
RUN --mount=type=cache,target=/root/.npm \
    npm ci --prefer-offline --registry=https://registry.npmmirror.com
COPY frontend/ ./
# 官方包自带的界面代码（assets/packs/<包>/ui/）随平台一起编译（ADR-0055），路径与仓库里一致
COPY assets/packs /build/assets/packs
RUN npm run build

# 连接器网关（ADR-0073、ADR-0104）：单独的镜像，`docker build --target connector-gateway`。只有转换库、数据库驱动、
# 网关代码与协议，不带助手 CLI 和后端其余代码；内置插件（转换器、数据源连接器）在只读目录 /opt/connector/converters，
# 跑在非 root 的子进程里。
# 放在主服务这一段前面：不带 --target 构建时仍是主服务镜像，也不会构建这一段
FROM python:3.11-slim AS connector-gateway
WORKDIR /app
COPY backend/connector-requirements.txt ./
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
RUN --mount=type=bind,source=vendor/wheels,target=/tmp/wheels \
    --mount=type=cache,target=/root/.cache/pip \
    pip install --find-links=/tmp/wheels -i "${PIP_INDEX_URL}" -r connector-requirements.txt
COPY backend/app/__init__.py ./app/__init__.py
COPY backend/app/evidence ./app/evidence
COPY backend/app/connector_gateway ./app/connector_gateway
COPY assets/packs/convert-pdf /opt/connector/converters/convert-pdf
COPY assets/packs/convert-docx /opt/connector/converters/convert-docx
COPY assets/packs/convert-xlsx /opt/connector/converters/convert-xlsx
# 数据源连接器（ADR-0104）：和转换器同在内置插件目录，驱动经 connector-requirements.txt 装进本镜像
COPY assets/packs/db-mysql /opt/connector/converters/db-mysql
COPY assets/packs/db-postgres /opt/connector/converters/db-postgres
# 插件卷、凭据卷的挂载点（ADR-0104）：镜像里先建好、属主 65534，命名卷首次挂载时照这个属主初始化，网关写得进去
RUN mkdir -p /var/lib/connector/plugins /var/lib/connector/secrets \
    && chown 65534:65534 /var/lib/connector/plugins /var/lib/connector/secrets \
    && chmod 0700 /var/lib/connector/secrets
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ONTO_CONNECTOR_CONVERTERS_DIR=/opt/connector/converters \
    ONTO_CONNECTOR_PLUGINS_DIR=/var/lib/connector/plugins \
    ONTO_CONNECTOR_SECRETS_DIR=/var/lib/connector/secrets \
    ONTO_CONNECTOR_GATEWAY_PORT=8120
USER 65534:65534
EXPOSE 8120
# 健康检查写在 compose 里（与 sandbox-gateway 一致）
CMD ["python", "-m", "app.connector_gateway"]

# python:3.11-slim 与 auth-server / consumer backend 同基础镜像，复用已拉取的层
FROM python:3.11-slim
WORKDIR /app

# git：每个项目 workspace 是独立 git 仓库；procps：运维排查用；tini：见文件末尾 ENTRYPOINT
RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# agent 及其工具子进程用独立的非 root 用户（方案 E10）：后端仍以 root 运行，起 CLI / 执行 agent 的命令时
# 切到它，只保留它自己的组；工作区、CLI 的 HOME 归这个组可写，密钥目录 0700 归后端用户，agent 读不到。
# 工作区仓库归 root，agent 跑 git 要放过「所有者不同」的检查
RUN groupadd --system --gid 10010 onto-agent \
    && useradd --system --uid 10010 --gid onto-agent --home-dir /data/home --no-create-home \
       --shell /usr/sbin/nologin onto-agent \
    && git config --system --add safe.directory '*'

COPY backend/requirements.txt ./
# 索引源默认华为云 PyPI 镜像，可经 --build-arg PIP_INDEX_URL 覆盖；ARG 放在 apt 层之后，改源不失效 apt 缓存
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
# vendor/wheels 由 build_release.sh 用宿主 pip 预下载，bind mount 不产生镜像层。
# 联网时 pip 对同版本优先取索引源，vendor/wheels 只在索引不可达时兜底；重复构建的下载由 pip 缓存挂载吸收
RUN --mount=type=bind,source=vendor/wheels,target=/tmp/wheels \
    --mount=type=cache,target=/root/.cache/pip \
    pip install \
    --find-links=/tmp/wheels \
    -i "${PIP_INDEX_URL}" \
    -r requirements.txt

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/run.py ./
COPY assets /opt/onto-modeling/assets
# 兼容软链：存量项目工作区的 .claude/CLAUDE.md 符号链接指向改名前的
# /opt/onto-web 绝对路径与旧资产文件名（ADR-0006），缺了它 agent 工具链断裂
RUN ln -s /opt/onto-modeling /opt/onto-web \
    && ln -s /opt/onto-modeling/assets/workspace-toolkit /opt/onto-modeling/assets/claude-workspace-dotclaude \
    && ln -s /opt/onto-modeling/assets/workspace-rules.md /opt/onto-modeling/assets/claude-workspace-CLAUDE.md \
    && ln -s /opt/onto-modeling/assets/workspace-readme.md /opt/onto-modeling/assets/claude-workspace-README.md
COPY --from=frontend-build /build/frontend/dist /opt/onto-modeling/static
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
    IS_SANDBOX=1 \
    ONTO_WEB_AGENT_USER=onto-agent

VOLUME /data
EXPOSE 8100

# 存活检查（方案 E7）：只看进程能否响应；就绪检查 /api/health/ready 探依赖，不接 HEALTHCHECK，
# 否则依赖抖动会让容器被反复重启
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/api/health/live', timeout=3).status == 200 else 1)"]

# tini 兜底当 1 号进程：回收 CLI、git 留下的孤儿进程并转发 SIGTERM。compose 已配 init: true，
# 但 redeploy-app.sh 沿用旧部署目录的 compose、docker run 也可能不带 --init，镜像自带才可靠；
# 两者叠加时 -s 让这一层以子进程收割者身份工作
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["python", "run.py"]
