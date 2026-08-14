import type { FastifyReply, FastifyRequest } from 'fastify';
import { decodeJwt } from 'jose';
import { getAuthClient } from './client.js';
import type { AuthUser } from '../types.js';
import { enterLlmUser } from '../llm/user-context.js';

declare module 'fastify' {
  interface FastifyRequest {
    user?: AuthUser;
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

export async function requireAuth(req: FastifyRequest, reply: FastifyReply) {
  const header = req.headers.authorization ?? '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (!token) {
    reply.code(401).send({ error: '缺少认证令牌' });
    return;
  }
  const cached = cache.get(token);
  if (cached && cached.exp > Date.now()) {
    req.user = cached.user;
    enterLlmUser(cached.user.username);
    return;
  }
  const user = await getAuthClient().verify(token);
  if (!user) {
    cache.delete(token);
    reply.code(401).send({ error: '认证失败或令牌已过期' });
    return;
  }
  const now = Date.now();
  const exp = cacheExpiry(token, now);
  if (exp <= now) {
    cache.delete(token);
    reply.code(401).send({ error: '认证失败或令牌已过期' });
    return;
  }
  cache.set(token, { user, exp });
  req.user = user;
  enterLlmUser(user.username);
}
