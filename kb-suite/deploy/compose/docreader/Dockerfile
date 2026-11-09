# docreader with WeKnora's newer parser sources (source locators: PDF page/bbox,
# Excel sheet/rows) laid over the released image. Only valid while the two
# share pyproject.toml / uv.lock; kbsuite.sh build-docreader checks that.
ARG BASE_IMAGE=wechatopenai/weknora-docreader:latest
FROM ${BASE_IMAGE}
ARG WEKNORA_COMMIT=unknown
LABEL kb-suite.weknora-commit=${WEKNORA_COMMIT} \
      org.opencontainers.image.source=https://github.com/Tencent/WeKnora
COPY docreader /app/docreader
