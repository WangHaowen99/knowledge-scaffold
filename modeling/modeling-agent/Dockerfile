FROM python:3.11-slim AS base

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
# 先用最小包骨架安装依赖，使业务代码变化不触发 openJiuwen 全量重装。
COPY modeling_agent/__init__.py ./modeling_agent/__init__.py
RUN python -m pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --timeout "${PIP_DEFAULT_TIMEOUT}" \
        --retries "${PIP_RETRIES}" \
        .
COPY modeling_agent ./modeling_agent
COPY skills ./skills

FROM python:3.11-slim AS test
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=base /usr/local /usr/local
COPY pyproject.toml ./
COPY modeling_agent ./modeling_agent
COPY skills ./skills
COPY tests ./tests
RUN python -m pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --timeout "${PIP_DEFAULT_TIMEOUT}" \
        --retries "${PIP_RETRIES}" \
        '.[dev]'
RUN pytest -q

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=base /usr/local /usr/local
COPY modeling_agent ./modeling_agent
COPY skills ./skills

EXPOSE 5110
CMD ["python", "-m", "modeling_agent"]
