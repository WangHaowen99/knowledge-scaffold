FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
# npm 默认走国内镜像，可经 --build-arg NPM_REGISTRY 覆盖；锁文件中的官方源地址由 npm 替换为该镜像
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm ci --ignore-scripts --no-audit --no-fund --registry="${NPM_REGISTRY}"
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt ./
# PyPI 默认走国内镜像，可经 --build-arg PIP_INDEX_URL 覆盖
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt && useradd --uid 10001 --create-home evaluation \
    && mkdir -p /var/lib/evaluation && chown evaluation /var/lib/evaluation
COPY evaluation/ ./evaluation/
COPY run.py ./
COPY --from=frontend /build/dist ./frontend/dist
USER evaluation
ENV EVAL_DATA_DIR=/var/lib/evaluation
EXPOSE 8090
CMD ["python", "run.py"]
