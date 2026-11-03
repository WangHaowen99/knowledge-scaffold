# DeepAnalyze knowledge pipeline (TypeScript backend), backend only.
#
# The DeepAnalyze source is not part of this repository; the build context is a
# DeepAnalyze checkout given by DA_TS_SRC (see ../kbsuite.sh build-da-kb). No
# frontend, no Python document services: txt/md/html/xlsx/csv/json parse
# natively; pdf/docx/pptx need docling or a MinerU service configured in DA.
FROM docker.1ms.run/oven/bun:1.3-slim

ARG NPM_REGISTRY=https://registry.npmmirror.com
ENV BUN_CONFIG_REGISTRY=${NPM_REGISTRY}

WORKDIR /app
COPY package.json bun.lock ./
# sharp is imported at startup and needs its optional platform package, so optional deps stay.
# Packages only used by agent tools this deployment does not run are dropped.
RUN bun install --frozen-lockfile --production --ignore-scripts \
 && rm -rf node_modules/onnxruntime-node node_modules/onnxruntime-common node_modules/better-sqlite3 \
 && rm -rf /root/.bun/install/cache

COPY src ./src
COPY da-assets ./da-assets

RUN mkdir -p /app/data /app/config
ENV NODE_ENV=production \
    PORT=21000 \
    DATA_DIR=/app/data \
    DA_AUTH_MODE=none \
    DA_SKIP_WIZARD=true \
    HF_ENDPOINT=
EXPOSE 21000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD bun -e "fetch('http://127.0.0.1:21000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["bun", "run", "src/main.ts"]
