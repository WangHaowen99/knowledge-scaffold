import Fastify from 'fastify';
import cors from '@fastify/cors';
import multipart from '@fastify/multipart';
import { config } from './config.js';
import { getDb } from './db/index.js';
import { registerRoutes } from './routes.js';

async function main() {
  getDb(); // 初始化 schema
  const app = Fastify({ logger: true, bodyLimit: 20 * 1024 * 1024 });
  await app.register(cors, { origin: true });
  await app.register(multipart, { limits: { fileSize: 30 * 1024 * 1024 } });
  await registerRoutes(app);
  await app.listen({ port: config.port, host: '0.0.0.0' });
  app.log.info(`知识包建模工具后端已启动：http://localhost:${config.port}`);
}

main().catch((e) => {
  console.error('启动失败：', e);
  process.exit(1);
});
