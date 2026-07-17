import Fastify from 'fastify';
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import cors from '@fastify/cors';
import multipart from '@fastify/multipart';
import fastifyStatic from '@fastify/static';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { config } from './config.js';
import { getDb } from './db/index.js';
import { registerRoutes } from './routes.js';
import { cleanupExpiredGraphImportUploads } from './graph-import/upload-store.js';
import { failRunningGraphImportRuns } from './graph-import/repo.js';

async function main() {
  getDb(); // 初始化 schema
  failRunningGraphImportRuns('modeling 服务已重启，后台图数据导入已中断，请重新上传 Excel 并重试');
  cleanupExpiredGraphImportUploads();
  const app = Fastify({ logger: true, bodyLimit: 55 * 1024 * 1024 });
  await app.register(cors, { origin: true });
  await app.register(multipart, { limits: { fileSize: 50 * 1024 * 1024 } });
  await registerRoutes(app);
  await registerFrontend(app);
  await app.listen({ port: config.port, host: '0.0.0.0' });
  app.log.info(`知识包建模工具后端已启动：http://localhost:${config.port}`);
}

async function registerFrontend(app: FastifyInstance) {
  const distDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend-dist');
  if (!existsSync(join(distDir, 'index.html'))) {
    app.log.warn({ distDir }, '未找到前端静态产物，跳过页面托管');
    return;
  }

  await app.register(fastifyStatic, {
    root: distDir,
    prefix: '/',
    wildcard: false,
  });

  app.setNotFoundHandler((request: FastifyRequest, reply: FastifyReply) => {
    const url = request.raw.url ?? '';
    if (url.startsWith('/api/') || url.startsWith('/auth/') || url === '/health') {
      return reply.code(404).send({ error: 'Not Found' });
    }
    return reply.type('text/html').sendFile('index.html');
  });
}

main().catch((e) => {
  console.error('启动失败：', e);
  process.exit(1);
});
