import { Readable } from 'node:stream';

import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

import { config } from '../config.js';
import { canReadProject, canWriteProject } from '../projects/repo.js';
import { deliverAgentProjectEvents } from './project-event-outbox.js';
import { takeAcceptanceFault } from '../acceptance/faults.js';

export function registerAgentProxyRoutes(app: FastifyInstance): void {
  app.get('/api/projects/:id/agent/state', async (request, reply) => {
    const id = projectId(request);
    if (!canReadProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    if (canWriteProject(id, request.user!.username)) {
      try {
        await deliverAgentProjectEvents(id, String(request.headers.authorization ?? ''));
      } catch (error) {
        request.log.warn({ err: error, projectId: id }, '项目事件将在下次刷新时重试');
      }
    }
    return proxyJson(request, reply, `/v1/projects/${encodeURIComponent(id)}/state`, 'GET', id);
  });

  app.post('/api/projects/:id/agent/runs', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const acceptanceFailure = takeAcceptanceMessageFailure(request, reply, id);
    if (acceptanceFailure) return acceptanceFailure;
    return proxyJson(request, reply, `/v1/projects/${encodeURIComponent(id)}/runs`, 'POST', id);
  });

  app.post('/api/projects/:id/agent/steer', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const acceptanceFailure = takeAcceptanceMessageFailure(request, reply, id);
    if (acceptanceFailure) return acceptanceFailure;
    return proxyJson(request, reply, `/v1/projects/${encodeURIComponent(id)}/steer`, 'POST', id);
  });

  app.post('/api/projects/:id/agent/issues/:issueId/resume', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const issueId = encodeURIComponent(String((request.params as any).issueId ?? ''));
    return proxyJson(
      request,
      reply,
      `/v1/projects/${encodeURIComponent(id)}/issues/${issueId}/resume`,
      'POST',
      id,
    );
  });

  app.post('/api/projects/:id/agent/cancel', async (request, reply) => (
    proxyJson(request, reply, `/v1/projects/${projectId(request)}/cancel`, 'POST')
  ));

  app.post('/api/projects/:id/agent/interactions/:interactionId/answer', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const interactionId = encodeURIComponent(String((request.params as any).interactionId ?? ''));
    return proxyJson(
      request,
      reply,
      `/v1/projects/${encodeURIComponent(id)}/interactions/${interactionId}/answer`,
      'POST',
      id,
    );
  });

  app.get('/api/projects/:id/agent/stream', async (request, reply) => {
    const id = projectId(request);
    if (!canReadProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const after = Math.max(0, Number((request.query as any)?.after) || 0);
    const abort = new AbortController();
    let bridge: Readable | null = null;
    reply.raw.once('close', () => {
      abort.abort();
      bridge?.destroy();
    });
    let upstream: Response;
    try {
      upstream = await fetch(`${config.agentBaseUrl}/v1/projects/${encodeURIComponent(id)}/stream?after=${after}`, {
        headers: authorizationHeaders(request),
        signal: abort.signal,
      });
    } catch (error) {
      request.log.error({ err: error }, 'Agent 事件流不可用');
      return reply.code(503).send({ code: 'AGENT_UNAVAILABLE', error: '建模助手暂时不可用' });
    }
    if (!upstream.ok || !upstream.body) {
      return sendUpstreamError(reply, upstream);
    }
    reply.hijack();
    reply.raw.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    });
    bridge = Readable.fromWeb(upstream.body as any);
    bridge.on('error', (error) => {
      if (abort.signal.aborted || reply.raw.destroyed
        || (error instanceof DOMException && error.name === 'AbortError')) return;
      request.log.error({ err: error }, 'Agent 事件流转发中断');
      reply.raw.destroy(error as Error);
    });
    bridge.pipe(reply.raw);
  });
}

function takeAcceptanceMessageFailure(
  request: FastifyRequest,
  reply: FastifyReply,
  projectIdValue: string,
) {
  if (!takeAcceptanceFault(request.user!.username, 'F17', `message:${projectIdValue}:${Date.now()}`)) return null;
  return reply.code(503).send({
    code: 'AGENT_RUN_CREATE_RETRYABLE',
    error: '消息暂时没有发送成功，输入内容已保留，请重试',
    retryable: true,
  });
}

async function proxyJson(
  request: FastifyRequest,
  reply: FastifyReply,
  path: string,
  method: 'GET' | 'POST' = 'GET',
  checkedProjectId?: string,
) {
  const id = checkedProjectId ?? projectId(request);
  const allowed = method === 'POST'
    ? canWriteProject(id, request.user!.username)
    : canReadProject(id, request.user!.username);
  if (!allowed) return reply.code(404).send({ error: '项目不存在' });
  let upstream: Response;
  try {
    upstream = await fetch(`${config.agentBaseUrl}${path}`, {
      method,
      headers: {
        ...authorizationHeaders(request),
        ...(method === 'POST' ? { 'content-type': 'application/json' } : {}),
      },
      ...(method === 'POST' ? { body: JSON.stringify(request.body ?? {}) } : {}),
    });
  } catch (error) {
    request.log.error({ err: error }, 'Agent 请求不可用');
    return reply.code(503).send({ code: 'AGENT_UNAVAILABLE', error: '建模助手暂时不可用' });
  }
  const payload = await upstream.json().catch(() => ({}));
  return reply.code(upstream.status).send(payload);
}

function projectId(request: FastifyRequest): string {
  return String((request.params as any).id ?? '').trim();
}

function authorizationHeaders(request: FastifyRequest): Record<string, string> {
  const authorization = String(request.headers.authorization ?? '');
  return authorization ? { authorization } : {};
}

async function sendUpstreamError(reply: FastifyReply, upstream: Response) {
  const payload = await upstream.json().catch(() => ({ error: '建模助手返回了不可读取的响应' }));
  return reply.code(upstream.status).send(payload);
}
