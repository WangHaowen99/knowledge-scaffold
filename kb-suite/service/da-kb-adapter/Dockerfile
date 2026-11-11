FROM docker.1ms.run/library/python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN pip install --no-cache-dir \
      "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "httpx>=0.27,<1.0" \
      "pydantic-settings>=2.2,<3" "python-multipart>=0.0.9"

# Build with: docker build --build-context provider-kit=<kb-suite>/service/provider-kit \
#   --build-context kb-service=<kb-suite>/service/kb-service <kb-suite>/service/da-kb-adapter
# From kb-service only kb_service.text_structure (and chunking) is used, so it goes in without dependencies.
COPY --from=provider-kit . /opt/src/provider-kit
COPY --from=kb-service pyproject.toml /opt/src/kb-service/pyproject.toml
COPY --from=kb-service kb_service /opt/src/kb-service/kb_service
RUN pip install --no-cache-dir --no-deps /opt/src/provider-kit /opt/src/kb-service && rm -rf /opt/src

COPY pyproject.toml ./
COPY da_kb_adapter ./da_kb_adapter
RUN pip install --no-cache-dir --no-deps .

USER nobody
EXPOSE 8080
CMD ["uvicorn", "da_kb_adapter.main:app", "--host", "0.0.0.0", "--port", "8080"]
