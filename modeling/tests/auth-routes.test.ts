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
