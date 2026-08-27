import { randomUUID } from 'node:crypto';

import Fastify from 'fastify';

import { config } from './config.js';
import { getDb } from './db/index.js';
import {
  markModelingMcpRuntimeRunning,
  markModelingMcpRuntimeStopped,
} from './mcp/service.js';
import { registerProjectMcpTransportRoutes } from './mcp/transport.js';

const HEARTBEAT_MS = 10_000;

async function main() {
  getDb();
  const app = Fastify({ logger: true, bodyLimit: 2 * 1024 * 1024 });
  app.get('/health', async () => ({ status: 'ok' }));
  registerProjectMcpTransportRoutes(app);
  await app.listen({ port: config.mcp.port, host: config.mcp.host });
  const instanceId = `mcp_${randomUUID().replaceAll('-', '')}`;
  markModelingMcpRuntimeRunning(instanceId, `${config.mcp.host}:${config.mcp.port}`);
  const heartbeat = setInterval(() => {
    try {
      markModelingMcpRuntimeRunning(instanceId, `${config.mcp.host}:${config.mcp.port}`);
    } catch (error) {
      app.log.error({ err: error }, 'MCP 心跳写入失败');
    }
  }, HEARTBEAT_MS);
  heartbeat.unref();
  const shutdown = async (signal: string) => {
    clearInterval(heartbeat);
    try {
      markModelingMcpRuntimeStopped(instanceId, `收到 ${signal}`);
    } finally {
      await app.close();
    }
  };
  process.once('SIGTERM', () => { void shutdown('SIGTERM'); });
  process.once('SIGINT', () => { void shutdown('SIGINT'); });
  app.log.info(`建模 MCP 服务已启动：${config.mcp.host}:${config.mcp.port}`);
}

main().catch((error) => {
  console.error('MCP 服务启动失败：', error);
  process.exit(1);
});
