import type { FastifyReply, FastifyRequest } from 'fastify';
import { timingSafeEqual } from 'node:crypto';
import { decodeJwt } from 'jose';
import { getAuthClient } from './client.js';
import type { AuthUser } from '../types.js';
import { enterLlmUser } from '../llm/user-context.js';
import { authenticateAgentRunCredential } from '../agent/run-credential.js';
import { config } from '../config.js';
import { getProject } from '../projects/repo.js';

declare module 'fastify' {
  interface FastifyRequest {
    user?: AuthUser;
    internalCleanup?: boolean;
    ontoAdminProjectId?: string;
  }
}

// preHandler：解析 Bearer token，经 AuthClient 本地 JWKS 验签，注入 request.user。
// 校验通过存缓存（同一 token 短期内不重复验签）。
const cache = new Map<string, { user: AuthUser; exp: number }>();
const TTL_MS = 60_000;

function cacheExpiry(token: string, now: number): number {
  try {
    const tokenExpiry = Number(decodeJwt(token).exp) * 1_000;
    return Number.isFinite(tokenExpiry) ? Math.min(now + TTL_MS, tokenExpiry) : now;
  } catch {
    return now;
  }
}

// The caller's role comes from verified JWT claims, never an Onto role header.
// Matching the external id confines delegation to the requested Onto project.
export function isOntoAdminRequest(req: FastifyRequest, externalId: string | undefined): boolean {
  return req.user?.role === 'admin'
    && typeof externalId === 'string'
    && /^onto:[A-Za-z0-9_-]+$/u.test(externalId)
    && req.headers['x-onto-project-id'] === externalId;
}

function delegateOntoMcpAdmin(req: FastifyRequest): void {
  const path = req.url.split('?', 1)[0];
  if (!/^\/api\/projects\/[^/]+\/mcp\/(?:runtime\/status|package|tokens(?:\/[^/]+)?)$/u.test(path)) return;
  const project = getProject(String((req.params as { id?: string }).id ?? ''));
  if (project && isOntoAdminRequest(req, project.externalId)) {
    req.ontoAdminProjectId = project.id;
  }
}

export async function requireAuth(req: FastifyRequest, reply: FastifyReply) {
  // Onto imports use a deployment-only service credential.  It is accepted
  // only on the project/MCP management API; normal user JWT authentication
  // remains unchanged for every other request.
  const serviceToken = String(req.headers['x-modeling-service-token'] ?? '').trim();
  const servicePath = req.url.split('?', 1)[0];
  const serviceEndpoint = req.method === 'POST' && servicePath === '/api/projects'
    || req.method === 'GET' && servicePath === '/api/projects/by-external-id'
    || req.method === 'PUT' && /^\/api\/projects\/[^/]+\/members\/sync$/u.test(servicePath)
    || req.method === 'GET' && /^\/api\/projects\/[^/]+\/mcp\/runtime\/status$/u.test(servicePath)
    || ['GET', 'POST'].includes(req.method) && /^\/api\/projects\/[^/]+\/mcp\/tokens$/u.test(servicePath)
    || req.method === 'POST' && /^\/api\/projects\/[^/]+\/mcp\/package$/u.test(servicePath)
    || req.method === 'DELETE' && /^\/api\/projects\/[^/]+\/mcp\/tokens\/[^/]+$/u.test(servicePath)
    || req.method === 'DELETE' && /^\/api\/projects\/[^/]+$/u.test(servicePath);
  const expectedServiceToken = config.modelingServiceToken.trim();
  const serviceTokenMatches = Buffer.byteLength(serviceToken) === Buffer.byteLength(expectedServiceToken)
    && expectedServiceToken.length >= 32
    && timingSafeEqual(Buffer.from(serviceToken), Buffer.from(expectedServiceToken));
  // Agent LLM config already requires both a user/run identity below and its
  // own service-token check in the route. Do not intercept that existing flow.
  const agentLlmConfig = req.method === 'GET' && servicePath === '/internal/agent/llm-config';
  if (serviceToken && !agentLlmConfig) {
    if (!serviceTokenMatches) return reply.code(401).send({ error: '服务凭证无效' });
    if (!serviceEndpoint) return reply.code(403).send({ error: '服务凭证不能访问此接口' });
    const externalId = String(req.headers['x-onto-project-id'] ?? '').trim();
    if (!/^onto:[A-Za-z0-9_-]+$/u.test(externalId)) {
      return reply.code(403).send({ error: '缺少有效的 Onto 项目归属' });
    }
    const body = (req.body ?? {}) as Record<string, unknown>;
    const projectId = (req.params as { id?: string }).id;
    const project = projectId ? getProject(projectId) : undefined;
    if (projectId && !project) return reply.code(404).send({ error: '项目不存在' });
    if (project && project.externalId !== externalId) {
      return reply.code(403).send({ error: '服务凭证不能访问此项目' });
    }
    if (req.method === 'POST' && servicePath === '/api/projects' && body.external_id !== externalId
      || servicePath === '/api/projects/by-external-id'
        && (req.query as { external_id?: string }).external_id !== externalId) {
      return reply.code(403).send({ error: 'Onto 项目归属不一致' });
    }
    // Cleanup retries have no user session. The exact external id and existing
    // delete route's confirmation / nonempty-project checks still apply.
    if (req.method === 'DELETE' && /^\/api\/projects\/[^/]+$/u.test(servicePath)) {
      if (body.external_id !== externalId) {
        return reply.code(403).send({ error: 'Onto 项目归属不一致' });
      }
      const username = String(req.headers['x-onto-username'] ?? '').trim();
      if (username && username !== project!.owner) {
        return reply.code(403).send({ error: '只有项目所有者可以删除项目' });
      }
      req.user = { username: project!.owner, role: 'service' };
      return;
    }
    const ontoUsername = String(req.headers['x-onto-username'] ?? '').trim();
    if (!ontoUsername) return reply.code(403).send({ error: '缺少 Onto 用户身份' });
    req.user = { username: ontoUsername, role: 'service' };
    // Only a service-authenticated request bound to this exact Onto project
    // may delegate admin access. Never promote the user's global role.
    if (project && servicePath.includes('/mcp/') && req.headers['x-onto-role'] === 'admin') {
      req.ontoAdminProjectId = project.id;
    }
    return;
  }
  const cleanupToken = String(req.headers['x-modeling-cleanup-token'] ?? '').trim();
  const requestPath = req.url.split('?', 1)[0];
  if (req.method === 'DELETE' && /^\/api\/projects\/[^/]+$/.test(requestPath)
    && cleanupToken && config.mcp.cleanupToken && cleanupToken === config.mcp.cleanupToken) {
    req.internalCleanup = true;
    req.user = { username: '__modeling_cleanup__', role: 'service' };
    return;
  }
  const runCredential = String(req.headers['x-modeling-agent-credential'] ?? '').trim();
  if ((req.url.startsWith('/internal/agent/tools/')
    || req.url.startsWith('/internal/agent/llm-config')
    || req.url.startsWith('/internal/agent/acceptance/')
    || req.url.startsWith('/internal/agent/runs/'))
    && runCredential) {
    const internalUser = authenticateAgentRunCredential(
      String(req.headers['x-modeling-project'] ?? '').trim(),
      String(req.headers['x-modeling-run'] ?? '').trim(),
      runCredential,
    );
    if (internalUser) {
      req.user = internalUser;
      enterLlmUser(internalUser.username);
      return;
    }
  }
  const header = req.headers.authorization ?? '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (!token) {
    return reply.code(401).send({ error: '缺少认证令牌' });
  }
  const cached = cache.get(token);
  if (cached && cached.exp > Date.now()) {
    req.user = cached.user;
    delegateOntoMcpAdmin(req);
    enterLlmUser(cached.user.username);
    return;
  }
  const user = await getAuthClient().verify(token);
  if (!user) {
    cache.delete(token);
    return reply.code(401).send({ error: '认证失败或令牌已过期' });
  }
  const now = Date.now();
  const exp = cacheExpiry(token, now);
  if (exp <= now) {
    cache.delete(token);
    return reply.code(401).send({ error: '认证失败或令牌已过期' });
  }
  cache.set(token, { user, exp });
  req.user = user;
  delegateOntoMcpAdmin(req);
  enterLlmUser(user.username);
}
