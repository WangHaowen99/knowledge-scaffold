# JiuwenSwarm 运行镜像：直接安装 PyPI 发布版，不改 Swarm 源码。
# 额外装上 DeepAnalyze harness provider（构建时用 --build-context da-harness=<kb-suite>/harness/da-harness）。
FROM docker.1ms.run/library/python:3.11-slim

ARG WORKSWARM_VERSION=0.2.7
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "workswarm==${WORKSWARM_VERSION}"

COPY --from=da-harness pyproject.toml /opt/src/da-harness/pyproject.toml
COPY --from=da-harness deepanalyze_harness /opt/src/da-harness/deepanalyze_harness
RUN pip install --no-cache-dir --no-deps /opt/src/da-harness \
 && python -c "import deepanalyze_harness, openjiuwen.harness_protocol"

COPY entrypoint.sh /usr/local/bin/swarm-entrypoint.sh
COPY configure_swarm.py /usr/local/bin/configure_swarm.py
RUN chmod +x /usr/local/bin/swarm-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/swarm-entrypoint.sh"]
