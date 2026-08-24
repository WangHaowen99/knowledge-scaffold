FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
# 先用最小包骨架安装依赖，使业务代码变化不触发 openJiuwen 全量重装。
COPY modeling_agent/__init__.py ./modeling_agent/__init__.py
RUN pip install --no-cache-dir .
COPY modeling_agent ./modeling_agent
COPY skills ./skills

FROM python:3.11-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=base /usr/local /usr/local
COPY pyproject.toml ./
COPY modeling_agent ./modeling_agent
COPY skills ./skills
COPY tests ./tests
RUN pip install --no-cache-dir '.[dev]'
RUN pytest -q

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=base /usr/local /usr/local
COPY modeling_agent ./modeling_agent
COPY skills ./skills

EXPOSE 5110
CMD ["python", "-m", "modeling_agent"]
