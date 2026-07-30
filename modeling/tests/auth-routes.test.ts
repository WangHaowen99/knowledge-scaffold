import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import multipart from '@fastify/multipart';
import { generateKeyPair, exportJWK, SignJWT, type CryptoKey } from 'jose';
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

// status: -1 表示直接掐断连接（模拟网络错）
const routes = new Map<string, { status: number; body?: string; contentType?: string }>();
const consumerRequests: string[] = [];
const testedDataSources: unknown[] = [];
const syncedDataSources: any[] = [];
const graphImportTasks: Array<() => Promise<void>> = [];
const evaluationDatabaseImportTasks: Array<() => Promise<void>> = [];
const evaluationDatabaseConnections: any[] = [];
const evaluationDatabaseQueries: string[] = [];
const projectDatabaseAdminConnections: any[] = [];
const projectDatabaseAdminQueries: string[] = [];
let projectDatabaseExists = false;
let packageChatPlanTask: (() => Promise<any>) | null = null;
let packageChatApplyTask: (() => Promise<any>) | null = null;

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
  process.env.CONSUMER_API_URL = `http://127.0.0.1:${addr.port}`;
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
    packageChatPlan: async () => {
      if (!packageChatPlanTask) throw new Error('测试未配置包修缮计划任务');
      return packageChatPlanTask();
    },
    packageChatApply: async () => {
      if (!packageChatApplyTask) throw new Error('测试未配置包修缮应用任务');
      return packageChatApplyTask();
    },
  });
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
  assert.equal(response.json().published.version, '1.1.0');
  assert.equal(response.json().published.draft.status, 'published');
  assert.deepEqual(response.json().published.changedFiles, ['operations/query_order.md']);

  response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/draft`, headers,
  });
  assert.equal(response.json().draft, null);
  response = await app.inject({
    method: 'GET',
    url: `/api/projects/${projectId}/package/file?version=1.1.0&path=${encodeURIComponent('operations/query_order.md')}`,
    headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.match(response.json().content, /根据订单编号查询最新订单详情/);
  response = await app.inject({
    method: 'GET', url: `/api/projects/${projectId}/package/export?version=1.1.0`, headers,
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.headers['content-type'], 'application/zip');
});

test('活动人工草稿统一门禁：修缮、重新生成和知识消费入口均要求先发布或放弃', async () => {
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

  const gatedRequests: Array<{ method: any; url: string; payload?: unknown; operation: string }> = [
    { method: 'POST', url: `/api/projects/${projectId}/generate`, payload: {}, operation: 'generate' },
    { method: 'POST', url: `/api/projects/${projectId}/package/chat`, payload: {}, operation: 'package-chat' },
    { method: 'POST', url: `/api/projects/${projectId}/package/chat/apply`, payload: {}, operation: 'package-chat-apply' },
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
  const generateResponse = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/generate`, headers, payload: {},
  });
  assert.equal(generateResponse.statusCode, 400, generateResponse.body);
  assert.match(generateResponse.json().error, /数据源/);
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
    assert.equal(response.statusCode, 200, response.body);
  }

  let startApply!: () => void;
  let releaseApply!: () => void;
  const applyStarted = new Promise<void>((resolve) => { startApply = resolve; });
  const applyBlocked = new Promise<void>((resolve) => { releaseApply = resolve; });
  packageChatApplyTask = async () => {
    startApply();
    await applyBlocked;
    return { reply: '修缮计划应用完成', version: '1.1.0' };
  };
  const applyRequest = app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/chat/apply`, headers,
    payload: {
      message: '应用修缮计划', history: [],
      plan: {
        summary: '测试修缮计划',
        changes: [{ targetKind: 'operation', targetKey: 'query_order', action: 'modify', description: '测试' }],
      },
    },
  });
  await applyStarted;
  try {
    const response = await app.inject({
      method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
      payload: {},
    });
    assert.equal(response.statusCode, 409, response.body);
    assert.equal(response.json().code, 'PACKAGE_REPAIR_IN_PROGRESS');
    assert.equal(response.json().operation, 'package-chat-apply');
  } finally {
    releaseApply();
    const response = await applyRequest;
    packageChatApplyTask = null;
    assert.equal(response.statusCode, 200, response.body);
  }

  const draftAfterRepair = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/package/draft`, headers,
    payload: {},
  });
  assert.equal(draftAfterRepair.statusCode, 201, draftAfterRepair.body);
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

test('领域问题 API：用户可新增并选中自定义问题，生成前必须选择数据源', async () => {
  const token = await signToken();
  const headers = { authorization: `Bearer ${token}` };
  const created = await app.inject({
    method: 'POST', url: '/api/projects', headers,
    payload: { name: '自定义领域问题项目' },
  });
  const projectId = created.json().project.id;

  let response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/competency`, headers,
    payload: { question: '  哪些订单存在交付风险？  ' },
  });
  assert.equal(response.statusCode, 201, response.body);
  assert.equal(response.json().question.question, '哪些订单存在交付风险？');
  assert.equal(response.json().question.selected, true);
  assert.equal(response.json().question.source, 'custom');

  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/competency`, headers,
    payload: { question: '   ' },
  });
  assert.equal(response.statusCode, 400);

  response = await app.inject({
    method: 'POST', url: `/api/projects/${projectId}/generate`, headers,
    payload: {},
  });
  assert.equal(response.statusCode, 400);
  assert.match(response.json().error, /数据源/);
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
