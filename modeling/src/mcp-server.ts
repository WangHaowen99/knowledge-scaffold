import { randomUUID } from 'node:crypto';

import Fastify from 'fastify';

import { config } from './config.js';
import { getDb } from './db/index.js';
import {
  markModelingMcpRuntimeRunning,
  markModelingMcpRuntimeStopped,
} from './mcp/service.js';
import {
  installMcpHttpErrorHandler,
  registerProjectMcpTransportRoutes,
} from './mcp/transport.js';

const HEARTBEAT_MS = 10_000;

export interface ModelingMcpServerHandle {
  close: (detail?: string) => Promise<void>;
}

export async function startModelingMcpServer(): Promise<ModelingMcpServerHandle> {
  getDb();
  const app = Fastify({ logger: true, bodyLimit: 2 * 1024 * 1024 });
  installMcpHttpErrorHandler(app);
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
  let closed = false;
  const close = async (detail = 'modeling 服务停止') => {
    if (closed) return;
    closed = true;
    clearInterval(heartbeat);
    try {
      markModelingMcpRuntimeStopped(instanceId, detail);
    } finally {
      await app.close();
    }
  };
  app.log.info(`建模 MCP 服务已启动：${config.mcp.host}:${config.mcp.port}`);
  return { close };
}
