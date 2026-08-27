import Fastify from 'fastify';

import { config } from './config.js';
import { getDb } from './db/index.js';
import { registerProjectMcpTransportRoutes } from './mcp/transport.js';

async function main() {
  getDb();
  const app = Fastify({ logger: true, bodyLimit: 2 * 1024 * 1024 });
  app.get('/health', async () => ({ status: 'ok' }));
  registerProjectMcpTransportRoutes(app);
  await app.listen({ port: config.mcp.port, host: config.mcp.host });
  app.log.info(`建模 MCP 服务已启动：${config.mcp.host}:${config.mcp.port}`);
}

main().catch((error) => {
  console.error('MCP 服务启动失败：', error);
  process.exit(1);
});
