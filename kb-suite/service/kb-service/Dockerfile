FROM docker.1ms.run/library/python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "asyncpg>=0.29,<0.31" "httpx>=0.27,<1.0" \
      "grpcio>=1.62,<2" "grpcio-tools>=1.62,<2" "protobuf>=5.26,<7" "pydantic>=2.6,<3" \
      "pydantic-settings>=2.2,<3" "python-multipart>=0.0.9" "mcp>=1.12,<2" "openpyxl>=3.1,<4" \
      "pytest>=8" "pytest-asyncio>=0.23"

COPY proto ./proto
COPY kb_service ./kb_service
COPY tests ./tests
RUN mkdir -p kb_service/_proto \
 && python -m grpc_tools.protoc -I proto --python_out=kb_service/_proto --grpc_python_out=kb_service/_proto proto/docreader.proto \
 && sed -i 's/^import docreader_pb2 as/from . import docreader_pb2 as/' kb_service/_proto/docreader_pb2_grpc.py \
 && touch kb_service/_proto/__init__.py \
 && pip install --no-cache-dir --no-deps .

RUN useradd -r -u 10001 kbs && mkdir -p /data/files && chown -R kbs /data
USER kbs

EXPOSE 8080
# h11 rather than httptools: Java's HttpClient (DA-java) sends POSTs with an
# "Upgrade: h2c" header; httptools rejects those at the protocol level, h11
# ignores the upgrade and serves the request as HTTP/1.1.
CMD ["uvicorn", "kb_service.api.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--http", "h11"]
