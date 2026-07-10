import { test, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { generateKeyPair, exportJWK, SignJWT, type CryptoKey } from 'jose';
import { AuthServerJwksClient, PasswordChangeRequiredError } from '../src/auth/client.js';

// 假 auth-server：node:http + jose 真签名，覆盖 JWKS 验签与转发协议两条路径。
const ISS = 'http://auth-server:5056';
const AUD = 'meta-wise';
const KID = 'test-key';

let privateKey: CryptoKey;
let server: Server;
let baseUrl: string;

// 每个测试按需设置 'METHOD /path' → 响应；jwks.json 常驻
const routes = new Map<string, { status: number; body: string; contentType?: string }>();
// 记录收到的请求供断言（转发 body、X-Forwarded-For 头）
const received: { method: string; path: string; headers: Record<string, unknown>; body: unknown }[] = [];

before(async () => {
  const pair = await generateKeyPair('RS256');
  privateKey = pair.privateKey;
  const jwk = await exportJWK(pair.publicKey);
  routes.set('GET /jwks.json', {
    status: 200,
    body: JSON.stringify({ keys: [{ ...jwk, kid: KID, alg: 'RS256', use: 'sig' }] }),
  });

  server = createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      let body: unknown;
      try { body = raw ? JSON.parse(raw) : undefined; } catch { body = raw; }
      received.push({ method: req.method!, path: req.url!, headers: req.headers, body });
      const route = routes.get(`${req.method} ${req.url}`);
      if (!route) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end('{"error":"not found"}');
        return;
      }
      res.writeHead(route.status, { 'Content-Type': route.contentType ?? 'application/json' });
      res.end(route.body);
    });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  const addr = server.address() as { port: number };
  baseUrl = `http://127.0.0.1:${addr.port}`;
});

after(() => new Promise<void>((r) => server.close(() => r())));

beforeEach(() => {
  received.length = 0;
});

function signToken(opts: {
  iss?: string; aud?: string; sub?: string; typ?: string; role?: string;
  exp?: number | string; pwdCr?: boolean; noExp?: boolean;
} = {}) {
  let jwt = new SignJWT({ typ: opts.typ ?? 'access', role: opts.role ?? 'admin', ver: 1, pwd_cr: opts.pwdCr ?? false })
    .setProtectedHeader({ alg: 'RS256', kid: KID })
    .setIssuedAt()
    .setIssuer(opts.iss ?? ISS)
    .setAudience(opts.aud ?? AUD);
  if (!opts.noExp) jwt = jwt.setExpirationTime(opts.exp ?? '15m');
  if (opts.sub !== '') jwt = jwt.setSubject(opts.sub ?? 'alice');
  return jwt.sign(privateKey);
}

function jwksClient() {
  return new AuthServerJwksClient({ issuerUrl: baseUrl, jwtIssuer: ISS, audience: AUD });
}

// ---------------- AuthServerJwksClient：login ----------------

test('jwks login 成功：user 来自已验证 claims，透传 refresh_token/expires_in', async () => {
  const accessToken = await signToken({ sub: 'alice', role: 'admin' });
  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: accessToken, refresh_token: 'rt-1', token_type: 'Bearer', expires_in: 900 }),
  });
  const result = await jwksClient().login('alice', 'pw');
  assert.deepEqual(result, {
    token: accessToken,
    user: { username: 'alice', role: 'admin' },
    refreshToken: 'rt-1',
    expiresIn: 900,
  });
  const tokenReq = received.find((r) => r.path === '/token');
  assert.deepEqual(tokenReq?.body, { grant_type: 'password', username: 'alice', password: 'pw' });
});

test('jwks login：invalid_grant → null', async () => {
  routes.set('POST /token', { status: 400, body: JSON.stringify({ error: 'invalid_grant' }) });
  assert.equal(await jwksClient().login('alice', 'wrong'), null);
});

test('jwks login：签出的 token iss 与配置不符 → 抛配置错误（登录时暴露而非静默 401）', async () => {
  const badToken = await signToken({ iss: 'http://someone-else' });
  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: badToken, refresh_token: 'rt', expires_in: 900 }),
  });
  await assert.rejects(jwksClient().login('alice', 'pw'), /AUTH_JWT_ISSUER/);
});

test('jwks login：上游 5xx → 抛错（路由映射 503，不吞成登录失败）', async () => {
  routes.set('POST /token', { status: 500, body: JSON.stringify({ error: 'boom' }) });
  await assert.rejects(jwksClient().login('alice', 'pw'));
});

test('jwks login：服务不可达 → 抛错', async () => {
  const dead = new AuthServerJwksClient({ issuerUrl: 'http://127.0.0.1:1', jwtIssuer: ISS, audience: AUD });
  await assert.rejects(dead.login('alice', 'pw'));
});

test('jwks login：pwd_cr=true（强制改密）→ PasswordChangeRequiredError', async () => {
  const token = await signToken({ pwdCr: true });
  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: token, refresh_token: 'rt', expires_in: 900 }),
  });
  await assert.rejects(jwksClient().login('admin', 'admin123'), PasswordChangeRequiredError);
});

// ---------------- AuthServerJwksClient：verify ----------------

test('jwks verify：合法 access token 通过', async () => {
  const user = await jwksClient().verify(await signToken({ sub: 'bob', role: 'user' }));
  assert.deepEqual(user, { username: 'bob', role: 'user' });
});

test('jwks verify：过期 → null', async () => {
  const token = await signToken({ exp: Math.floor(Date.now() / 1000) - 60 });
  assert.equal(await jwksClient().verify(token), null);
});

test('jwks verify：iss 不匹配 → null', async () => {
  assert.equal(await jwksClient().verify(await signToken({ iss: 'http://evil' })), null);
});

test('jwks verify：aud 不匹配 → null', async () => {
  assert.equal(await jwksClient().verify(await signToken({ aud: 'other-app' })), null);
});

test('jwks verify：typ=refresh 不得当访问凭据 → null', async () => {
  assert.equal(await jwksClient().verify(await signToken({ typ: 'refresh' })), null);
});

test('jwks verify：HS256 伪造 → null', async () => {
  const forged = await new SignJWT({ typ: 'access', role: 'admin' })
    .setProtectedHeader({ alg: 'HS256', kid: KID })
    .setIssuedAt()
    .setIssuer(ISS)
    .setAudience(AUD)
    .setSubject('alice')
    .setExpirationTime('15m')
    .sign(new TextEncoder().encode('x'.repeat(32)));
  assert.equal(await jwksClient().verify(forged), null);
});

test('jwks verify：无 sub → null', async () => {
  assert.equal(await jwksClient().verify(await signToken({ sub: '' })), null);
});

test('jwks verify：无 exp → null（不接受永不过期 token）', async () => {
  assert.equal(await jwksClient().verify(await signToken({ noExp: true })), null);
});

test('jwks verify：pwd_cr=true → null（强制改密用户禁止进入业务路由）', async () => {
  assert.equal(await jwksClient().verify(await signToken({ pwdCr: true })), null);
});

test('jwks verify：JWKS 端点不可达 → null（verify 永不抛异常）', async () => {
  const dead = new AuthServerJwksClient({ issuerUrl: 'http://127.0.0.1:1', jwtIssuer: ISS, audience: AUD });
  assert.equal(await dead.verify(await signToken({})), null);
});

// ---------------- AuthServerJwksClient：refresh ----------------

test('jwks refresh 成功：refresh_token grant 且字段映射正确', async () => {
  routes.set('POST /token', {
    status: 200,
    body: JSON.stringify({ access_token: 'at-2', refresh_token: 'rt-2', token_type: 'Bearer', expires_in: 900 }),
  });
  const result = await jwksClient().refresh('rt-1');
  assert.deepEqual(result, { token: 'at-2', refreshToken: 'rt-2', expiresIn: 900 });
  const tokenReq = received.find((r) => r.path === '/token');
  assert.deepEqual(tokenReq?.body, { grant_type: 'refresh_token', refresh_token: 'rt-1' });
});

test('jwks refresh：400 invalid_grant → null（路由映射 401）', async () => {
  routes.set('POST /token', { status: 400, body: JSON.stringify({ error: 'invalid_grant' }) });
  assert.equal(await jwksClient().refresh('bad'), null);
});

test('jwks refresh：服务不可达 → 抛错（路由映射 503）', async () => {
  const dead = new AuthServerJwksClient({ issuerUrl: 'http://127.0.0.1:1', jwtIssuer: ISS, audience: AUD });
  await assert.rejects(dead.refresh('rt'));
});

test('jwks refresh：5xx → 抛错', async () => {
  routes.set('POST /token', { status: 500, body: JSON.stringify({ error: 'boom' }) });
  await assert.rejects(jwksClient().refresh('rt'));
});

// ---------------- AuthServerJwksClient：注册代理 ----------------

test('jwks register：状态码与 body 透传，带 X-Forwarded-For（无 /auth 前缀）', async () => {
  routes.set('POST /register', {
    status: 429,
    body: JSON.stringify({ error: '请求过于频繁，请稍后再试' }),
  });
  const result = await jwksClient().register({ username: 'u1', email: 'u1@x.com', password: 'p' }, '10.0.0.9');
  assert.deepEqual(result, { status: 429, body: { error: '请求过于频繁，请稍后再试' } });
  const req = received.find((r) => r.path === '/register');
  assert.equal(req?.headers['x-forwarded-for'], '10.0.0.9');
  assert.deepEqual(req?.body, { username: 'u1', email: 'u1@x.com', password: 'p' });
});

test('jwks register：密码策略等 400 body 原样透传', async () => {
  routes.set('POST /register', {
    status: 400,
    body: JSON.stringify({ error: '密码不符合策略', code: 'PASSWORD_POLICY_FAILED', violations: ['min_length'] }),
  });
  const result = await jwksClient().register({ username: 'u1', email: 'u1@x.com', password: 'p' }, '10.0.0.9');
  assert.deepEqual(result, { status: 400, body: { error: '密码不符合策略', code: 'PASSWORD_POLICY_FAILED', violations: ['min_length'] } });
});

test('jwks register：上游响应非 JSON → 兜底 error 但保留状态码', async () => {
  routes.set('POST /register', { status: 502, body: '<html>Bad Gateway</html>', contentType: 'text/html' });
  const result = await jwksClient().register({ username: 'u1', email: 'u1@x.com', password: 'p' }, '10.0.0.9');
  assert.deepEqual(result, { status: 502, body: { error: '注册服务响应异常' } });
});
