import { Readable } from 'node:stream';

import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

import { config } from '../config.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { MaterialPurposeReviewStore } from '../documents/material-purpose-review.js';
import { sendHttpError } from '../http-error.js';
import { canReadProject, canWriteProject } from '../projects/repo.js';
import { getDb } from '../db/index.js';
import { ChangeControlStore } from './change-control.js';
import { deliverAgentProjectEvents } from './project-event-outbox.js';
import { takeAcceptanceFault } from '../acceptance/faults.js';

const AGENT_REQUEST_TIMEOUT_MS = 30_000;

export function registerAgentProxyRoutes(app: FastifyInstance): void {
  const artifacts = getPackageArtifactStore();
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
    const pausedPlan = new ChangeControlStore(getDb()).pausedPlanSnapshot(id);
    const resumePlanId = String((request.body as any)?.resumePlanId ?? '').trim();
    if (pausedPlan && resumePlanId !== pausedPlan.id) {
      return reply.code(409).send(pausedPlanDecision(pausedPlan));
    }
    if (!pausedPlan && resumePlanId) {
      return reply.code(409).send({
        code: 'PAUSED_CHANGE_PLAN_STALE',
        error: '暂停计划状态已经变化，请刷新页面后再操作',
      });
    }
    const acceptanceFailure = takeAcceptanceMessageFailure(request, reply, id);
    if (acceptanceFailure) return acceptanceFailure;
    if (shouldStartIncrementRound(String((request.body as any)?.message ?? ''))) {
      const prepared = await prepareIncrementRound(request, id, artifacts);
      if (prepared.status >= 400) return reply.code(prepared.status).send(prepared.payload);
    }
    return proxyJson(request, reply, `/v1/projects/${encodeURIComponent(id)}/runs`, 'POST', id);
  });

  app.post('/api/projects/:id/agent/change-plans/:planId/resume', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const planId = String((request.params as any).planId ?? '').trim();
    const pausedPlan = new ChangeControlStore(getDb()).pausedPlanSnapshot(id);
    if (!pausedPlan || pausedPlan.id !== planId) {
      return reply.code(409).send({
        code: 'PAUSED_CHANGE_PLAN_STALE',
        error: '暂停计划状态已经变化，请刷新页面后再操作',
      });
    }
    const result = await agentJson(
      request,
      `/v1/projects/${encodeURIComponent(id)}/runs`,
      'POST',
      {
        message: '继续页面中明确选择的未完成建模计划。',
        baselineRevision: '',
        resumePlanId: planId,
      },
    );
    return reply.code(result.status).send(result.payload);
  });

  app.post('/api/projects/:id/agent/increment/start', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const prepared = await prepareIncrementRound(request, id, artifacts);
    return reply.code(prepared.status).send(prepared.payload);
  });

  app.post('/api/projects/:id/agent/steer', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const pausedPlan = new ChangeControlStore(getDb()).pausedPlanSnapshot(id);
    if (pausedPlan) return reply.code(409).send(pausedPlanDecision(pausedPlan));
    const acceptanceFailure = takeAcceptanceMessageFailure(request, reply, id);
    if (acceptanceFailure) return acceptanceFailure;
    if ((request.body as any)?.purposeReviewCompleted === true) {
      const progress = new MaterialPurposeReviewStore().progress(id);
      if (!progress.allConfirmed) {
        return reply.code(409).send({
          code: 'MATERIAL_PURPOSE_REVIEW_REQUIRED',
          error: progress.totalCount
            ? `仍有 ${progress.pendingCount} 份资料用途尚未确认`
            : '当前没有已完成用途确认的资料批次',
          purposeReviewProgress: progress,
        });
      }
    }
    if (shouldStartIncrementRound(String((request.body as any)?.message ?? ''))) {
      const prepared = await prepareIncrementRound(request, id, artifacts);
      if (prepared.status >= 400) return reply.code(prepared.status).send(prepared.payload);
    }
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

  app.get('/api/projects/:id/agent/constraints', async (request, reply) => {
    const id = projectId(request);
    if (!canReadProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    return proxyJson(request, reply, `/v1/projects/${encodeURIComponent(id)}/constraints`, 'GET', id);
  });

  app.post('/api/projects/:id/agent/constraints/:constraintId/revoke', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const constraintId = encodeURIComponent(String((request.params as any).constraintId ?? ''));
    return proxyJson(
      request,
      reply,
      `/v1/projects/${encodeURIComponent(id)}/constraints/${constraintId}/revoke`,
      'POST',
      id,
    );
  });

  app.post('/api/projects/:id/agent/cancel', async (request, reply) => (
    proxyJson(request, reply, `/v1/projects/${projectId(request)}/cancel`, 'POST')
  ));

  app.post('/api/projects/:id/agent/recovery/terminate-stale', async (request, reply) => {
    const id = projectId(request);
    if (!canWriteProject(id, request.user!.username)) return reply.code(404).send({ error: '项目不存在' });
    const upstream = await agentJson(
      request,
      `/v1/projects/${encodeURIComponent(id)}/recovery/terminate-stale`,
      'POST',
      request.body,
    );
    if (upstream.status >= 400 && upstream.payload.code !== 'RUN_NOT_ACTIVE') {
      return reply.code(upstream.status).send(upstream.payload);
    }
    try {
      const terminatedRunId = typeof upstream.payload.terminatedRunId === 'string'
        ? upstream.payload.terminatedRunId
        : undefined;
      const cancelled = new ChangeControlStore(getDb()).safeTerminateActivePlansForProject({
        projectId: id,
        actor: request.user!.username,
        ...(terminatedRunId ? { runId: terminatedRunId } : {}),
      });
      return reply.code(200).send({
        ...upstream.payload,
        cancelledActivePlans: cancelled,
        ...(upstream.status >= 400 ? { reconciledAfterPythonShortCircuit: true } : {}),
      });
    } catch (error) {
      request.log.error({ err: error, projectId: id }, '安全终止 Node 侧活动计划失败');
      return reply.code(500).send({
        code: 'PLAN_SAFE_TERMINATE_FAILED',
        error: '失效运行已终止，但旧计划清理失败，请重试',
      });
    }
  });

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
      return sendUpstreamError(request, reply, upstream);
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

function pausedPlanDecision(pausedPlan: Record<string, unknown>) {
  return {
    code: 'PAUSED_CHANGE_PLAN_DECISION_REQUIRED',
    error: '当前项目还有暂停的建模计划，请先选择继续该计划或结束后重新规划',
    pausedPlan,
    requiresUserDecision: true,
  };
}

export function incrementRoundRequested(message: string): boolean {
  const text = String(message || '').trim();
  if (!text || /(?:为什么|为何|怎么|如何|是否|有没有|是什么|吗\s*[？?]?|[？?])/u.test(text)) return false;
  return /(?:开始|启动|开启|进入|发起|继续)\s*.{0,8}(?:增量(?:建模|修改|迭代)?|新一轮迭代)|(?:增量建模|增量迭代)\s*.{0,8}(?:开始|启动|继续)/u.test(text);
}

/**
 * 已发布项目即使没有写出“增量”，点名文档并要求新增规则也必须进入增量轮次。
 * 同时具备文档引用和规则变更动作，普通阅读或咨询才不会误触发。
 */
export function documentRuleIncrementRequested(message: string): boolean {
  const text = String(message || '').trim();
  if (!text || /(?:为什么|为何|怎么|如何|是否|有没有|是什么|能否|可否|请问|吗\s*[？?]?|[？?])/u.test(text)) {
    return false;
  }
  const hasDocumentReference = Boolean(
    /《[^》]{1,200}》/u.test(text)
    || /(?:[\w\u4e00-\u9fff][\w\u4e00-\u9fff ._-]{0,120}\.(?:docx?|pdf|xlsx?|csv|md|txt))/iu.test(text)
    || /(?:这个|该|指定的?|上述)\s*(?:文档|文件|资料)/u.test(text)
    || /(?:读取|查看|参考|根据|按照|依据)\s*.{0,80}(?:文档|文件|资料)/u.test(text),
  );
  const hasRuleMutation = Boolean(
    /(?:新增|添加|补充|增加|制定|写入|修改|更新|完善)\s*(?:一条|一项|新的?|相关的?)?\s*(?:业务)?规则/u.test(text)
    || /(?:业务)?规则\s*(?:新增|添加|补充|增加|制定|写入|修改|更新|完善)/u.test(text),
  );
  return hasDocumentReference && hasRuleMutation;
}

function shouldStartIncrementRound(message: string): boolean {
  return incrementRoundRequested(message) || documentRuleIncrementRequested(message);
}

async function prepareIncrementRound(
  request: FastifyRequest,
  projectIdValue: string,
  artifacts: ReturnType<typeof getPackageArtifactStore>,
): Promise<{ status: number; payload: Record<string, unknown> }> {
  let current;
  try {
    current = await artifacts.current(projectIdValue);
  } catch (error) {
    request.log.error({ err: error, projectId: projectIdValue }, '读取增量基线失败');
    return { status: 503, payload: { code: 'INCREMENT_BASELINE_UNAVAILABLE', error: '暂时无法读取正式知识包基线' } };
  }
  if (!current || current.spec.status !== 'released') {
    return {
      status: 409,
      payload: {
        code: 'INCREMENT_REQUIRES_RELEASED_PACKAGE',
        error: '当前项目还没有正式发布的知识包，不能开始增量建模',
      },
    };
  }
  let upstream: Response;
  try {
    upstream = await fetch(
      `${config.agentBaseUrl}/v1/projects/${encodeURIComponent(projectIdValue)}/increment/start`,
      {
        method: 'POST',
        headers: { ...authorizationHeaders(request), 'content-type': 'application/json' },
        body: JSON.stringify({
          baselineRevision: current.revision.oid,
          packageVersion: current.packageVersion,
        }),
      },
    );
  } catch (error) {
    request.log.error({ err: error, projectId: projectIdValue }, '增量轮次入口不可用');
    return { status: 503, payload: { code: 'AGENT_UNAVAILABLE', error: '建模助手暂时不可用' } };
  }
  const payload = (await upstream.json().catch(() => ({}))) as Record<string, unknown>;
  return {
    status: upstream.status,
    payload: payload && typeof payload === 'object'
      ? payload as Record<string, unknown>
      : { code: 'AGENT_PROTOCOL_INVALID', error: '建模助手返回了不可读取的响应' },
  };
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

async function agentJson(
  request: FastifyRequest,
  path: string,
  method: 'GET' | 'POST' = 'GET',
  body?: unknown,
): Promise<{ status: number; payload: Record<string, unknown> }> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), AGENT_REQUEST_TIMEOUT_MS);
  try {
    const upstream = await fetch(`${config.agentBaseUrl}${path}`, {
      method,
      headers: {
        ...authorizationHeaders(request),
        ...(method === 'POST' ? { 'content-type': 'application/json' } : {}),
      },
      ...(method === 'POST' ? { body: JSON.stringify(body ?? {}) } : {}),
      signal: controller.signal,
    });
    const payload = (await upstream.json().catch(() => ({}))) as Record<string, unknown>;
    return { status: upstream.status, payload };
  } catch (error) {
    request.log.error({ err: error }, 'Agent 请求不可用');
    return { status: 503, payload: { code: 'AGENT_UNAVAILABLE', error: '建模助手暂时不可用' } };
  } finally {
    clearTimeout(timeout);
  }
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
  const result = await agentJson(request, path, method, method === 'POST' ? request.body : undefined);
  if (result.status >= 500) {
    return sendAgentServiceError(request, reply, result.status);
  }
  return reply.code(result.status).send(result.payload);
}

function projectId(request: FastifyRequest): string {
  return String((request.params as any).id ?? '').trim();
}

function authorizationHeaders(request: FastifyRequest): Record<string, string> {
  const authorization = String(request.headers.authorization ?? '');
  return authorization ? { authorization } : {};
}

async function sendUpstreamError(
  request: FastifyRequest,
  reply: FastifyReply,
  upstream: Response,
) {
  const payload = await upstream.json().catch(() => ({ error: '建模助手返回了不可读取的响应' }));
  if (upstream.status >= 500 || upstream.ok) {
    return sendAgentServiceError(request, reply, upstream.ok ? 502 : upstream.status);
  }
  return reply.code(upstream.status).send(payload);
}

function sendAgentServiceError(
  request: FastifyRequest,
  reply: FastifyReply,
  upstreamStatus: number,
): FastifyReply {
  const statusCode = upstreamStatus === 504 ? 504 : upstreamStatus === 503 ? 503 : 502;
  return sendHttpError(reply, new Error(`Agent upstream HTTP ${upstreamStatus}`), {
    statusCode,
    code: 'AGENT_UNAVAILABLE',
    message: statusCode === 504
      ? '建模助手响应超时，请稍后重试'
      : '建模助手暂时不可用，请稍后重试',
    logMessage: `${request.method} ${request.url} Agent 上游请求失败`,
  });
}
