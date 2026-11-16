FROM docker.1ms.run/library/python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN pip install --no-cache-dir \
      "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "httpx>=0.27,<1.0" "pydantic>=2.6,<3" \
      "pydantic-settings>=2.2,<3" "python-multipart>=0.0.9" "pyyaml>=6,<7" "mcp>=1.26,<2"

COPY pyproject.toml ./
COPY kb_gateway ./kb_gateway
RUN pip install --no-cache-dir --no-deps .

RUN useradd -r -u 10001 kbgw
USER kbgw

EXPOSE 8080
# h11: DA-java's HttpClient sends "Upgrade: h2c", which httptools rejects (same as kb-service)
CMD ["uvicorn", "kb_gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--http", "h11"]
