import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import { generateKeyPair, exportJWK, SignJWT, type CryptoKey } from 'jose';

// 路由层状态码映射的集成测试：真实 registerRoutes + app.inject，上游用假 auth-server。
// config 在 import 时读 process.env，因此先起假服务、设好 env，再动态 import routes。
const ISS = 'http://auth-server:5056';
const AUD = 'meta-wise';
const KID = 'route-test-key';

let privateKey: CryptoKey;
let upstream: Server;
let app: FastifyInstance;

// status: -1 表示直接掐断连接（模拟网络错）
const routes = new Map<string, { status: number; body?: string; contentType?: string }>();

before(async () => {
  const pair = await generateKeyPair('RS256');
  privateKey = pair.privateKey;
  const jwk = await exportJWK(pair.publicKey);
  routes.set('GET /jwks.json', {
    status: 200,
    body: JSON.stringify({ keys: [{ ...jwk, kid: KID, alg: 'RS256', use: 'sig' }] }),
  });

  upstream = createServer((req, res) => {
    req.on('data', () => {});
    req.on('end', () => {
      const route = routes.get(`${req.method} ${req.url}`);
      if (!route) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end('{"error":"not found"}');
        return;
      }
      if (route.status === -1) { req.socket.destroy(); return; }
      res.writeHead(route.status, { 'Content-Type': route.contentType ?? 'application/json' });
      res.end(route.body ?? '{}');
    });
  });
  await new Promise<void>((r) => upstream.listen(0, '127.0.0.1', r));
  const addr = upstream.address() as { port: number };

  process.env.AUTH_ISSUER_URL = `http://127.0.0.1:${addr.port}`;
  process.env.AUTH_JWT_ISSUER = ISS;
  process.env.AUTH_JWT_AUDIENCE = AUD;
  process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'mtool-route-test-'));

  const { registerRoutes } = await import('../src/routes.js');
  app = Fastify({ logger: false });
  await app.register(registerRoutes);
  await app.ready();
});

after(async () => {
  await app.close();
  await new Promise<void>((r) => upstream.close(() => r()));
});

function signToken(claims: Record<string, unknown> = {}) {
  return new SignJWT({ typ: 'access', role: 'user', ver: 1, pwd_cr: false, ...claims })
    .setProtectedHeader({ alg: 'RS256', kid: KID })
    .setIssuedAt()
    .setIssuer(ISS)
    .setAudience(AUD)
    .setSubject('alice')
    .setExpirationTime('15m')
    .sign(privateKey);
}

// ---------------- /auth/login ----------------

test('login：缺参 → 400', async () => {
  const res = await app.inject({ method: 'POST', url: '/auth/login', payload: { username: 'a' } });
  assert.equal(res.statusCode, 400);
});

test('login 成功 → 200 {token, user, refresh_token, expires_in}', async () => {
  const token = await signToken();
  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: token, refresh_token: 'rt-1', token_type: 'Bearer', expires_in: 900 }),
  });
  const res = await app.inject({ method: 'POST', url: '/auth/login', payload: { username: 'alice', password: 'pw' } });
  assert.equal(res.statusCode, 200);
  const body = res.json();
  assert.deepEqual(Object.keys(body).sort(), ['expires_in', 'refresh_token', 'token', 'user']);
  assert.deepEqual(body.user, { username: 'alice', role: 'user' });
});

test('login：invalid_grant → 401 登录失败', async () => {
  routes.set('POST /token', { status: 400, body: JSON.stringify({ error: 'invalid_grant' }) });
  const res = await app.inject({ method: 'POST', url: '/auth/login', payload: { username: 'alice', password: 'x' } });
  assert.equal(res.statusCode, 401);
  assert.deepEqual(res.json(), { error: '登录失败' });
});

test('login：上游 5xx → 503 认证服务暂不可用', async () => {
  routes.set('POST /token', { status: 500, body: '{}' });
  const res = await app.inject({ method: 'POST', url: '/auth/login', payload: { username: 'alice', password: 'pw' } });
  assert.equal(res.statusCode, 503);
  assert.deepEqual(res.json(), { error: '认证服务暂不可用' });
});

test('login：强制改密用户 → 403 PASSWORD_CHANGE_REQUIRED', async () => {
  const token = await signToken({ pwd_cr: true });
  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: token, refresh_token: 'rt', expires_in: 900 }),
  });
  const res = await app.inject({ method: 'POST', url: '/auth/login', payload: { username: 'admin', password: 'admin123' } });
  assert.equal(res.statusCode, 403);
  assert.equal(res.json().code, 'PASSWORD_CHANGE_REQUIRED');
});

// ---------------- /auth/refresh ----------------

test('refresh：缺参 → 400；上游拒绝 → 401；上游 5xx → 503；成功 → 200 字段名正确', async () => {
  let res = await app.inject({ method: 'POST', url: '/auth/refresh', payload: {} });
  assert.equal(res.statusCode, 400);

  routes.set('POST /token', { status: 400, body: JSON.stringify({ error: 'invalid_grant' }) });
  res = await app.inject({ method: 'POST', url: '/auth/refresh', payload: { refresh_token: 'revoked' } });
  assert.equal(res.statusCode, 401);
  assert.deepEqual(res.json(), { error: 'Unauthorized' });

  routes.set('POST /token', { status: 500, body: '{}' });
  res = await app.inject({ method: 'POST', url: '/auth/refresh', payload: { refresh_token: 'rt' } });
  assert.equal(res.statusCode, 503);

  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: 'at-2', refresh_token: 'rt-2', token_type: 'Bearer', expires_in: 900 }),
  });
  res = await app.inject({ method: 'POST', url: '/auth/refresh', payload: { refresh_token: 'rt-1' } });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json(), { token: 'at-2', refresh_token: 'rt-2', expires_in: 900 });
});

// ---------------- 注册代理 ----------------

test('register：上游状态码 body 透传（429/503）；网络错 → 502', async () => {
  routes.set('POST /register', { status: 429, body: JSON.stringify({ error: '请求过于频繁，请稍后再试' }) });
  let res = await app.inject({ method: 'POST', url: '/auth/register', payload: { username: 'u', email: 'u@x.com', password: 'p' } });
  assert.equal(res.statusCode, 429);
  assert.deepEqual(res.json(), { error: '请求过于频繁，请稍后再试' });

  routes.set('POST /register', { status: 503, body: JSON.stringify({ error: '注册功能未开启' }) });
  res = await app.inject({ method: 'POST', url: '/auth/register', payload: { username: 'u', email: 'u@x.com', password: 'p' } });
  assert.equal(res.statusCode, 503);

  routes.set('POST /register', { status: -1 });
  res = await app.inject({ method: 'POST', url: '/auth/register', payload: { username: 'u', email: 'u@x.com', password: 'p' } });
  assert.equal(res.statusCode, 502);
  assert.deepEqual(res.json(), { error: '注册服务暂不可用，请稍后再试' });
});

// ---------------- 鉴权中间件集成 ----------------

test('/api/me：合法 token → 200；typ=refresh 的 token → 401；无 token → 401', async () => {
  const good = await signToken();
  let res = await app.inject({ method: 'GET', url: '/api/me', headers: { authorization: `Bearer ${good}` } });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.json(), { user: { username: 'alice', role: 'user' } });

  const refreshTyp = await new SignJWT({ typ: 'refresh', ver: 1 })
    .setProtectedHeader({ alg: 'RS256', kid: KID })
    .setIssuedAt().setIssuer(ISS).setAudience(AUD).setSubject('alice').setExpirationTime('72h')
    .sign(privateKey);
  res = await app.inject({ method: 'GET', url: '/api/me', headers: { authorization: `Bearer ${refreshTyp}` } });
  assert.equal(res.statusCode, 401);

  res = await app.inject({ method: 'GET', url: '/api/me' });
  assert.equal(res.statusCode, 401);
});
