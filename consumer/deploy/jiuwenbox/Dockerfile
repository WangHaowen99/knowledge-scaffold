ARG JIUWENBOX_BASE
ARG RUNTIME_PAYLOAD_IMAGE
FROM ${JIUWENBOX_BASE} AS runtime-payload

COPY requirements.txt /tmp/onto-sandbox-requirements.txt
RUN /usr/bin/python3 -m pip install \
        --break-system-packages \
        --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r /tmp/onto-sandbox-requirements.txt \
    && rm -f /tmp/onto-sandbox-requirements.txt

COPY runtime/ /app/sandbox-runtime/python/
COPY runtime-contract.json /app/sandbox-runtime/runtime-contract.json
COPY runtime-entrypoint.sh /usr/local/bin/onto-jiuwenbox-entrypoint

RUN PYTHONPATH=/app/sandbox-runtime/python \
        /usr/bin/python3 -c "import neo4j; import requests; import onto_sandbox_runtime.bounded_runner; from shared_capabilities.business_data import ProviderResult; from shared_capabilities.ontology_script_sdk import DataProviderClient"

RUN chmod 0555 /usr/local/bin/onto-jiuwenbox-entrypoint

ENTRYPOINT ["/usr/local/bin/onto-jiuwenbox-entrypoint"]
CMD ["python3", "-m", "jiuwenbox.server.launcher"]

FROM ${RUNTIME_PAYLOAD_IMAGE} AS runtime-attested

ARG JIUWENSWARM_COMMIT
ARG SANDBOX_RUNTIME_REVISION

LABEL org.opencontainers.image.title="onto-agent-jiuwenbox" \
      org.opencontainers.image.description="JiuwenBox with the Onto Agent sandbox runtime" \
      org.opencontainers.image.revision="${JIUWENSWARM_COMMIT}" \
      io.onto.sandbox-runtime.revision="${SANDBOX_RUNTIME_REVISION}"

RUN test -n "${SANDBOX_RUNTIME_REVISION}" \
    && printf '%s\n' "${SANDBOX_RUNTIME_REVISION}" \
        > /app/sandbox-runtime/python/.runtime-revision
