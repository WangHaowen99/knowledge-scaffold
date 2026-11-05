# REA 静态分析旁路容器（ADR-0082；接口见 docs/plugin-architecture/design/evidence-collection.md §5）。
# 构建上下文就是这个目录：docker build -t onto-modeling-rea-sidecar .
FROM node:22.23.4-slim

ARG NPM_REGISTRY=https://registry.npmmirror.com

# procps：REA 在 Linux 上要 ps；tini 做 1 号进程，回收取消任务时被杀进程留下的僵尸
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/rea-sidecar
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund --registry "$NPM_REGISTRY" \
    && npm cache clean --force
COPY server.mjs ./
COPY src ./src

# /scratch 运行时挂命名卷；先建好归 node，否则卷第一次挂上时 node 写不进去
RUN mkdir -p /scratch && chown node:node /scratch

USER node
ENV NODE_ENV=production REA_SIDECAR_SCRATCH=/scratch
EXPOSE 8130
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8130/health').then((r) => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "server.mjs"]
