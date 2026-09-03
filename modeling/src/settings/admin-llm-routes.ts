import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { requireAuth } from '../auth/middleware.js';
import { DEFAULT_LLM_CONTEXT_WINDOW, DEFAULT_LLM_MAX_TOKENS } from '../config.js';
import { testLlmConnection } from './llm-connection-test.js';
import {
  deleteLlmDefaultSettings,
  getLlmDefaultSettings,
  getPublicLlmDefaults,
  isLlmPurpose,
  LlmDefaultRevisionConflictError,
  LlmDefaultValidationError,
  saveLlmDefaultSettings,
  validateLlmDefaultDraft,
} from './llm-defaults-repo.js';
import {
  llmConfigEncryptionKeyMode,
  LlmConfigEncryptionKeyError,
} from './llm-secret-codec.js';

function requireAdmin(request: FastifyRequest, reply: FastifyReply): boolean {
  // 外层 requireAuth 已发送 401 时 Fastify 仍可能进入 handler；不得再次写 403。
  if (reply.sent) return false;
  if (request.user?.role !== 'admin') {
    reply.code(403).send({ error: '仅 admin 可以管理平台 LLM 默认配置' });
    return false;
  }
  return true;
}

function expectedRevision(value: unknown): number | undefined {
  return value === undefined || value === null || value === '' ? undefined : Number(value);
}

function sendLlmDefaultError(reply: FastifyReply, error: unknown) {
  if (error instanceof LlmDefaultValidationError) return reply.code(400).send({ error: error.message });
  if (error instanceof LlmDefaultRevisionConflictError) return reply.code(409).send({ error: error.message });
  if (error instanceof LlmConfigEncryptionKeyError) return reply.code(503).send({ error: error.message });
  throw error;
}

/** 注册平台 LLM 默认配置接口；独立使用时可让本模块挂载 requireAuth。 */
export function registerAdminLlmRoutes(app: FastifyInstance, authenticate = false): void {
  if (authenticate) app.addHook('preHandler', requireAuth);
  app.get('/api/admin/llm-defaults', async (request, reply) => {
    if (!requireAdmin(request, reply)) return;
    return {
      defaults: getPublicLlmDefaults(),
      encryptionKeyConfigured: true,
      encryptionKeyMode: llmConfigEncryptionKeyMode(),
    };
  });

  app.put('/api/admin/llm-defaults', async (request, reply) => {
    if (!requireAdmin(request, reply)) return;
    const body = (request.body ?? {}) as Record<string, unknown>;
    if (!isLlmPurpose(body.purpose)) {
      return reply.code(400).send({ error: 'purpose 必须是 main 或 task' });
    }
    try {
      if (body.enabled === false) {
        deleteLlmDefaultSettings(body.purpose, expectedRevision(body.expectedRevision));
      } else {
        const saved = getLlmDefaultSettings(body.purpose);
        saveLlmDefaultSettings({
          purpose: body.purpose,
          baseUrl: String(body.baseUrl ?? ''),
          model: String(body.model ?? ''),
          maxTokens: Number(body.maxTokens),
          contextWindow: body.contextWindow === undefined
            ? (saved?.contextWindow ?? DEFAULT_LLM_CONTEXT_WINDOW)
            : Number(body.contextWindow),
          thinkingMode: body.thinkingMode,
          reasoningEffort: body.reasoningEffort,
          extraBodyJson: body.extraBodyJson,
          apiKey: body.apiKey === undefined ? undefined : String(body.apiKey),
          apiKeyClear: body.apiKeyClear === true,
          expectedRevision: expectedRevision(body.expectedRevision),
        }, request.user!.username);
      }
      return { ok: true, defaults: getPublicLlmDefaults() };
    } catch (error) {
      return sendLlmDefaultError(reply, error);
    }
  });

  app.post('/api/admin/llm-defaults/test', async (request, reply) => {
    if (!requireAdmin(request, reply)) return;
    const body = (request.body ?? {}) as Record<string, unknown>;
    if (!isLlmPurpose(body.purpose)) {
      return reply.code(400).send({ error: 'purpose 必须是 main 或 task' });
    }
    try {
      const saved = getLlmDefaultSettings(body.purpose);
      const draft = {
        baseUrl: body.baseUrl === undefined ? (saved?.baseUrl ?? '') : String(body.baseUrl),
        model: body.model === undefined ? (saved?.model ?? '') : String(body.model),
        maxTokens: body.maxTokens === undefined
          ? (saved?.maxTokens ?? DEFAULT_LLM_MAX_TOKENS)
          : Number(body.maxTokens),
        contextWindow: body.contextWindow === undefined
          ? (saved?.contextWindow ?? DEFAULT_LLM_CONTEXT_WINDOW)
          : Number(body.contextWindow),
        thinkingMode: body.thinkingMode === undefined ? saved?.thinkingMode : body.thinkingMode,
        reasoningEffort: body.reasoningEffort === undefined ? saved?.reasoningEffort : body.reasoningEffort,
        extraBodyJson: body.extraBodyJson === undefined ? saved?.extraBodyJson : body.extraBodyJson,
      };
      validateLlmDefaultDraft(draft);
      const apiKey = body.apiKeyClear === true
        ? ''
        : (body.apiKey === undefined || body.apiKey === '' ? saved?.apiKey : String(body.apiKey));
      return testLlmConnection({ ...draft, apiKey });
    } catch (error) {
      return sendLlmDefaultError(reply, error);
    }
  });
}
