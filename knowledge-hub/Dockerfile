FROM python:3.11-slim-bookworm
WORKDIR /app
# Debian 与 PyPI 默认走国内镜像，可经 --build-arg APT_MIRROR（主机名，deb.debian.org 即官方源）
# 与 PIP_INDEX_URL 覆盖；部署与构建脚本读取 KNOWLEDGE_HUB_APT_MIRROR、KNOWLEDGE_HUB_PIP_INDEX_URL。
# git：知识包版本以 git 仓库归档（hub/storage.py）
ARG APT_MIRROR=mirrors.huaweicloud.com
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt
COPY backend ./
ENV HUB_HOST=0.0.0.0 HUB_PORT=8200
VOLUME /data
EXPOSE 8200
CMD ["python", "run.py"]
