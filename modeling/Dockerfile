# 知识包建模工具。node:sqlite 需 Node 22+（本镜像用 22 LTS）。
FROM node:22-slim AS backend-build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-slim AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend ./
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY --from=backend-build /app/dist ./dist
COPY --from=frontend-build /app/dist ./frontend-dist
EXPOSE 5100 5188
# node:sqlite 在 22 LTS 需实验开关；24+ 稳定后可移除。
CMD ["node", "--experimental-sqlite", "dist/server.js"]
