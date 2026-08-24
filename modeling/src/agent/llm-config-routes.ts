import { timingSafeEqual } from 'node:crypto';

import type { FastifyInstance, FastifyRequest } from 'fastify';

import { config } from '../config.js';
import { getLlmSettings, getLlmSource } from '../settings/repo.js';

function serviceAuthorized(request: FastifyRequest): boolean {
  const expected = config.modelingServiceToken.trim();
  const supplied = String(request.headers['x-modeling-service-token'] ?? '').trim();
  if (expected.length < 32 || supplied.length !== expected.length) return false;
  return timingSafeEqual(Buffer.from(supplied), Buffer.from(expected));
}

export function registerAgentLlmConfigRoutes(app: FastifyInstance): void {
  app.get('/internal/agent/llm-config', async (request, reply) => {
    reply.header('cache-control', 'no-store');
    if (!serviceAuthorized(request)) {
      return reply.code(403).send({ code: 'AGENT_SERVICE_FORBIDDEN', error: 'Agent 服务认证失败' });
    }
    const username = request.user!.username;
    const llm = getLlmSettings(username);
    if (!llm.baseUrl || !llm.model) {
      return reply.code(409).send({
        code: 'LLM_NOT_CONFIGURED',
        error: '尚未配置建模模型，请由 admin 在配置中心设置平台默认模型',
      });
    }
    return {
      llm: {
        baseUrl: llm.baseUrl,
        model: llm.model,
        apiKey: llm.apiKey,
        maxTokens: llm.maxTokens,
        contextWindow: llm.contextWindow,
        source: getLlmSource(username),
      },
    };
  });
}
