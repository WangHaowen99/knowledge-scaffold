import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import multipart from '@fastify/multipart';
import { generateKeyPair, exportJWK, SignJWT, type CryptoKey } from 'jose';
import { renderPackage } from '../src/workflow/package-render.js';
import { zipPackage } from '../src/workflow/export.js';
import type { PackageSpec } from '../src/types.js';
import { neo4jInput, nl2sqlInput } from './helpers/data-source-fixtures.js';
import {
  graphMapping,
  graphPackageSpec,
  workbookBuffer,
} from './helpers/graph-import-fixtures.js';

// 路由层状态码映射的集成测试：真实 registerRoutes + app.inject，上游用假 auth-server。
// config 在 import 时读 process.env，因此先起假服务、设好 env，再动态 import routes。
const ISS = 'http://auth-server:5056';
const AUD = 'meta-wise';
const KID = 'route-test-key';

let privateKey: CryptoKey;
let upstream: Server;
let app: FastifyInstance;
let upstreamBaseUrl: string;

// status: -1 表示直接掐断连接（模拟网络错）
const routes = new Map<string, { status: number; body?: string; contentType?: string }>();
const consumerRequests: string[] = [];
const llmConnectionRequests: Array<{ path: string; authorization: string }> = [];
const testedDataSources: unknown[] = [];
const syncedDataSources: any[] = [];
const graphImportTasks: Array<() => Promise<void>> = [];
const evaluationDatabaseImportTasks: Array<() => Promise<void>> = [];
const evaluationDatabaseConnections: any[] = [];
const evaluationDatabaseQueries: string[] = [];
const projectDatabaseAdminConnections: any[] = [];
const projectDatabaseAdminQueries: string[] = [];
let projectDatabaseExists = false;
let packageChatPlanTask: ((...args: any[]) => Promise<any>) | null = null;
let packageChatApplyTask: (() => Promise<any>) | null = null;
let materialAnalysisTask: ((...args: any[]) => Promise<any>) | null = null;

before(async () => {
  const pair = await generateKeyPair('RS256');
  privateKey = pair.privateKey;
  const jwk = await exportJWK(pair.publicKey);
  routes.set('GET /jwks.json', {
    status: 200,
    body: JSON.stringify({ keys: [{ ...jwk, kid: KID, alg: 'RS256', use: 'sig' }] }),
  });

  upstream = createServer((req, res) => {
    if (req.url?.startsWith('/agentic/')) consumerRequests.push(`${req.method} ${req.url}`);
    if (req.url?.endsWith('/chat/completions')) {
      llmConnectionRequests.push({
        path: req.url,
        authorization: String(req.headers.authorization ?? ''),
      });
    }
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
  upstreamBaseUrl = `http://127.0.0.1:${addr.port}`;

  process.env.AUTH_ISSUER_URL = upstreamBaseUrl;
  process.env.AUTH_JWT_ISSUER = ISS;
  process.env.AUTH_JWT_AUDIENCE = AUD;
  process.env.CONSUMER_API_URL = upstreamBaseUrl;
  process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'mtool-route-test-'));
  process.env.EVAL_DB_URL = 'mysql://root:eval-secret@mysql.local:3306';

  const { registerRoutes } = await import('../src/routes.js');
  app = Fastify({ logger: false });
  await app.register(multipart, { limits: { fileSize: 50 * 1024 * 1024 } });
  await app.register(registerRoutes, {
    dataSourceConnectionTester: async (source: unknown) => { testedDataSources.push(source); },
    dataSourceConsumerSync: async (options: any) => {
      syncedDataSources.push(options);
      return {
        workspaceId: options.workspaceId,
        ontologyId: options.ontologyId,
      };
    },
    evaluationDatabaseDependencies: {
      databaseUrl: process.env.EVAL_DB_URL,
      schedule: (task: () => Promise<void>) => { evaluationDatabaseImportTasks.push(task); },
      connectAdmin: async (database: any) => {
        projectDatabaseAdminConnections.push(database);
        return {
          query: async (sql: string) => {
            projectDatabaseAdminQueries.push(sql);
            if (sql.startsWith('SELECT')) return projectDatabaseExists ? [{ exists: true }] : [];
            if (sql.startsWith('CREATE DATABASE')) projectDatabaseExists = true;
            return [];
          },
          close: async () => {},
        };
      },
      connect: async (database: any) => {
        evaluationDatabaseConnections.push(database);
        return {
          query: async (sql: string) => { evaluationDatabaseQueries.push(sql); },
          close: async () => {},
        };
      },
    },
    graphImportExecutionDependencies: {
      inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
      schedule: (task: () => Promise<void>) => { graphImportTasks.push(task); },
      writer: async (plan: any) => ({
        nodeCount: plan.nodeCount,
        relationshipCount: plan.relationshipCount,
      }),
    },
    packageChatPlan: async (...args: any[]) => {
      if (!packageChatPlanTask) throw new Error('测试未配置包修缮计划任务');
      return packageChatPlanTask(...args);
    },
    packageChatApply: async () => {
      if (!packageChatApplyTask) throw new Error('测试未配置包修缮应用任务');
      return packageChatApplyTask();
    },
    runAnalyze: async (...args: any[]) => {
      if (!materialAnalysisTask) throw new Error('测试未配置资料分析任务');
      return materialAnalysisTask(...args);
    },
  });
  await app.ready();
});

after(async () => {
  await app.close();
  await new Promise<void>((r) => upstream.close(() => r()));
});

function signToken(claims: Record<string, unknown> = {}, subject = 'alice') {
  return new SignJWT({ typ: 'access', role: 'user', ver: 1, pwd_cr: false, ...claims })
    .setProtectedHeader({ alg: 'RS256', kid: KID })
    .setIssuedAt()
    .setIssuer(ISS)
    .setAudience(AUD)
    .setSubject(subject)
    .setExpirationTime('15m')
    .sign(privateKey);
}

function multipartExcel(filename: string, buffer: Buffer) {
  const boundary = `----modeling-${Date.now()}`;
  return {
    headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
    payload: Buffer.concat([
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name=file; filename=${filename}\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n`),
      buffer,
      Buffer.from(`\r\n--${boundary}--\r\n`),
    ]),
  };
}

function multipartFile(filename: string, buffer: Buffer, contentType = 'text/plain') {
  const boundary = `----modeling-material-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return {
    headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
    payload: Buffer.concat([
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: ${contentType}\r\n\r\n`),
      buffer,
      Buffer.from(`\r\n--${boundary}--\r\n`),
    ]),
  };
}

function multipartZip(filename: string, buffer: Buffer) {
  return multipartFile(filename, buffer, 'application/zip');
}

function multipartEvaluationDatabaseExcel(
  filename: string,
  buffer: Buffer,
  replaceConfirmed = true,
  databaseUrlMode: 'default' | 'custom' | '' = 'default',
  databaseUrl = '',
) {
  const boundary = `----modeling-eval-${Date.now()}`;
  return {
    headers: { 'content-type': `multipart/form-data; boundary=${boundary}` },
    payload: Buffer.concat([
      ...(replaceConfirmed ? [
        Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="replace_confirmation"\r\n\r\nreplace_same_name_tables\r\n`),
      ] : []),
      ...(databaseUrlMode ? [
        Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="database_url_mode"\r\n\r\n${databaseUrlMode}\r\n`),
      ] : []),
      ...(databaseUrl ? [
        Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="database_url"\r\n\r\n${databaseUrl}\r\n`),
      ] : []),
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n`),
      buffer,
      Buffer.from(`\r\n--${boundary}--\r\n`),
    ]),
  };
}

async function advanceModelingProjectToMaterialAnalysis(projectId: string, packageRevisionOid: string) {
  const [
    { AiConstructionTaskGraph },
    { AiConstructionOrchestrator },
    { getAuthoringState },
  ] = await Promise.all([
    import('../src/ai-construction/task-graph.js'),
    import('../src/ai-construction/orchestrator.js'),
    import('../src/authoring/state.js'),
  ]);
  const graph = new AiConstructionTaskGraph();
  const orchestrator = new AiConstructionOrchestrator();
  const state = getAuthoringState();
  const initial = graph.snapshot(projectId)!;
  const prompted = orchestrator.advanceAfterMilestoneConfirmation(projectId, initial);
  const interaction = prompted.interaction!;
  const answered = state.apply({
    kind: 'respond_interaction',
    projectId,
    interactionId: interaction.id,
    responseId: `goal-${projectId}`,
    response: { answers: [] },
  });
  if (answered.kind !== 'interaction') throw new Error('建模目标回答失败');
  const brief = {
    problem: '校验月度生产计划',
    targetUsers: '生产计划专家',
    scenarios: '计划下发前审核',
    successCriteria: '审核结果可追溯',
  };
  state.apply({
    kind: 'update_run',
    projectId,
    runId: answered.run.id,
    status: 'completed',
    result: { purpose: 'modeling_brief', ...brief },
  });
  let snapshot = orchestrator.completeModelingBrief(projectId, {
    taskId: initial.tasks[0].id,
    packageRevisionOid,
    messageKey: `goal.completed.${projectId}`,
    brief,
  }).aiConstruction;
  snapshot = graph.apply(projectId, {
    type: 'confirm_milestone',
    expectedRevision: snapshot.revision,
    milestoneId: 'goal_confirmed',
    packageRevisionOid,
    actor: { username: 'alice', role: 'project_owner' },
  });
  orchestrator.advanceAfterMilestoneConfirmation(projectId, snapshot);
  return { graph, brief };
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

test('过期 token 访问不存在项目只返回一次 401，后续请求仍可处理', async () => {
  const now = Math.floor(Date.now() / 1_000);
  const expired = await new SignJWT({ typ: 'access', role: 'user', ver: 1, pwd_cr: false })
    .setProtectedHeader({ alg: 'RS256', kid: KID })
    .setIssuedAt(now - 120)
    .setIssuer(ISS)
    .setAudience(AUD)
    .setSubject('expired-user')
    .setExpirationTime(now - 60)
    .sign(privateKey);

  const response = await app.inject({
    method: 'GET',
    url: '/api/projects/project_missing',
    headers: { authorization: `Bearer ${expired}` },
  });

  assert.equal(response.statusCode, 401, response.body);
  assert.deepEqual(response.json(), { error: '认证失败或令牌已过期' });

  const health = await app.inject({ method: 'GET', url: '/health' });
  assert.equal(health.statusCode, 200);
  assert.deepEqual(health.json(), { status: 'ok' });
});

test('平台 LLM 默认配置 API 同时要求登录和 admin 角色', async () => {
  let response = await app.inject({ method: 'GET', url: '/api/admin/llm-defaults' });
  assert.equal(response.statusCode, 401);

  const userToken = await signToken({}, 'regular-user');
  response = await app.inject({
    method: 'GET', url: '/api/admin/llm-defaults',
    headers: { authorization: `Bearer ${userToken}` },
  });
  assert.equal(response.statusCode, 403);

  const adminToken = await signToken({ role: 'admin' }, 'platform-admin');
  response = await app.inject({
    method: 'GET', url: '/api/admin/llm-defaults',
    headers: { authorization: `Bearer ${adminToken}` },
  });
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.json().defaults, { main: null, task: null });
});

test('新注册用户初始化当时的默认快照，老用户与既有快照不受 admin 后续修改影响', async () => {
  const adminToken = await signToken({ role: 'admin' }, 'registration-platform-admin');
  const adminHeaders = { authorization: `Bearer ${adminToken}` };
  const configured = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'faux', model: 'registration-default-v1',
      maxTokens: 5000, expectedRevision: 0,
    },
  });
  assert.equal(configured.statusCode, 200, configured.body);
  let revision = configured.json().defaults.main.revision;

  try {
    routes.set('POST /register', {
      status: 200,
      body: JSON.stringify({
        success: true,
        user: { username: 'registered_default_user_a', role: 'user', password_change_required: false },
      }),
    });
    let response = await app.inject({
      method: 'POST', url: '/auth/register',
      payload: { username: 'registered_default_user_a', email: 'new-a@example.com', password: 'synthetic-password' },
    });
    assert.equal(response.statusCode, 200, response.body);

    const firstUserToken = await signToken({}, 'registered_default_user_a');
    const firstUserHeaders = { authorization: `Bearer ${firstUserToken}` };
    const firstSnapshot = await app.inject({ method: 'GET', url: '/api/settings', headers: firstUserHeaders });
    assert.equal(firstSnapshot.json().llm.model, 'registration-default-v1');
    assert.equal(firstSnapshot.json().llm.maxTokens, 5000);
    assert.equal(firstSnapshot.json().llm.source, 'default_snapshot');

    const existingToken = await signToken({}, 'existing_without_default_snapshot');
    const existing = await app.inject({
      method: 'GET', url: '/api/settings',
      headers: { authorization: `Bearer ${existingToken}` },
    });
    assert.equal(existing.json().llm.source, 'env');
    assert.notEqual(existing.json().llm.model, 'registration-default-v1');

    const updated = await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: {
        purpose: 'main', baseUrl: 'faux', model: 'registration-default-v2',
        maxTokens: 6000, expectedRevision: revision,
      },
    });
    assert.equal(updated.statusCode, 200, updated.body);
    revision = updated.json().defaults.main.revision;

    const firstStillV1 = await app.inject({ method: 'GET', url: '/api/settings', headers: firstUserHeaders });
    assert.equal(firstStillV1.json().llm.model, 'registration-default-v1');
    assert.equal(firstStillV1.json().llm.maxTokens, 5000);

    routes.set('POST /register', {
      status: 200,
      body: JSON.stringify({
        success: true,
        user: { username: 'registered_default_user_b', role: 'user', password_change_required: false },
      }),
    });
    response = await app.inject({
      method: 'POST', url: '/auth/register',
      payload: { username: 'registered_default_user_b', email: 'new-b@example.com', password: 'synthetic-password' },
    });
    assert.equal(response.statusCode, 200, response.body);
    const secondUserToken = await signToken({}, 'registered_default_user_b');
    const secondSnapshot = await app.inject({
      method: 'GET', url: '/api/settings',
      headers: { authorization: `Bearer ${secondUserToken}` },
    });
    assert.equal(secondSnapshot.json().llm.model, 'registration-default-v2');
    assert.equal(secondSnapshot.json().llm.maxTokens, 6000);
  } finally {
    await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose: 'main', enabled: false, expectedRevision: revision },
    });
  }
});

test('注册成功会清理同名旧账号的 Modeling LLM 配置，注册失败不触碰旧配置', async () => {
  const { getDb } = await import('../src/db/index.js');
  const username = 'reused_registration_username';
  getDb().prepare(`INSERT INTO user_llm_settings (
      username, base_url, model, api_key, max_tokens, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?)`)
    .run(username, 'https://former-owner.example.com/v1', 'former-owner-model', 'former-owner-key', 2048, new Date().toISOString());

  routes.set('POST /register', { status: 400, body: JSON.stringify({ error: '用户名已存在' }) });
  let response = await app.inject({
    method: 'POST', url: '/auth/register',
    payload: { username, email: 'failed@example.com', password: 'synthetic-password' },
  });
  assert.equal(response.statusCode, 400);
  assert.equal((getDb().prepare('SELECT model FROM user_llm_settings WHERE username = ?')
    .get(username) as { model: string }).model, 'former-owner-model');

  routes.set('POST /register', {
    status: 200,
    body: JSON.stringify({ success: true, user: { username, role: 'user' } }),
  });
  response = await app.inject({
    method: 'POST', url: '/auth/register',
    payload: { username, email: 'created@example.com', password: 'synthetic-password' },
  });
  assert.equal(response.statusCode, 200);
  assert.equal(getDb().prepare('SELECT model FROM user_llm_settings WHERE username = ?').get(username), undefined);
  assert.equal(getDb().prepare('SELECT model FROM user_llm_default_snapshots WHERE username = ?').get(username), undefined);

  const adminToken = await signToken({ role: 'admin' }, 'post-registration-admin');
  const adminHeaders = { authorization: `Bearer ${adminToken}` };
  const configured = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'faux', model: 'future-default-must-not-fallback',
      maxTokens: 4096, expectedRevision: 0,
    },
  });
  assert.equal(configured.statusCode, 200, configured.body);
  try {
    const userToken = await signToken({}, username);
    const settings = await app.inject({
      method: 'GET', url: '/api/settings', headers: { authorization: `Bearer ${userToken}` },
    });
    assert.equal(settings.json().llm.source, 'env');
    assert.notEqual(settings.json().llm.model, 'future-default-must-not-fallback');
  } finally {
    await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose: 'main', enabled: false, expectedRevision: configured.json().defaults.main.revision },
    });
  }
});

test('LLM 配置按认证用户隔离，API Key 不回显', async () => {
  const aliceToken = await signToken({}, 'settings_user_a');
  const bobToken = await signToken({}, 'settings_user_b');
  const aliceHeaders = { authorization: `Bearer ${aliceToken}` };
  const bobHeaders = { authorization: `Bearer ${bobToken}` };

  let response = await app.inject({
    method: 'PUT', url: '/api/settings', headers: aliceHeaders,
    payload: {
      llmBaseUrl: 'https://user-a.example.com/v1', llmModel: 'model-a',
      llmApiKey: 'synthetic-key-a', llmMaxTokens: '4096', llmContextWindow: '1000000',
    },
  });
  assert.equal(response.statusCode, 200);

  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers: bobHeaders,
    payload: {
      llmBaseUrl: 'https://user-b.example.com/v1', llmModel: 'model-b',
      llmApiKey: 'synthetic-key-b', llmMaxTokens: '6144', llmContextWindow: '128000',
    },
  });
  assert.equal(response.statusCode, 200);

  const alice = await app.inject({ method: 'GET', url: '/api/settings', headers: aliceHeaders });
  const bob = await app.inject({ method: 'GET', url: '/api/settings', headers: bobHeaders });
  assert.equal(alice.statusCode, 200);
  assert.equal(bob.statusCode, 200);
  assert.deepEqual(alice.json().llm, {
    baseUrl: 'https://user-a.example.com/v1', model: 'model-a', maxTokens: 4096, contextWindow: 1000000,
    apiKeySet: true, personalApiKeySet: true, source: 'user', userIsolated: true,
    platformDefaultAvailable: false,
  });
  assert.deepEqual(bob.json().llm, {
    baseUrl: 'https://user-b.example.com/v1', model: 'model-b', maxTokens: 6144, contextWindow: 128000,
    apiKeySet: true, personalApiKeySet: true, source: 'user', userIsolated: true,
    platformDefaultAvailable: false,
  });
  assert.doesNotMatch(alice.body, /synthetic-key/);
  assert.doesNotMatch(bob.body, /synthetic-key/);

  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers: aliceHeaders,
    payload: { llmBaseUrl: 'https://invalid-update.example.com/v1', llmMaxTokens: '0' },
  });
  assert.equal(response.statusCode, 400);
  const unchanged = await app.inject({ method: 'GET', url: '/api/settings', headers: aliceHeaders });
  assert.equal(unchanged.json().llm.baseUrl, 'https://user-a.example.com/v1');

  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers: aliceHeaders,
    payload: { llmContextWindow: '0' },
  });
  assert.equal(response.statusCode, 400);
  assert.match(response.json().error, /llmContextWindow/);

  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers: aliceHeaders,
    payload: { llmApiKeyClear: true },
  });
  assert.equal(response.statusCode, 200);
  const aliceCleared = await app.inject({ method: 'GET', url: '/api/settings', headers: aliceHeaders });
  const bobUnchanged = await app.inject({ method: 'GET', url: '/api/settings', headers: bobHeaders });
  assert.equal(aliceCleared.json().llm.personalApiKeySet, false);
  assert.equal(bobUnchanged.json().llm.personalApiKeySet, true);

  const [aliceConcurrent, bobConcurrent] = await Promise.all([
    app.inject({ method: 'GET', url: '/api/settings', headers: aliceHeaders }),
    app.inject({ method: 'GET', url: '/api/settings', headers: bobHeaders }),
  ]);
  assert.equal(aliceConcurrent.json().llm.baseUrl, 'https://user-a.example.com/v1');
  assert.equal(bobConcurrent.json().llm.baseUrl, 'https://user-b.example.com/v1');
});

test('后台任务模型可按用户独立保存并在清空后使用主模型', async () => {
  const userToken = await signToken({}, 'task_settings_user');
  const headers = { authorization: `Bearer ${userToken}` };

  let response = await app.inject({
    method: 'PUT', url: '/api/settings', headers,
    payload: {
      llmBaseUrl: 'https://main-user.example.com/v1', llmModel: 'main-user-model',
      llmApiKey: 'synthetic-main-user-key', llmMaxTokens: '4096', llmContextWindow: '128000',
    },
  });
  assert.equal(response.statusCode, 200, response.body);
  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers,
    payload: {
      llmPurpose: 'task',
      llmBaseUrl: 'https://task-user.example.com/v1', llmModel: 'task-user-model',
      llmApiKey: 'synthetic-task-user-key', llmMaxTokens: '2048', llmContextWindow: '64000',
    },
  });
  assert.equal(response.statusCode, 200, response.body);

  let settings = await app.inject({ method: 'GET', url: '/api/settings', headers });
  assert.deepEqual(settings.json().taskLlm, {
    baseUrl: 'https://task-user.example.com/v1', model: 'task-user-model',
    maxTokens: 2048, contextWindow: 64000,
    apiKeySet: true, personalApiKeySet: true, source: 'user', userIsolated: true,
    platformDefaultAvailable: false,
  });
  assert.equal(settings.json().llm.model, 'main-user-model');
  assert.doesNotMatch(settings.body, /synthetic-(main|task)-user-key/);

  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers,
    payload: {
      llmPurpose: 'task', llmProviderClear: true,
      llmMaxTokens: '', llmContextWindow: '',
    },
  });
  assert.equal(response.statusCode, 200, response.body);
  settings = await app.inject({ method: 'GET', url: '/api/settings', headers });
  assert.equal(settings.json().taskLlm.source, 'main');
  assert.equal(settings.json().taskLlm.model, 'main-user-model');
  assert.equal(settings.json().taskLlm.baseUrl, 'https://main-user.example.com/v1');
  assert.equal(settings.json().taskLlm.personalApiKeySet, false);

  response = await app.inject({
    method: 'PUT', url: '/api/settings', headers,
    payload: { llmPurpose: 'invalid', llmModel: 'must-not-save' },
  });
  assert.equal(response.statusCode, 400);

  response = await app.inject({
    method: 'POST', url: '/api/settings/llm/sync-default', headers,
    payload: { purpose: 'invalid' },
  });
  assert.equal(response.statusCode, 400);

  response = await app.inject({
    method: 'POST', url: '/api/settings/llm/test', headers,
    payload: { purpose: 'invalid' },
  });
  assert.equal(response.statusCode, 400);
});

test('主模型与后台任务模型可分别同步平台默认，任务默认缺省时跟随主默认', async () => {
  const adminToken = await signToken({ role: 'admin' }, 'purpose-sync-admin');
  const userToken = await signToken({}, 'purpose_sync_user');
  const adminHeaders = { authorization: `Bearer ${adminToken}` };
  const userHeaders = { authorization: `Bearer ${userToken}` };

  const mainDefault = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'faux', model: 'purpose-main-default',
      maxTokens: 7000, expectedRevision: 0,
    },
  });
  assert.equal(mainDefault.statusCode, 200, mainDefault.body);
  let mainRevision = mainDefault.json().defaults.main.revision;
  let taskRevision = 0;

  try {
    let response = await app.inject({
      method: 'POST', url: '/api/settings/llm/sync-default', headers: userHeaders,
      payload: { purpose: 'task' },
    });
    assert.equal(response.statusCode, 200, response.body);
    let settings = await app.inject({ method: 'GET', url: '/api/settings', headers: userHeaders });
    assert.equal(settings.json().llm.source, 'env');
    assert.equal(settings.json().taskLlm.source, 'default_snapshot');
    assert.equal(settings.json().taskLlm.model, 'purpose-main-default');

    const taskDefault = await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: {
        purpose: 'task', baseUrl: 'faux', model: 'purpose-task-default',
        maxTokens: 3000, expectedRevision: 0,
      },
    });
    assert.equal(taskDefault.statusCode, 200, taskDefault.body);
    taskRevision = taskDefault.json().defaults.task.revision;

    response = await app.inject({
      method: 'POST', url: '/api/settings/llm/sync-default', headers: userHeaders,
      payload: { purpose: 'main' },
    });
    assert.equal(response.statusCode, 200, response.body);
    settings = await app.inject({ method: 'GET', url: '/api/settings', headers: userHeaders });
    assert.equal(settings.json().llm.model, 'purpose-main-default');
    assert.equal(settings.json().taskLlm.model, 'purpose-main-default');

    response = await app.inject({
      method: 'POST', url: '/api/settings/llm/sync-default', headers: userHeaders,
      payload: { purpose: 'task' },
    });
    assert.equal(response.statusCode, 200, response.body);
    settings = await app.inject({ method: 'GET', url: '/api/settings', headers: userHeaders });
    assert.equal(settings.json().llm.model, 'purpose-main-default');
    assert.equal(settings.json().taskLlm.model, 'purpose-task-default');
    assert.equal(settings.json().taskLlm.maxTokens, 3000);
  } finally {
    if (taskRevision) await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose: 'task', enabled: false, expectedRevision: taskRevision },
    });
    await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose: 'main', enabled: false, expectedRevision: mainRevision },
    });
  }
});

test('同步平台默认 LLM 只替换当前用户快照，admin 后续修改不会自动追写', async () => {
  const adminToken = await signToken({ role: 'admin' }, 'sync-platform-admin');
  const userAToken = await signToken({}, 'sync_user_a');
  const userBToken = await signToken({}, 'sync_user_b');
  const adminHeaders = { authorization: `Bearer ${adminToken}` };
  const userAHeaders = { authorization: `Bearer ${userAToken}` };
  const userBHeaders = { authorization: `Bearer ${userBToken}` };

  let response = await app.inject({ method: 'POST', url: '/api/settings/llm/sync-default' });
  assert.equal(response.statusCode, 401);

  for (const [headers, suffix] of [[userAHeaders, 'a'], [userBHeaders, 'b']] as const) {
    response = await app.inject({
      method: 'PUT', url: '/api/settings', headers,
      payload: {
        llmBaseUrl: `https://sync-user-${suffix}.example.com/v1`,
        llmModel: `personal-model-${suffix}`,
        llmApiKey: `synthetic-sync-key-${suffix}`,
        llmMaxTokens: suffix === 'a' ? '4096' : '6144',
      },
    });
    assert.equal(response.statusCode, 200);
  }

  response = await app.inject({
    method: 'POST', url: '/api/settings/llm/sync-default', headers: userAHeaders,
  });
  assert.equal(response.statusCode, 409);
  assert.match(response.json().error, /尚未配置平台默认 LLM/);
  const userABeforeDefault = await app.inject({ method: 'GET', url: '/api/settings', headers: userAHeaders });
  assert.equal(userABeforeDefault.json().llm.model, 'personal-model-a');

  const configured = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'faux', model: 'platform-default-model',
      maxTokens: 7777, expectedRevision: 0,
    },
  });
  assert.equal(configured.statusCode, 200, configured.body);
  let revision = configured.json().defaults.main.revision;

  try {
    const available = await app.inject({ method: 'GET', url: '/api/settings', headers: userAHeaders });
    assert.equal(available.json().llm.platformDefaultAvailable, true);

    response = await app.inject({
      method: 'POST', url: '/api/settings/llm/sync-default', headers: userAHeaders,
    });
    assert.equal(response.statusCode, 200, response.body);

    const userA = await app.inject({ method: 'GET', url: '/api/settings', headers: userAHeaders });
    const userB = await app.inject({ method: 'GET', url: '/api/settings', headers: userBHeaders });
    assert.deepEqual(userA.json().llm, {
      baseUrl: 'faux', model: 'platform-default-model', maxTokens: 7777, contextWindow: 1000000,
      apiKeySet: false, personalApiKeySet: false, source: 'default_snapshot', userIsolated: true,
      platformDefaultAvailable: true,
    });
    assert.equal(userB.json().llm.model, 'personal-model-b');
    assert.equal(userB.json().llm.maxTokens, 6144);
    assert.equal(userB.json().llm.personalApiKeySet, true);
    assert.equal(userB.json().llm.source, 'user');

    const updated = await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: {
        purpose: 'main', baseUrl: 'faux', model: 'platform-default-model-v2',
        maxTokens: 8888, expectedRevision: revision,
      },
    });
    assert.equal(updated.statusCode, 200, updated.body);
    revision = updated.json().defaults.main.revision;

    const userAStillV1 = await app.inject({ method: 'GET', url: '/api/settings', headers: userAHeaders });
    assert.equal(userAStillV1.json().llm.model, 'platform-default-model');
    assert.equal(userAStillV1.json().llm.maxTokens, 7777);
    const userBAfterAdminUpdate = await app.inject({ method: 'GET', url: '/api/settings', headers: userBHeaders });
    assert.equal(userBAfterAdminUpdate.json().llm.model, 'personal-model-b');

    response = await app.inject({
      method: 'POST', url: '/api/settings/llm/sync-default', headers: userAHeaders,
    });
    assert.equal(response.statusCode, 200, response.body);
    const userAAfterResync = await app.inject({ method: 'GET', url: '/api/settings', headers: userAHeaders });
    assert.equal(userAAfterResync.json().llm.model, 'platform-default-model-v2');
    assert.equal(userAAfterResync.json().llm.maxTokens, 8888);
  } finally {
    await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose: 'main', enabled: false, expectedRevision: revision },
    });
  }
});

test('编辑默认快照的端点/模型做连接测试时不会把快照 Key 发到新端点', async () => {
  const adminToken = await signToken({ role: 'admin' }, 'connection-test-admin');
  const userToken = await signToken({}, 'connection_test_snapshot_user');
  const adminHeaders = { authorization: `Bearer ${adminToken}` };
  const userHeaders = { authorization: `Bearer ${userToken}` };
  routes.set('POST /chat/completions', { status: 200, body: '{}' });
  routes.set('POST /changed/chat/completions', { status: 200, body: '{}' });

  const configured = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: upstreamBaseUrl, model: 'snapshot-test-model',
      apiKey: 'snapshot-secret-must-not-cross-scope', maxTokens: 4096, expectedRevision: 0,
    },
  });
  assert.equal(configured.statusCode, 200, configured.body);
  let revision = configured.json().defaults.main.revision;

  try {
    const synced = await app.inject({ method: 'POST', url: '/api/settings/llm/sync-default', headers: userHeaders });
    assert.equal(synced.statusCode, 200, synced.body);

    llmConnectionRequests.length = 0;
    let response = await app.inject({
      method: 'POST', url: '/api/settings/llm/test', headers: userHeaders,
      payload: { baseUrl: upstreamBaseUrl, model: 'snapshot-test-model' },
    });
    assert.equal(response.statusCode, 200);
    assert.equal(llmConnectionRequests.at(-1)?.authorization, 'Bearer snapshot-secret-must-not-cross-scope');

    response = await app.inject({
      method: 'POST', url: '/api/settings/llm/test', headers: userHeaders,
      payload: { baseUrl: `${upstreamBaseUrl}/changed`, model: 'edited-model' },
    });
    assert.equal(response.statusCode, 200);
    assert.equal(llmConnectionRequests.at(-1)?.path, '/changed/chat/completions');
    assert.equal(llmConnectionRequests.at(-1)?.authorization, '');
  } finally {
    await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose: 'main', enabled: false, expectedRevision: revision },
    });
  }
});

test('知识包工作区：导入 ZIP 后建立仅编辑项目、版本索引和原始文件快照', async () => {
  const token = await signToken();
  const authorization = `Bearer ${token}`;
  const spec: PackageSpec = {
    id: 'route-import-demo', name: '路由导入演示', description: '导入已有知识包',
    version: '1.0.0', language: 'zh-CN', status: 'review', defaultProviderAlias: 'default',
    objects: [],
    operations: [{
      key: 'answer_demo', title: '回答问题', description: '回答演示问题',
      inputs: [], outputs: [], flow: ['读取知识包。'], relatedObjects: [],
      competencyQuestion: '如何使用知识包？',
    }],
  };
  const upload = multipartZip('demo.zip', Buffer.from(zipPackage(renderPackage(spec), spec.id)));
  const imported = await app.inject({
    method: 'POST', url: '/api/package-workspaces/import',
    headers: { authorization, ...upload.headers }, payload: upload.payload,
  });

  assert.equal(imported.statusCode, 201, imported.body);
  assert.equal(imported.json().project.kind, 'package');
  assert.equal(imported.json().project.stage, 'review');
  assert.equal(imported.json().version, '1.0.0');
  assert.ok(imported.json().revision);

  const projectId = imported.json().project.id;
  const pkg = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package`, headers: { authorization },
  });
  assert.equal(pkg.statusCode, 200, pkg.body);
  assert.equal(pkg.json().version, '1.0.0');
  assert.ok(pkg.json().files.includes('operations/answer_demo.md'));
});

test('项目删除 API：要求明确确认、校验 owner、阻止运行任务并完成删除', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '待删除项目' },
  });
  const projectId = created.json().project.id;

  let response = await app.inject({
    method: 'DELETE', url: `/api/projects/${projectId}`, headers,
    payload: {},
  });
  assert.equal(response.statusCode, 400, response.body);

  const { createProject } = await import('../src/projects/repo.js');
  const foreignProject = createProject({ owner: 'bob', name: '其他用户项目' });
  response = await app.inject({
    method: 'DELETE', url: `/api/projects/${foreignProject.id}`, headers,
    payload: { confirmed: true },
  });
  assert.equal(response.statusCode, 404, response.body);

  const busyEvaluationDatabaseProject = createProject({ owner: 'alice', name: '评测库导入中项目' });
  const {
    createEvaluationDatabaseImportRun,
    finishEvaluationDatabaseImportRun,
  } = await import('../src/evaluation/eval-db-import-repo.js');
  const evaluationDatabaseImport = createEvaluationDatabaseImportRun({
    projectId: busyEvaluationDatabaseProject.id,
    filename: 'evaluation.xlsx',
    database: 'mw_busy',
  });
  response = await app.inject({
    method: 'DELETE', url: `/api/projects/${busyEvaluationDatabaseProject.id}`, headers,
    payload: { confirmed: true },
  });
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PROJECT_BUSY');
  assert.equal(response.json().operation, 'evaluation-database-import');
  finishEvaluationDatabaseImportRun(evaluationDatabaseImport.id, { error: 'test cleanup' });
  response = await app.inject({
    method: 'DELETE', url: `/api/projects/${busyEvaluationDatabaseProject.id}`, headers,
    payload: { confirmed: true },
  });
  assert.equal(response.statusCode, 200, response.body);

  response = await app.inject({
    method: 'DELETE', url: `/api/projects/${projectId}`, headers,
    payload: { confirmed: true },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().deleted, projectId);
  const missing = await app.inject({ method: 'GET', url: `/api/projects/${projectId}`, headers });
  assert.equal(missing.statusCode, 404);
});

test('项目删除 API：数据库删除成功后清理工件，清理失败仍返回已删除', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '工件清理失败策略项目' },
  });
  const projectId = created.json().project.id;
  const [{ getPackageArtifactStore }, { getProject }] = await Promise.all([
    import('../src/artifacts/runtime.js'),
    import('../src/projects/repo.js'),
  ]);
  const store = getPackageArtifactStore();
  await store.ensureProject(projectId);
  const originalRemove = store.removeProjectArtifacts;
  let cleanupCalls = 0;
  let projectExistedDuringCleanup = true;
  store.removeProjectArtifacts = async (removedProjectId: string) => {
    if (removedProjectId !== projectId) {
      return originalRemove.call(store, removedProjectId);
    }
    cleanupCalls += 1;
    projectExistedDuringCleanup = Boolean(getProject(removedProjectId));
    throw new Error('模拟工件仓库清理失败');
  };

  try {
    const response = await app.inject({
      method: 'DELETE', url: `/api/projects/${projectId}`, headers,
      payload: { confirmed: true },
    });
    assert.equal(response.statusCode, 200, response.body);
    assert.equal(response.json().deleted, projectId);
    assert.equal(cleanupCalls, 1);
    assert.equal(projectExistedDuringCleanup, false);
    assert.equal(getProject(projectId), null);
  } finally {
    store.removeProjectArtifacts = originalRemove;
    await originalRemove.call(store, projectId);
  }
});

/**
 * 资料分析改成后台作业后，结果不再随响应返回。这里等任务图走到终态再断言。
 * 用轮询而不是钩内部实现：作业收尾涉及好几步写入，钩住其中一步容易断在半路。
 */
async function waitForMaterialTask(
  projectId: string,
  statuses: string[],
  timeoutMs = 5_000,
): Promise<any> {
  const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
  const { getDb } = await import('../src/db/index.js');
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const task = new AiConstructionTaskGraph(getDb()).snapshot(projectId)?.tasks
      .find((item: any) => item.kind === 'material_analysis');
    if (task && statuses.includes(task.status)) return task;
    if (Date.now() > deadline) {
      throw new Error(`资料分析任务未在 ${timeoutMs}ms 内进入 ${statuses.join('/')}，当前=${task?.status}`);
    }
    await new Promise((resolve) => { setTimeout(resolve, 20); });
  }
}

test('资料分析 API：推进统一任务图并绑定当前工件修订', async (t) => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '资料分析统一编排项目' },
  });
  const projectId = created.json().project.id;
  const [{ getPackageArtifactStore }, { insertDocumentRevision }] = await Promise.all([
    import('../src/artifacts/runtime.js'),
    import('../src/documents/repo.js'),
  ]);
  const current = await getPackageArtifactStore().initializeDraftPackage(projectId, {
    id: 'material-analysis-route',
    name: '资料分析统一编排项目',
    description: '验证分析结果和 Git 工件修订的绑定',
  });
  const { brief } = await advanceModelingProjectToMaterialAnalysis(projectId, current.revision.oid);
  insertDocumentRevision({
    projectId,
    filename: '生产计划.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '生产计划需要按月度审核并记录例外。',
  });
  const progress: any[] = [];
  materialAnalysisTask = async (
    receivedProjectId: string,
    _tables: unknown[],
    documents: Array<{ id: string; filename: string }>,
    _llm: unknown,
    onProgress: (input: any) => void,
    confirmedBrief: unknown,
  ) => {
    assert.equal(receivedProjectId, projectId);
    assert.equal(documents[0]?.filename, '生产计划.md');
    assert.deepEqual(confirmedBrief, brief);
    onProgress({ phase: 'domain_analysis', title: '正在整理领域概览', percent: 42 });
    progress.push(42);
    return {
      node: { id: 'analysis-route-node', projectId, type: 'analyze', status: 'done', ordinal: 1 },
      analysis: {
        domainOverview: '生产计划审核领域',
        modelingBrief: brief,
        candidateObjects: [],
        openQuestions: [],
      },
    };
  };
  t.after(() => { materialAnalysisTask = null; });

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/analyze`, headers,
  });

  // 分析改到后台跑，请求只负责受理：立刻返回，任务已是执行中
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(response.json().accepted, true);
  assert.equal(response.json().packageRevision, current.revision.oid);
  assert.equal(
    response.json().aiConstruction.tasks.find((task: any) => task.kind === 'material_analysis').status,
    'running',
  );

  const materialTask = await waitForMaterialTask(projectId, ['completed']);
  assert.equal(materialTask.outputRevisionOid, current.revision.oid);
  assert.deepEqual(progress, [42]);
});

test('资料分析 API：分析期间资料变化时拒绝挂接旧结果并保留可重试状态', async (t) => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '资料分析陈旧保护项目' },
  });
  const projectId = created.json().project.id;
  const [{ getPackageArtifactStore }, { insertDocumentRevision }] = await Promise.all([
    import('../src/artifacts/runtime.js'),
    import('../src/documents/repo.js'),
  ]);
  const current = await getPackageArtifactStore().initializeDraftPackage(projectId, {
    id: 'material-analysis-stale',
    name: '资料分析陈旧保护项目',
    description: '验证分析期间资料修订变化会中止旧结果',
  });
  const { graph, brief } = await advanceModelingProjectToMaterialAnalysis(projectId, current.revision.oid);
  insertDocumentRevision({
    projectId,
    filename: '审批规则.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '初始审批规则。',
  });
  materialAnalysisTask = async () => {
    insertDocumentRevision({
      projectId,
      filename: '审批规则.md',
      kind: 'markdown',
      parseStatus: 'parsed',
      textContent: '分析期间更新的审批规则。',
    });
    return {
      node: { id: 'analysis-stale-node', projectId, type: 'analyze', status: 'done', ordinal: 1 },
      analysis: {
        domainOverview: '旧分析',
        modelingBrief: brief,
        candidateObjects: [],
        openQuestions: [],
      },
    };
  };
  t.after(() => { materialAnalysisTask = null; });

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/analyze`, headers,
  });

  // 受理是同步的，陈旧判定发生在后台——失败状态和重试出口走助手消息，不再是 409
  assert.equal(response.statusCode, 202, response.body);
  const materialTask = await waitForMaterialTask(projectId, ['failed']);
  assert.equal(materialTask.status, 'failed');
  // 失败原因写在助手消息里（fail_task 本身不带 reason），用户看到的也是这条
  const { getDb: getDbForMessage } = await import('../src/db/index.js');
  const failureMessage = getDbForMessage().prepare(
    `SELECT blocks_json FROM ai_construction_messages
     WHERE project_id = ? AND task_id = ? ORDER BY seq DESC LIMIT 1`,
  ).get(projectId, materialTask.id) as { blocks_json?: string } | undefined;
  assert.match(String(failureMessage?.blocks_json ?? ''), /建模资料已变化/);
  // 并且给出重试出口，而不是让界面停在那儿
  assert.match(String(failureMessage?.blocks_json ?? ''), /analyze_materials/);
});

test('版本汇总 API：按工件修订关联评测并单列未版本化的当前修订', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '评测修订关联项目' },
  });
  const projectId = created.json().project.id;
  const [{ getPackageArtifactStore }, m3repo] = await Promise.all([
    import('../src/artifacts/runtime.js'),
    import('../src/workflow/m3-repo.js'),
  ]);
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(projectId);
  const versioned = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: {
      id: 'revision-linked-package',
      name: '评测修订关联项目',
      description: '验证评测只按工件修订关联',
      version: '1.0.0',
      status: 'review',
      language: 'zh-CN',
      defaultProviderAlias: 'data',
      objects: [],
      operations: [],
    },
    coverage: { covered: [], uncovered: [] },
    note: '已版本化基线',
    action: {
      cause: 'test_version', summary: '创建已版本化基线', actorKind: 'user', actorName: 'alice',
    },
  });
  const exactVersionRound = m3repo.createRound(
    projectId,
    '错误的展示版本也不影响修订关联',
    2,
    versioned.revision.oid,
  );
  m3repo.roundProgress(exactVersionRound.id, 2, 1);
  m3repo.finishRound(exactVersionRound.id, 'done');
  const sameNameWrongRevisionRound = m3repo.createRound(
    projectId,
    '1.0.0',
    3,
    'f'.repeat(40),
  );

  const indexFile = await store.readFile(projectId, 'package/index.md');
  const edited = await store.saveFile(projectId, {
    expectedRevision: versioned.revision.oid,
    path: 'package/index.md',
    content: `${indexFile.content}\n当前人工修订。\n`,
    action: {
      cause: 'test_edit', summary: '形成未版本化当前修订', actorKind: 'user', actorName: 'alice',
    },
  });
  const currentRound = m3repo.createRound(projectId, '1.0.0', 1, edited.revision.oid);

  const response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/versions`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  const body = response.json();
  assert.equal(body.currentRevision, edited.revision.oid);
  assert.equal(body.versions.length, 2);

  const releasedVersion = body.versions.find((version: any) => !version.unversioned);
  assert.equal(releasedVersion.revision, versioned.revision.oid);
  assert.equal(releasedVersion.current, false);
  assert.deepEqual(releasedVersion.rounds.map((round: any) => round.id), [exactVersionRound.id]);

  const currentVersion = body.versions.find((version: any) => version.unversioned);
  assert.equal(currentVersion.revision, edited.revision.oid);
  assert.equal(currentVersion.current, true);
  assert.equal(currentVersion.note, '当前未版本化修订');
  assert.deepEqual(currentVersion.rounds.map((round: any) => round.id), [currentRound.id]);
  assert.equal(body.versions.some((version: any) => (
    version.rounds.some((round: any) => round.id === sameNameWrongRevisionRound.id)
  )), false);
});

test('知识包检查查询隐藏旧修订发现并声明当前修订需要重检', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '检查修订一致性项目' },
  });
  assert.equal(created.statusCode, 201, created.body);
  const projectId = created.json().project.id;
  const [{ getPackageArtifactStore }, { saveCheck }] = await Promise.all([
    import('../src/artifacts/runtime.js'),
    import('../src/workflow/package-check.js'),
  ]);
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(projectId);
  const checkedRevision = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: {
      id: 'check-revision-package',
      name: '检查修订一致性知识包',
      description: '验证旧检查结果不会作为当前修订问题展示',
      version: '1.0.0-rc.1',
      language: 'zh-CN',
      status: 'review',
      defaultProviderAlias: 'data',
      objects: [],
      operations: [],
    },
    coverage: { covered: [], uncovered: [] },
    note: '形成已检查修订',
    action: {
      cause: 'generation', summary: '形成已检查修订', actorKind: 'agent', actorName: '助手',
    },
  });
  saveCheck(projectId, checkedRevision.packageVersion, [{
    id: 'old-finding', source: 'rule', severity: '错误', category: '旧问题',
    target: 'package/index.md', message: '只属于旧修订的问题',
  }], checkedRevision.revision.oid);

  const indexFile = await store.readFile(projectId, 'package/index.md');
  const current = await store.saveFile(projectId, {
    expectedRevision: checkedRevision.revision.oid,
    path: 'package/index.md',
    content: `${indexFile.content}\n当前修订已经变化。\n`,
    action: {
      cause: 'manual_edit', summary: '形成待重检修订', actorKind: 'user', actorName: 'alice',
    },
  });

  const response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/check`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().currentRevision, current.revision.oid);
  assert.equal(response.json().stale, true);
  assert.equal(response.json().check, null);
  assert.equal(response.json().warningReview, null);
  assert.equal(response.json().errorDeferral, null);
  assert.doesNotMatch(response.body, /old-finding|只属于旧修订的问题/);
});

test('验证候选完成发布警告确认后提升为对应稳定版本并固化已知限制', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '发布候选转正式版' },
  });
  assert.equal(created.statusCode, 201, created.body);
  const projectId = created.json().project.id;
  const [
    { getDb },
    { getPackageArtifactStore },
    m3repo,
    { saveCheck },
    { getReleaseKnownLimitations },
  ] = await Promise.all([
    import('../src/db/index.js'),
    import('../src/artifacts/runtime.js'),
    import('../src/workflow/m3-repo.js'),
    import('../src/workflow/package-check.js'),
    import('../src/workflow/warning-acceptance.js'),
  ]);
  getDb().prepare("UPDATE projects SET kind = 'package' WHERE id = ?").run(projectId);
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(projectId);
  const generated = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: {
      id: 'release-candidate-package',
      name: '发布候选知识包',
      description: '用于验证发布候选提升为稳定版本的知识包',
      version: '1.2.0',
      language: 'zh-CN',
      status: 'draft',
      defaultProviderAlias: 'data',
      objects: [],
      operations: [],
    },
    coverage: { covered: [], uncovered: [] },
    note: '生成发布基线',
    action: {
      cause: 'generation', summary: '生成发布基线',
      actorKind: 'agent', actorName: '智能助手',
    },
  });
  const candidate = await store.saveCurrentAsPackageVersion(projectId, {
    expectedRevision: generated.revision.oid,
    version: '1.3.0-rc.2',
    status: 'review',
    note: '验证候选',
    action: {
      cause: 'validation_candidate', summary: '创建验证候选',
      actorKind: 'user', actorName: 'alice',
    },
  });
  saveCheck(projectId, candidate.packageVersion, [{
    id: 'warning-release', source: 'rule', severity: '警告', category: '口径待确认',
    target: 'metrics/score.md', message: '边界口径将在后续版本继续收敛',
  }], candidate.revision.oid);
  const checked = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/check`, headers,
  });
  assert.equal(checked.json().currentRevision, candidate.revision.oid);
  assert.equal(checked.json().stale, false);
  assert.equal(checked.json().check.revision, candidate.revision.oid);
  const warning = checked.json().warningReview.items[0];
  const accepted = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/warnings/accept`, headers,
    payload: {
      revision: candidate.revision.oid,
      fingerprints: [warning.fingerprint],
      note: '已确认只影响边界说明，不改变当前主流程结论',
      phase: 'validation',
    },
  });
  assert.equal(accepted.statusCode, 200, accepted.body);
  const testcase = m3repo.addTestcase(projectId, '如何处理申请？', '按确认流程处理');
  const round = m3repo.createRound(
    projectId, candidate.packageVersion, 1, candidate.revision.oid,
  );
  m3repo.addResult(round.id, {
    testcaseId: testcase.id,
    question: testcase.question,
    groundTruth: testcase.groundTruth,
    finalAnswer: '按确认流程处理',
    verdict: 'pass',
  });
  m3repo.roundProgress(round.id, 1, 1);
  m3repo.finishRound(round.id, 'done');

  const blocked = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/release`, headers,
  });
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().gates.find((gate: any) => gate.id === 'warning_review').passed, false);

  const releaseConfirmed = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/warnings/accept`, headers,
    payload: {
      revision: candidate.revision.oid,
      fingerprints: [warning.fingerprint],
      note: '同意作为本次正式版本的已知限制并在后续修复',
      phase: 'release',
    },
  });
  assert.equal(releaseConfirmed.statusCode, 200, releaseConfirmed.body);

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/release`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().version, '1.3.0');
  assert.match(response.json().revision, /^[0-9a-f]{40}$/);
  const released = await store.current(projectId);
  assert.equal(released?.packageVersion, '1.3.0');
  assert.equal(released?.spec.status, 'released');
  assert.equal(getReleaseKnownLimitations(projectId, '1.3.0').length, 1);
});

test('人工编辑草稿 API：创建恢复、文件保存、revision 冲突、撤销与放弃', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '人工编辑草稿项目' },
  });
  const projectId = created.json().project.id;

  let response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PACKAGE_DRAFT_BASE_VERSION_REQUIRED');

  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'draft-route-package', name: '草稿路由包', description: '草稿路由包', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'Order', title: '订单', description: '订单对象', sourceTable: 'Orders',
      fields: [{ key: 'id', cnName: '订单ID', type: 'string', required: true }],
    }],
    operations: [{
      key: 'query_order', title: '查询订单', description: '查询订单',
      inputs: [], outputs: [], flow: ['查询 object:Order'], relatedObjects: ['Order'],
    }],
  }, { covered: [], uncovered: [] });

  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.statusCode, 201, response.body);
  const draft = response.json().draft;
  assert.equal(response.json().created, true);
  assert.equal(draft.baseVersion, '1.0.0');
  assert.equal(draft.revision, 0);

  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().created, false);
  assert.equal(response.json().draft.id, draft.id);

  response = await app.inject({
    method: 'GET',
    url: `/api/projects/${projectId}/package/draft/file?path=${encodeURIComponent('operations/query_order.md')}`,
    headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  const baseContent = response.json().file.baseContent;
  const baseContentHash = response.json().file.contentHash;
  const editedContent = baseContent.replace('查询订单', '查询最新订单');

  response = await app.inject({
    method: 'PUT', url: `/api/projects/${projectId}/package/draft/file`, headers,
    payload: { path: 'operations/query_order.md', content: editedContent, revision: 0 },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().draft.revision, 1);
  assert.equal(response.json().draft.changedFileCount, 1);
  assert.equal(response.json().file.content, editedContent);
  const editedContentHash = response.json().file.contentHash;

  response = await app.inject({
    method: 'PUT', url: `/api/projects/${projectId}/package/draft/file`, headers,
    payload: {
      path: 'operations/query_order.md', content: `${editedContent}\n过期覆盖`, revision: 0,
      expectedContentHash: baseContentHash,
    },
  });
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PACKAGE_DRAFT_REVISION_CONFLICT');
  assert.equal(response.json().currentRevision, 1);
  assert.equal(response.json().conflictScope, 'file');

  response = await app.inject({
    method: 'PUT', url: `/api/projects/${projectId}/package/draft/file`, headers,
    payload: {
      path: 'operations/query_order.md', content: `${editedContent}\n旧草稿覆盖`, revision: 1,
      expectedContentHash: editedContentHash,
      draftId: 'pd_replaced_draft',
    },
  });
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PACKAGE_DRAFT_REVISION_CONFLICT');
  assert.equal(response.json().conflictScope, 'draft');
  assert.equal(response.json().currentDraftId, draft.id);

  response = await app.inject({
    method: 'DELETE',
    url: `/api/projects/${projectId}/package/draft/file?path=${encodeURIComponent('operations/query_order.md')}&revision=1`,
    headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().draft.revision, 2);
  assert.equal(response.json().draft.changedFileCount, 0);
  assert.equal(response.json().file.changed, false);

  response = await app.inject({
    method: 'DELETE', url: `/api/projects/${projectId}/package/draft`, headers,
    payload: { revision: 2 },
  });
  assert.equal(response.statusCode, 400, response.body);

  response = await app.inject({
    method: 'DELETE', url: `/api/projects/${projectId}/package/draft`, headers,
    payload: { confirmed: true, revision: 2 },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().discarded.status, 'discarded');
  assert.equal(response.json().discarded.revision, 3);

  response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().draft, null);

  const { createProject } = await import('../src/projects/repo.js');
  const foreignProject = createProject({ owner: 'bob', name: '其他用户草稿项目' });
  response = await app.inject({
    method: 'GET', url: `/api/projects/${foreignProject.id}/package/draft`, headers,
  });
  assert.equal(response.statusCode, 404, response.body);
});

test('人工编辑草稿发布 API：确认后一次生成正式版本并恢复读取与导出', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '人工草稿发布项目' },
  });
  const projectId = created.json().project.id;
  const [{ insertPackageVersion }, { insertDocument }] = await Promise.all([
    import('../src/workflow/m2-repo.js'),
    import('../src/documents/repo.js'),
  ]);
  insertDocument({
    projectId,
    filename: 'orders.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'Orders', rowCount: 1,
      fields: [{
        name: 'order_id', inferredType: 'string', sampleValues: ['O-1'], distinctRatio: 1,
      }],
    }],
  });
  insertPackageVersion(projectId, '1.0.0', {
    id: 'draft-publish-route', name: '人工草稿发布项目', description: '人工草稿发布项目', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'Order', title: '订单', description: '用于查询订单详情的业务对象', sourceTable: 'Orders', primaryKey: ['id'],
      fields: [{ key: 'id', cnName: '订单ID', type: 'string', required: true, sourceColumn: 'order_id' }],
    }],
    operations: [{
      key: 'query_order', title: '查询订单', description: '根据订单ID查询订单详情',
      inputs: [{ param: 'id', cnName: '订单ID', type: 'string', required: true }],
      outputs: [{ field: 'order', cnName: '订单', type: 'object' }],
      flow: ['根据 id 查询 object:Order'], relatedObjects: ['Order'],
    }],
  }, { covered: [], uncovered: [] });

  let response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.statusCode, 201, response.body);
  response = await app.inject({
    method: 'GET',
    url: `/api/projects/${projectId}/package/draft/file?path=${encodeURIComponent('operations/query_order.md')}`,
    headers,
  });
  const edited = response.json().file.content.replaceAll('根据订单ID查询订单详情', '根据订单编号查询最新订单详情');
  response = await app.inject({
    method: 'PUT', url: `/api/projects/${projectId}/package/draft/file`, headers,
    payload: { path: 'operations/query_order.md', content: edited, revision: 0 },
  });
  assert.equal(response.statusCode, 200, response.body);

  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft/publish`, headers,
    payload: { revision: 1 },
  });
  assert.equal(response.statusCode, 400, response.body);
  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft/publish`, headers,
    payload: { confirmed: true, revision: 0 },
  });
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PACKAGE_DRAFT_REVISION_CONFLICT');
  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft/publish`, headers,
    payload: { confirmed: true, revision: 1, summary: '修订订单查询说明' },
  });
  assert.equal(response.statusCode, 201, response.body);
  assert.equal(response.json().published.version, '1.0.0-rc.1');
  assert.equal(response.json().published.draft.status, 'published');
  assert.deepEqual(response.json().published.changedFiles, ['operations/query_order.md']);

  response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.json().draft, null);
  response = await app.inject({
    method: 'GET',
    url: `/api/projects/${projectId}/package/file?version=1.0.0-rc.1&path=${encodeURIComponent('operations/query_order.md')}`,
    headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.match(response.json().content, /根据订单编号查询最新订单详情/);
  response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/export?version=1.0.0-rc.1`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.headers['content-type'], 'application/zip');
});

test('活动人工草稿统一门禁：建模写入、修缮和知识消费入口均要求先发布或放弃', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '人工草稿统一门禁项目' },
  });
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'draft-gate-route', name: '人工草稿统一门禁项目', description: '人工草稿统一门禁项目', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  const draftResponse = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(draftResponse.statusCode, 201, draftResponse.body);
  const [{ AiConstructionTaskGraph }, { getPackageArtifactStore }] = await Promise.all([
    import('../src/ai-construction/task-graph.js'),
    import('../src/artifacts/runtime.js'),
  ]);
  const construction = new AiConstructionTaskGraph().snapshot(projectId)!;
  const currentPackage = await getPackageArtifactStore().current(projectId);

  const gatedRequests: Array<{ method: any; url: string; payload?: unknown; operation: string }> = [
    {
      method: 'POST',
      url: `/api/projects/${projectId}/ai-construction/commands`,
      payload: {
        command: {
          type: 'confirm_milestone',
          expectedRevision: construction.revision,
          milestoneId: 'goal_confirmed',
          packageRevisionOid: currentPackage!.revision.oid,
          actor: { username: 'ignored', role: 'project_owner' },
        },
      },
      operation: 'milestone-confirm:goal_confirmed',
    },
    {
      method: 'POST',
      url: `/api/projects/${projectId}/ai-construction/knowledge/generate`,
      payload: {},
      operation: 'knowledge-generate',
    },
    {
      method: 'POST',
      url: `/api/projects/${projectId}/ai-construction/knowledge/repair`,
      payload: {},
      operation: 'controlled-repair',
    },
    { method: 'POST', url: `/api/projects/${projectId}/package/chat`, payload: {}, operation: 'package-chat' },
    { method: 'POST', url: `/api/projects/${projectId}/package/check`, payload: {}, operation: 'package-check' },
    { method: 'GET', url: `/api/projects/${projectId}/package/export`, operation: 'package-export' },
    { method: 'PUT', url: `/api/projects/${projectId}/data-source`, payload: {}, operation: 'data-source-update' },
    { method: 'POST', url: `/api/projects/${projectId}/graph-import/uploads`, payload: {}, operation: 'graph-import-upload' },
    { method: 'POST', url: `/api/projects/${projectId}/graph-import/uploads/missing/validate`, payload: {}, operation: 'graph-import-validate' },
    { method: 'POST', url: `/api/projects/${projectId}/graph-import/uploads/missing/execute`, payload: {}, operation: 'graph-import-execute' },
    { method: 'POST', url: `/api/projects/${projectId}/evaluate`, payload: {}, operation: 'evaluate' },
    { method: 'POST', url: `/api/projects/${projectId}/optimize/propose`, payload: {}, operation: 'optimize-propose' },
    { method: 'POST', url: `/api/projects/${projectId}/optimize/apply`, payload: {}, operation: 'optimize-apply' },
  ];
  for (const request of gatedRequests) {
    const response = await app.inject({
      method: request.method,
      url: request.url,
      headers,
      payload: request.payload,
    });
    assert.equal(response.statusCode, 409, `${request.operation}: ${response.body}`);
    const body = response.json();
    assert.equal(body.code, 'PACKAGE_DRAFT_REQUIRES_PUBLISH');
    assert.equal(body.operation, request.operation);
    assert.equal(body.baseVersion, '1.0.0');
    assert.equal(body.revision, 0);
    assert.equal(body.changedFileCount, 0);
    assert.deepEqual(body.changedFiles, []);
  }

  const discarded = await app.inject({
    method: 'DELETE', url: `/api/projects/${projectId}/package/draft`, headers,
    payload: { confirmed: true, revision: 0 },
  });
  assert.equal(discarded.statusCode, 200, discarded.body);
  const exportResponse = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/export?version=1.0.0`, headers,
  });
  assert.equal(exportResponse.statusCode, 200, exportResponse.body);
});

test('资料上传 API 保留不可变修订，仅格式变化不传播失效', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers, payload: { name: '资料修订项目' },
  });
  const projectId = created.json().project.id;
  const firstForm = multipartFile('policy.md', Buffer.from('规则 A\n阈值 10'));
  const first = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/documents`,
    headers: { ...headers, ...firstForm.headers }, payload: firstForm.payload,
  });
  const nextForm = multipartFile('policy.md', Buffer.from('  规则 A  \r\n\r\n 阈值   10  '));
  const next = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/documents`,
    headers: { ...headers, ...nextForm.headers }, payload: nextForm.payload,
  });

  assert.equal(first.statusCode, 201, first.body);
  assert.equal(next.statusCode, 201, next.body);
  assert.equal(next.json().document.revisionNo, 2);
  assert.equal(next.json().materialRevision.changeKind, 'format_only');
  assert.equal(next.json().materialRevision.semanticChanged, false);
  assert.deepEqual(next.json().materialImpact.affectedResources, []);

  const history = await app.inject({
    method: 'GET',
    url: `/api/projects/${projectId}/documents/revisions?sourceKey=${encodeURIComponent(first.json().document.sourceKey)}`,
    headers,
  });
  assert.equal(history.statusCode, 200, history.body);
  assert.deepEqual(history.json().revisions.map((item: any) => [item.revisionNo, item.lifecycleStatus]), [
    [2, 'active'], [1, 'superseded'],
  ]);

  const removed = await app.inject({
    method: 'DELETE', url: `/api/projects/${projectId}/documents/${next.json().document.id}`, headers,
  });
  assert.equal(removed.statusCode, 200, removed.body);
  assert.deepEqual(removed.json().documents, []);
  const retiredHistory = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/documents/revisions`, headers,
  });
  assert.equal(retiredHistory.json().revisions[0].lifecycleStatus, 'deleted');
});

test('包修缮运行期间人工草稿只读，所有写接口统一返回 409', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '包修缮期间禁止人工编辑项目' },
  });
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'repair-edit-gate', name: '包修缮期间禁止人工编辑', description: '包修缮期间禁止人工编辑', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
  }, { covered: [], uncovered: [] });

  let startPlan!: () => void;
  let releasePlan!: () => void;
  const planStarted = new Promise<void>((resolve) => { startPlan = resolve; });
  const planBlocked = new Promise<void>((resolve) => { releasePlan = resolve; });
  packageChatPlanTask = async () => {
    startPlan();
    await planBlocked;
    return { reply: '修缮影响分析完成' };
  };
  const planRequest = app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/chat`, headers,
    payload: { message: '检查需要修缮的内容', history: [] },
  });
  await planStarted;
  try {
    const readablePackage = await app.inject({
      method: 'GET', url: `/api/projects/${projectId}/package`, headers,
    });
    assert.equal(readablePackage.statusCode, 200, readablePackage.body);
    const readableDraft = await app.inject({
      method: 'GET', url: `/api/projects/${projectId}/package/draft`, headers,
    });
    assert.equal(readableDraft.statusCode, 200, readableDraft.body);
    assert.equal(readableDraft.json().draft, null);

    const blockedRequests: Array<{ method: any; url: string; payload?: unknown }> = [
      { method: 'POST', url: `/api/projects/${projectId}/package/draft`, payload: {} },
      {
        method: 'PUT', url: `/api/projects/${projectId}/package/draft/file`,
        payload: { path: 'operations/query_order.md', content: '修改内容', revision: 0 },
      },
      {
        method: 'DELETE',
        url: `/api/projects/${projectId}/package/draft/file?path=${encodeURIComponent('operations/query_order.md')}&revision=0`,
      },
      {
        method: 'DELETE', url: `/api/projects/${projectId}/package/draft`,
        payload: { confirmed: true, revision: 0 },
      },
      {
        method: 'POST', url: `/api/projects/${projectId}/package/draft/publish`,
        payload: { confirmed: true, revision: 0 },
      },
    ];
    for (const request of blockedRequests) {
      const response = await app.inject({ ...request, headers } as any);
      assert.equal(response.statusCode, 409, response.body);
      assert.equal(response.json().code, 'PACKAGE_REPAIR_IN_PROGRESS');
      assert.equal(response.json().error, '知识包修缮正在进行中，暂不能人工编辑');
      assert.equal(response.json().operation, 'package-chat');
    }
  } finally {
    releasePlan();
    const response = await planRequest;
    packageChatPlanTask = null;
    assert.equal(response.statusCode, 202, response.body);
    await new Promise<void>((resolve) => setImmediate(resolve));
  }

  const draftAfterRepair = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
    payload: {},
  });
  assert.equal(draftAfterRepair.statusCode, 201, draftAfterRepair.body);
});

test('主建模聊天把全部资料清单与当前文本材料分别交给 packageChatPlan', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '全文阅读聊天材料加载' },
  });
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { insertDocument } = await import('../src/documents/repo.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'full-read-chat', name: '全文阅读聊天材料加载', description: '测试', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  const document = insertDocument({
    projectId, filename: '营销产品手册.docx', kind: 'word', parseStatus: 'parsed',
    textContent: '第一章 产品准入条件',
  });
  const spreadsheet = insertDocument({
    projectId, filename: 'dataset.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: 'Data', fields: [], rowCount: 2 }],
  });

  let receivedOptions: any;
  let finish!: () => void;
  const planned = new Promise<void>((resolve) => { finish = resolve; });
  packageChatPlanTask = async (_project, _history, _message, options) => {
    receivedOptions = options;
    finish();
    return { reply: '已处理' };
  };
  try {
    const response = await app.inject({
      method: 'POST', url: `/api/projects/${projectId}/package/chat`, headers,
      payload: { message: '把这份文档全文看完' },
    });
    assert.equal(response.statusCode, 202, response.body);
    await planned;
    assert.equal(receivedOptions.documentAccess, 'full-read-only');
    assert.deepEqual(receivedOptions.documents, [{
      id: document.id,
      filename: document.filename,
      textContent: '第一章 产品准入条件',
      revisionFingerprint: [
        document.id, document.sourceKey, document.revisionNo,
        document.contentHash, document.semanticHash,
      ].join(':'),
    }]);
    assert.deepEqual(
      [...receivedOptions.materials].sort((left, right) => left.filename.localeCompare(right.filename)),
      [
        {
          id: spreadsheet.id, filename: 'dataset.xlsx', kind: 'excel', parseStatus: 'parsed',
          textLength: 0, tableCount: 1,
        },
        {
          id: document.id, filename: '营销产品手册.docx', kind: 'word', parseStatus: 'parsed',
          textLength: '第一章 产品准入条件'.length, tableCount: 0,
        },
      ].sort((left, right) => left.filename.localeCompare(right.filename)),
    );
  } finally {
    packageChatPlanTask = null;
  }
});

test('旧版单文件 override 写接口已永久停用且不会落库', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '旧 override 接口停用项目' },
  });
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { getDb } = await import('../src/db/index.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'override-removed', name: '旧接口停用', description: '旧接口停用', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
  }, { covered: [], uncovered: [] });

  for (const request of [
    { method: 'PUT', url: `/api/projects/${projectId}/package/file`, payload: { path: 'index.md', content: 'changed' } },
    { method: 'DELETE', url: `/api/projects/${projectId}/package/file?path=index.md` },
  ]) {
    const response = await app.inject({ ...request, headers } as any);
    assert.equal(response.statusCode, 410, response.body);
    assert.equal(response.json().code, 'PACKAGE_FILE_OVERRIDE_REMOVED');
  }
  assert.equal((getDb().prepare(
    'SELECT COUNT(*) AS count FROM package_overrides WHERE project_id = ?',
  ).get(projectId) as { count: number }).count, 0);
});

test('项目数据源 API：保存脱敏配置并通过注入依赖推进阶段', async () => {
  consumerRequests.length = 0;
  testedDataSources.length = 0;
  syncedDataSources.length = 0;
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: 'Neo4j 数据源项目' },
  });
  assert.equal(created.statusCode, 201);
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'ontology-001', name: 'Neo4j 数据源项目', description: 'Neo4j 数据源项目', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'graph', objects: [], operations: [],
  }, { covered: [], uncovered: [] });
  const saved = await app.inject({
    method: 'PUT', url: `/api/projects/${projectId}/data-source`, headers,
    payload: neo4jInput({ database: 'domain' }),
  });
  assert.equal(saved.statusCode, 200, saved.body);
  const dataSource = saved.json().dataSource;
  assert.equal(dataSource.provider, 'neo4j');
  assert.equal(dataSource.config.has_password, true);
  assert.equal('password' in dataSource.config, false);
  assert.equal(dataSource.workspaceId, 'test');
  assert.equal(dataSource.ontologyId, 'ontology-001');
  assert.equal(testedDataSources.length, 1);
  assert.equal(syncedDataSources.length, 1);
  assert.deepEqual(
    {
      projectId: syncedDataSources[0].projectId,
      workspaceId: syncedDataSources[0].workspaceId,
      ontologyId: syncedDataSources[0].ontologyId,
    },
    { projectId, workspaceId: 'test', ontologyId: 'ontology-001' },
  );
  assert.equal(syncedDataSources[0].token, token);
  assert.equal(syncedDataSources[0].source.config.password, 'secret');
  assert.equal(consumerRequests.length, 0, `测试注入后不应真实访问 consumer：${consumerRequests.join(', ')}`);
  const project = await app.inject({ method: 'GET', url: `/api/projects/${projectId}`, headers });
  assert.equal(project.json().project.stage, 'data_source');
  assert.deepEqual(project.json().dataSource, dataSource);
});

test('NL2SQL 评测库 API：使用 EVAL_DB_URL 建库并按已保存 Scene 导入 Excel', async () => {
  evaluationDatabaseImportTasks.length = 0;
  evaluationDatabaseConnections.length = 0;
  evaluationDatabaseQueries.length = 0;
  projectDatabaseAdminConnections.length = 0;
  projectDatabaseAdminQueries.length = 0;
  projectDatabaseExists = false;
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: 'NL2SQL 评测库项目' },
  });
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { getDb } = await import('../src/db/index.js');
  insertPackageVersion(projectId, '1.0.0', {
    id: 'nl2sql-api', name: 'NL2SQL 评测库项目', description: 'NL2SQL 评测库项目', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'default', objects: [], operations: [],
  }, { covered: [], uncovered: [] });

  const { projectEvaluationDatabaseName } = await import(
    '../src/evaluation/project-evaluation-database.js'
  );
  const expectedDatabase = projectEvaluationDatabaseName(projectId);
  const target = await app.inject({
    method: 'GET',
    url: `/api/projects/${projectId}/evaluation-database/target`,
    headers,
  });
  assert.equal(target.statusCode, 200, target.body);
  assert.deepEqual(target.json(), {
    database: expectedDatabase,
    configured: true,
    databaseUrlPreview: 'mysql://root:******@mysql.local:3306',
  });
  assert.doesNotMatch(target.body, /eval-secret/);

  const unconfirmedDatabase = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/project-database`,
    headers,
  });
  assert.equal(unconfirmedDatabase.statusCode, 400, unconfirmedDatabase.body);
  assert.match(unconfirmedDatabase.json().error, /确认.*EVAL_DB_URL/);

  const customDatabaseUrl = 'mysql://project:project-secret@project.mysql:3308/original';
  const createdDatabase = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/project-database`,
    headers,
    payload: { database_url_mode: 'custom', database_url: customDatabaseUrl },
  });
  assert.equal(createdDatabase.statusCode, 201, createdDatabase.body);
  assert.deepEqual(createdDatabase.json(), {
    database: expectedDatabase, created: true,
  });
  const reusedDatabase = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/project-database`,
    headers,
    payload: { database_url_mode: 'default' },
  });
  assert.equal(reusedDatabase.statusCode, 200, reusedDatabase.body);
  assert.equal(reusedDatabase.json().created, false);
  assert.equal(projectDatabaseAdminQueries.filter((sql) => sql.startsWith('CREATE DATABASE')).length, 1);
  assert.match(projectDatabaseAdminQueries.join('\n'), new RegExp(expectedDatabase));
  assert.deepEqual(projectDatabaseAdminConnections[0], {
    connectionUrl: 'mysql://project:project-secret@project.mysql:3308/',
    database: expectedDatabase,
  });
  assert.deepEqual(projectDatabaseAdminConnections[1], {
    connectionUrl: 'mysql://root:eval-secret@mysql.local:3306/',
    database: expectedDatabase,
  });
  assert.doesNotMatch(createdDatabase.body, /project-secret|eval-secret/);

  const saved = await app.inject({
    method: 'PUT', url: `/api/projects/${projectId}/data-source`, headers,
    payload: nl2sqlInput({ scene: expectedDatabase }),
  });
  assert.equal(saved.statusCode, 200, saved.body);
  assert.equal(saved.json().dataSource.workspaceId, 'nl2sql-eval');
  assert.deepEqual(saved.json().dataSource.config, {
    url: 'http://nl2sql:8080', scene: expectedDatabase,
    evaluation_workspace_id: 'nl2sql-eval', has_token: true,
  });
  const raw = getDb().prepare('SELECT public_config_json FROM project_data_sources WHERE project_id = ?')
    .get(projectId) as { public_config_json: string };
  assert.doesNotMatch(raw.public_config_json, /eval-secret/);

  const excel = workbookBuffer({
    orders: [['id', 'active', 'name'], ['1', '是', '测试订单']],
  });
  const unconfirmedForm = multipartEvaluationDatabaseExcel(
    'unconfirmed.xlsx', excel, false,
  );
  const unconfirmed = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/import`,
    headers: { authorization: `Bearer ${token}`, ...unconfirmedForm.headers },
    payload: unconfirmedForm.payload,
  });
  assert.equal(unconfirmed.statusCode, 400, unconfirmed.body);
  assert.match(unconfirmed.json().error, /确认.*同名表/);
  assert.equal(evaluationDatabaseImportTasks.length, 0);

  const unconfirmedTargetForm = multipartEvaluationDatabaseExcel(
    'unconfirmed-target.xlsx', excel, true, '',
  );
  const unconfirmedTarget = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/import`,
    headers: { authorization: `Bearer ${token}`, ...unconfirmedTargetForm.headers },
    payload: unconfirmedTargetForm.payload,
  });
  assert.equal(unconfirmedTarget.statusCode, 400, unconfirmedTarget.body);
  assert.match(unconfirmedTarget.json().error, /确认.*EVAL_DB_URL/);
  assert.equal(evaluationDatabaseImportTasks.length, 0);

  const importDatabaseUrl = 'mysql://importer:import-secret@import.mysql:3309/ignored';
  const form = multipartEvaluationDatabaseExcel(
    'evaluation.xlsx', excel, true, 'custom', importDatabaseUrl,
  );
  const imported = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/import`,
    headers: { authorization: `Bearer ${token}`, ...form.headers },
    payload: form.payload,
  });
  assert.equal(imported.statusCode, 202, imported.body);
  assert.equal(imported.json().run.status, 'running');
  assert.equal(evaluationDatabaseImportTasks.length, 1);
  assert.equal(evaluationDatabaseQueries.length, 0, 'HTTP 响应返回前不应执行写库 SQL');
  const persistedRun = getDb().prepare(
    'SELECT * FROM evaluation_database_import_runs WHERE id = ?',
  ).get(imported.json().run.id);
  assert.doesNotMatch(JSON.stringify(persistedRun), /eval-secret|import-secret|import\.mysql/);

  const running = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/evaluation-database/imports`, headers,
  });
  assert.equal(running.statusCode, 200, running.body);
  assert.equal(running.json().imports[0].status, 'running');

  const duplicateForm = multipartEvaluationDatabaseExcel('duplicate.xlsx', excel);
  const duplicate = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/evaluation-database/import`,
    headers: { authorization: `Bearer ${token}`, ...duplicateForm.headers },
    payload: duplicateForm.payload,
  });
  assert.equal(duplicate.statusCode, 409, duplicate.body);
  assert.equal(duplicate.json().run.id, imported.json().run.id);
  assert.equal(evaluationDatabaseImportTasks.length, 1);
  const {
    assertProjectEvaluationDatabaseImportAvailable,
    EvaluationDatabaseImportCapacityError,
  } = await import('../src/evaluation/eval-db-import-service.js');
  assert.throws(
    () => assertProjectEvaluationDatabaseImportAvailable('another-project'),
    EvaluationDatabaseImportCapacityError,
  );

  await evaluationDatabaseImportTasks.shift()!();
  const finished = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/evaluation-database/imports`, headers,
  });
  assert.equal(finished.statusCode, 200, finished.body);
  assert.equal(finished.json().imports[0].status, 'done');
  assert.deepEqual(finished.json().imports[0].tables, [{ name: 'orders', rowCount: 1 }]);
  assert.deepEqual(evaluationDatabaseConnections[0], {
    connectionUrl: 'mysql://importer:import-secret@import.mysql:3309/',
    database: expectedDatabase,
  });
  assert.ok(evaluationDatabaseQueries.some((sql) => sql.startsWith('CREATE TABLE `')));
  assert.doesNotMatch(raw.public_config_json, /eval-secret|project-secret|import-secret/);
});

test('评测用例 API：手工新增和修改均推进项目阶段', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '手工评测用例项目' },
  });
  const projectId = created.json().project.id;
  const { setStage } = await import('../src/projects/repo.js');
  setStage(projectId, 'data_source');
  const added = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/testcases`, headers,
    payload: { question: '问题', groundTruth: '' },
  });
  assert.equal(added.statusCode, 200, added.body);
  let project = await app.inject({ method: 'GET', url: `/api/projects/${projectId}`, headers });
  assert.equal(project.json().project.stage, 'testcases');
  setStage(projectId, 'data_source');
  const updated = await app.inject({
    method: 'PATCH',
    url: `/api/projects/${projectId}/testcases/${added.json().testcase.id}`,
    headers,
    payload: { groundTruth: '答案' },
  });
  assert.equal(updated.statusCode, 200, updated.body);
  project = await app.inject({ method: 'GET', url: `/api/projects/${projectId}`, headers });
  assert.equal(project.json().project.stage, 'testcases');
});

test('评测用例批量导入 API 去重并一次推进项目阶段', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '批量评测用例项目' },
  });
  const projectId = created.json().project.id;
  const imported = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/testcases/batch`, headers,
    payload: { cases: [
      { question: '真实问题 A', groundTruth: '答案 A' },
      { question: '真实问题 B', groundTruth: '' },
      { question: '  真实问题 A  ', groundTruth: '不覆盖' },
    ] },
  });

  assert.equal(imported.statusCode, 200, imported.body);
  assert.deepEqual({ added: imported.json().added, skipped: imported.json().skipped }, { added: 2, skipped: 1 });
  assert.equal(imported.json().testcases.filter((item: any) => item.source === 'manual').length, 2);
  const project = await app.inject({ method: 'GET', url: `/api/projects/${projectId}`, headers });
  assert.equal(project.json().project.stage, 'testcases');
});
test('专家处置只记录下一步提示且不会改写 legacy 工作流节点', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '专家处置节点隔离项目' },
  });
  const projectId = created.json().project.id;
  const [{ getPackageArtifactStore }, diagnosisRepo, { upsertNode, getNode }] = await Promise.all([
    import('../src/artifacts/runtime.js'),
    import('../src/evaluation/failure-diagnosis-repo.js'),
    import('../src/workflow/engine.js'),
  ]);
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(projectId);
  const generated = await store.savePackageVersion(projectId, {
    expectedRevision: initial.oid,
    spec: {
      id: 'diagnosis-resolution-route', name: '专家处置节点隔离', description: '测试专家处置',
      version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
    },
    coverage: { covered: [], uncovered: [] },
    action: { cause: 'generate', summary: '生成测试知识包', actorKind: 'agent', actorName: '助手' },
  });
  const legacyNode = upsertNode({
    projectId, type: 'evaluate', status: 'done', ordinal: 900,
  });
  const [diagnosis] = diagnosisRepo.replaceFailureDiagnoses(
    projectId, 'round-resolution-route', '1.0.0', generated.revision.oid, [{
      category: 'data_gap', title: '业务口径待确认', summary: '需要专家确认业务口径',
      evidence: ['业务规则文档'], confidence: 'high', blocking: true,
      repairScope: 'data', packageOptimizable: false, resultIds: ['result-1'],
      relatedQuestions: ['如何处理？'], suggestedAction: '请专家确认口径',
    }],
  );

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/optimize/diagnoses/${diagnosis.id}/resolve`,
    headers,
    payload: { action: 'clarify_semantics', note: '确认该口径适用于所有同类业务。' },
  });

  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(response.json().nextSteps, [
    'generate_optimization_suggestions', 'rerun_evaluation',
  ]);
  assert.equal('staleSteps' in response.json(), false);
  assert.equal(getNode(legacyNode.id)?.status, 'done');
});

test('优化应用专家复核可由状态接口恢复，并支持带反馈重试或跳过', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '优化应用专家复核项目' },
  });
  const projectId = created.json().project.id;
  const [m4repo, progress] = await Promise.all([
    import('../src/workflow/m4-repo.js'),
    import('../src/evaluation/optimization-apply-progress.js'),
  ]);
  const suggestions = m4repo.insertSuggestions(
    projectId, 'round-review', '1.0.0', 'revision-review', [{
      title: '补充规则边界', detail: '补充失败场景所需规则', kind: 'modify_rule',
    }, {
      title: '跳过无效建议', detail: '该建议不应继续应用', kind: 'modify_object',
    }],
  );
  const retryTask = progress.startOptimizationApplyTask(projectId, [suggestions[0].id], 2);
  progress.waitOptimizationExpertReview(retryTask.id, '自动修订两次失败', ['修订未覆盖目标']);

  const restored = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/optimize/apply-status`, headers,
  });
  assert.equal(restored.statusCode, 200, restored.body);
  assert.equal(restored.json().task.node.id, retryTask.id);
  assert.equal(restored.json().task.node.status, 'waiting_user');
  assert.equal(restored.json().task.node.output.phase, 'expert_review');

  const retried = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/optimize/apply-review`, headers,
    payload: { taskId: retryTask.id, action: 'retry', note: '必须覆盖退款边界。' },
  });
  assert.equal(retried.statusCode, 200, retried.body);
  assert.deepEqual(retried.json(), { status: 'retry_ready', suggestionIds: [suggestions[0].id] });
  const retriedSuggestion = m4repo.getSuggestions([suggestions[0].id])[0];
  assert.equal(retriedSuggestion.status, 'pending');
  assert.match(retriedSuggestion.candidateFeedback ?? '', /修订未覆盖目标/);
  assert.match(retriedSuggestion.candidateFeedback ?? '', /必须覆盖退款边界/);

  const skipTask = progress.startOptimizationApplyTask(projectId, [suggestions[1].id], 2);
  progress.waitOptimizationExpertReview(skipTask.id, '自动修订两次失败', ['目标已不适用']);
  const skipped = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/optimize/apply-review`, headers,
    payload: { taskId: skipTask.id, action: 'skip', note: '确认不再处理。' },
  });
  assert.equal(skipped.statusCode, 200, skipped.body);
  assert.deepEqual(skipped.json(), { status: 'skipped', suggestionIds: [suggestions[1].id] });
  assert.equal(m4repo.getSuggestions([suggestions[1].id])[0].status, 'rejected');
});

test('图导入 API：支持取消后台任务并在删除项目时等待任务退出', async () => {
  graphImportTasks.length = 0;
  const token = await signToken();
  const authorization = `Bearer ${token}`;
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers: { authorization },
    payload: { name: '图导入 API 项目' },
  });
  const projectId = created.json().project.id;
  const { insertPackageVersion } = await import('../src/workflow/m2-repo.js');
  const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
  insertPackageVersion(projectId, '1.0.0', {
    ...graphPackageSpec,
    id: 'graph-api',
    objects: graphPackageSpec.objects.map((object) => object.key === 'Part'
      ? { ...object, relations: [{ relation: 'contains', target: 'BOMItem' }] }
      : object),
  }, { covered: [], uncovered: [] });
  saveProjectDataSource({
    projectId, mode: 'connection', provider: 'neo4j', workspaceId: 'shadow', ontologyId: 'ontology-001',
    config: {
      uri: 'bolt://neo4j:7687', username: 'neo4j', database: 'domain', has_password: true,
      workspace_property: 'd2o_workspace_id', workspace_value: 'workspace-001',
      ontology_property: 'd2o_ontology_id', ontology_value: 'ontology-001',
    },
  });
  const graphBuffer = workbookBuffer({
    Part: [['part_id', 'relation_code', 'name'], ['P-1', 'R-1', '零件']],
    BOMItem: [['item_id', 'parent_code', 'name'], ['I-1', 'R-1', 'BOM 行']],
  });
  const form = multipartExcel('graph.xlsx', graphBuffer);
  const uploaded = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-import/uploads`,
    headers: { authorization, ...form.headers },
    payload: form.payload,
  });
  assert.equal(uploaded.statusCode, 201, uploaded.body);
  const upload = uploaded.json().upload;
  assert.deepEqual(upload.sheets.map((sheet: any) => sheet.name), ['Part', 'BOMItem']);
  assert.equal(upload.schema.relations[0].relationName, 'contains');
  assert.equal('storedPath' in upload, false);
  const malformedMapping = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-import/uploads/${upload.id}/validate`,
    headers: { authorization },
    payload: { mapping: {} },
  });
  assert.equal(malformedMapping.statusCode, 400, malformedMapping.body);
  assert.match(malformedMapping.json().error, /mapping\.relations.*数组/);
  const entityOnlyMapping = { relations: [] };
  const validated = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-import/uploads/${upload.id}/validate`,
    headers: { authorization },
    payload: { mapping: entityOnlyMapping },
  });
  assert.equal(validated.statusCode, 200, validated.body);
  assert.deepEqual(validated.json().validation.counts, {
    nodeCount: 2,
    relationshipCount: 0,
    skippedCount: 0,
    skippedBlankCount: 0,
    skippedUnmatchedCount: 0,
  });

  const noPassword = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-import/uploads/${upload.id}/execute`,
    headers: { authorization },
    payload: { mapping: entityOnlyMapping, password: '' },
  });
  assert.equal(noPassword.statusCode, 400);
  assert.match(noPassword.json().error, /Password/);
  const started = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-import/uploads/${upload.id}/execute`,
    headers: { authorization },
    payload: { mapping: entityOnlyMapping, password: 'one-time-secret' },
  });
  assert.equal(started.statusCode, 202, started.body);
  assert.equal(started.json().run.status, 'running');
  assert.equal(graphImportTasks.length, 1);

  const runningHistory = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/graph-imports`, headers: { authorization },
  });
  assert.equal(runningHistory.json().imports[0].status, 'running');
  const cancelled = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-imports/${started.json().run.id}/cancel`,
    headers: { authorization },
  });
  assert.equal(cancelled.statusCode, 200, cancelled.body);
  assert.equal(cancelled.json().run.status, 'cancelled');
  await graphImportTasks.shift()!();
  const cancelledHistory = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/graph-imports`, headers: { authorization },
  });
  assert.equal(cancelledHistory.json().imports[0].status, 'cancelled');

  const restarted = await app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/graph-import/uploads/${upload.id}/execute`,
    headers: { authorization },
    payload: { mapping: entityOnlyMapping, password: 'one-time-secret' },
  });
  assert.equal(restarted.statusCode, 202, restarted.body);
  assert.equal(graphImportTasks.length, 1);

  const deleted = await app.inject({
    method: 'DELETE',
    url: `/api/projects/${projectId}`,
    headers: { authorization },
    payload: { confirmed: true },
  });
  assert.equal(deleted.statusCode, 200, deleted.body);
  assert.equal(deleted.json().deleted, projectId);
  await graphImportTasks.shift()!();
  const missing = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}`, headers: { authorization },
  });
  assert.equal(missing.statusCode, 404);
});

test('评测发现的知识包缺陷可以回流成建模修缮任务', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers, payload: { name: '评测缺陷回建模' },
  });
  const projectId = created.json().project.id;
  await app.inject({ method: 'GET', url: `/api/projects/${projectId}/session`, headers });

  const [m3repo, diagnosisRepo] = await Promise.all([
    import('../src/workflow/m3-repo.js'),
    import('../src/evaluation/failure-diagnosis-repo.js'),
  ]);
  const round = m3repo.createRound(projectId, '0.1.0', 2);
  const stored = diagnosisRepo.replaceFailureDiagnoses(projectId, round.id, '0.1.0', 'rev-0', [
    {
      category: 'knowledge_gap', title: '缺少分流适用条件', summary: '没有说明触发门槛',
      evidence: ['用例 3 回答与预期不符'], confidence: 'high', blocking: false,
      repairScope: 'package', packageOptimizable: true, resultIds: [],
      relatedQuestions: ['何时启用分流'], suggestedAction: '补一条 rule 明确门槛值',
    },
    {
      category: 'data_issue', title: '数据源缺列', summary: '评测库缺少必要字段',
      evidence: [], confidence: 'medium', blocking: true,
      repairScope: 'data', packageOptimizable: false, resultIds: [],
      relatedQuestions: [], suggestedAction: '补齐数据源字段',
    },
  ] as any);
  const packageDefect = stored.find((item) => item.repairScope === 'package')!;
  const dataDefect = stored.find((item) => item.repairScope === 'data')!;

  // 混入非知识包缺陷要被拒：改知识包解决不了运行环境和数据问题
  const rejected = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/optimize/diagnoses/plan-repair`, headers,
    payload: { roundId: round.id, diagnosisIds: [packageDefect.id, dataDefect.id] },
  });
  assert.equal(rejected.statusCode, 409, rejected.body);
  assert.equal(rejected.json().code, 'NOT_PACKAGE_DEFECT');

  const missingRound = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/optimize/diagnoses/plan-repair`, headers,
    payload: { roundId: 'round-does-not-exist', diagnosisIds: [packageDefect.id] },
  });
  assert.equal(missingRound.statusCode, 404, missingRound.body);

  const planned = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/optimize/diagnoses/plan-repair`, headers,
    payload: { roundId: round.id, diagnosisIds: [packageDefect.id] },
  });
  assert.equal(planned.statusCode, 200, planned.body);
  assert.equal(planned.json().canProposeOptimizations, true);
  const plannedDiagnosis = planned.json().diagnoses
    .find((item: any) => item.id === packageDefect.id);
  assert.equal(plannedDiagnosis.resolutionStatus, 'confirmed');
  assert.equal(plannedDiagnosis.resolutionAction, 'plan_repair');
  const repair = planned.json().aiConstruction.tasks
    .find((item: any) => item.kind === 'evaluation_repair');
  assert.ok(repair, '应规划出评测缺陷修缮任务');
  assert.equal(repair.status, 'waiting_user');

  // 缺陷清单和下一步要出现在建模助手的对话流里
  const session = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/session`, headers,
  });
  const defectMessage = session.json().aiConstructionMessages.find((message: any) => (
    message.blocks?.some((block: any) => block.type === 'status'
      && String(block.title).includes('评测发现'))
  ));
  assert.ok(defectMessage, '助手对话里应出现评测缺陷消息');
  assert.match(
    String(defectMessage.blocks.find((block: any) => block.type === 'text').text),
    /缺少分流适用条件/,
  );
  assert.deepEqual(
    defectMessage.blocks.find((block: any) => block.type === 'next_actions')
      .actions.map((action: any) => action.id),
    ['open_optimization', 'review_knowledge_files'],
  );
});
