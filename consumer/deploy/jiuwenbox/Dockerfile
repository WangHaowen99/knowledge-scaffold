ARG JIUWENBOX_BASE
FROM ${JIUWENBOX_BASE}

ARG JIUWENSWARM_COMMIT

LABEL org.opencontainers.image.title="onto-agent-jiuwenbox" \
      org.opencontainers.image.description="JiuwenBox with the Onto Agent sandbox runtime" \
      org.opencontainers.image.revision="${JIUWENSWARM_COMMIT}"

COPY requirements.txt /tmp/onto-sandbox-requirements.txt
RUN /usr/bin/python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r /tmp/onto-sandbox-requirements.txt \
    && rm -f /tmp/onto-sandbox-requirements.txt

COPY runtime/ /app/sandbox-runtime/python/

RUN PYTHONPATH=/app/sandbox-runtime/python \
    /usr/bin/python3 -c "import requests; from shared_capabilities.business_data import ProviderResult; from shared_capabilities.ontology_script_sdk import DataProviderClient"
