# 知识包建模工具后端。node:sqlite 需 Node 22+（本镜像用 22 LTS）。
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY --from=build /app/dist ./dist
EXPOSE 5100
# node:sqlite 在 22 LTS 需实验开关；24+ 稳定后可移除。
CMD ["node", "--experimental-sqlite", "dist/server.js"]
