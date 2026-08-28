import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request as httpRequest } from 'node:http';
import { once } from 'node:events';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-agent-proxy-'));

const [
  { registerAgentProxyRoutes },
  { createProject },
  outbox,
  { ensureAgentProjectEventKinds },
  { DocumentRevisionRepo },
  { MaterialPurposeReviewStore },
] = await Promise.all([
  import('../src/agent/proxy-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/agent/project-event-outbox.js'),
  import('../src/db/index.js'),
  import('../src/documents/repo.js'),
  import('../src/documents/material-purpose-review.js'),
]);

function appFor(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    request.user = { username, role: 'modeler' };
  });
  registerAgentProxyRoutes(app);
  t.after(() => app.close());
  return app;
}

test('Agent 代理校验项目归属并透传登录身份与输入', async (t) => {
  const project = createProject({ owner: 'alice', name: '代理边界' });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ mode: 'steering', runId: 'run-1' }), {
      status: 202,
      headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/steer`,
    headers: { authorization: 'Bearer user-token' },
    payload: { message: '继续读取资料' },
  });

  assert.equal(response.statusCode, 202, response.body);
  assert.match(calls[0].url, new RegExp(`/v1/projects/${project.id}/steer$`));
  assert.equal((calls[0].init?.headers as Record<string, string>).authorization, 'Bearer user-token');
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { message: '继续读取资料' });

  const deniedApp = appFor(t, 'bob');
  const denied = await deniedApp.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/steer`,
    payload: { message: '越权请求' },
  });
  assert.equal(denied.statusCode, 404);
  assert.equal(calls.length, 1);
});

test('只有后端确认全部资料用途后才允许续跑正常建模轮次', async (t) => {
  const project = createProject({ owner: 'alice', name: '用途确认边界' });
  const documents = new DocumentRevisionRepo();
  const reviews = new MaterialPurposeReviewStore();
  const firstDocument = documents.insert({
    projectId: project.id,
    filename: '业务说明一.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 业务说明一',
  }).document;
  const secondDocument = documents.insert({
    projectId: project.id,
    filename: '业务说明二.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 业务说明二',
  }).document;
  reviews.ensure(firstDocument);
  reviews.ensure(secondDocument);
  const firstSuggestion = reviews.suggest(firstDocument, {
    purpose: 'evidence',
    sheets: [],
    suggestedBy: 'agent',
  });
  const secondSuggestion = reviews.suggest(secondDocument, {
    purpose: 'evidence',
    sheets: [],
    suggestedBy: 'agent',
  });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ mode: 'new_run', run: { id: 'run-purpose' } }), {
      status: 202,
      headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);
  const request = {
    method: 'POST' as const,
    url: `/api/projects/${project.id}/agent/steer`,
    payload: { message: '全部用途已确认，请继续建模', purposeReviewCompleted: true },
  };

  reviews.confirm(firstDocument, {
    purpose: 'evidence',
    sheets: firstSuggestion.sheetSuggestions,
    confirmedBy: 'alice',
    expectedDirectiveRevision: firstDocument.directiveRevision ?? 0,
  });
  const blocked = await app.inject(request);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().purposeReviewProgress.pendingCount, 1);
  assert.equal(calls.length, 0);

  reviews.confirm(secondDocument, {
    purpose: 'evidence',
    sheets: secondSuggestion.sheetSuggestions,
    confirmedBy: 'alice',
    expectedDirectiveRevision: secondDocument.directiveRevision ?? 0,
  });
  const continued = await app.inject(request);

  assert.equal(continued.statusCode, 202, continued.body);
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), request.payload);
});

test('交互回答始终带项目作用域转发', async (t) => {
  const project = createProject({ owner: 'alice', name: '回答作用域' });
  let forwarded = '';
  t.mock.method(globalThis, 'fetch', async (url) => {
    forwarded = String(url);
    return new Response(JSON.stringify({ run: { id: 'run-2', status: 'queued' } }), {
      status: 202,
      headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/interactions/interaction%20one/answer`,
    payload: { answer: { value: 'warehouse' } },
  });

  assert.equal(response.statusCode, 202, response.body);
  assert.match(forwarded, new RegExp(`/v1/projects/${project.id}/interactions/interaction%20one/answer$`));
});

test('刷新 Agent 状态会重投持久化资料事件且成功后不重复', async (t) => {
  const project = createProject({ owner: 'alice', name: '资料事件恢复', runtime: 'agent' });
  outbox.enqueueMaterialParseProjectEvent(project.id, {
    eventId: 'material:doc-recover:terminal',
    status: 'parsed',
    documentId: 'doc-recover',
    sourceKey: 'source-recover',
    filename: '恢复资料.docx',
    revisionNo: 1,
    purpose: 'evidence',
    readingMode: 'on_demand',
    autoContinue: false,
    semanticChanged: true,
  });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).includes('/material-events')) {
      return new Response(JSON.stringify({ accepted: true }), {
        status: 202, headers: { 'content-type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ state: { phase: 'knowledge' }, lastSeq: 12 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);

  const first = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/state`,
    headers: { authorization: 'Bearer refresh-token' },
  });
  const second = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/state`,
    headers: { authorization: 'Bearer refresh-token' },
  });

  assert.equal(first.statusCode, 200, first.body);
  assert.equal(second.statusCode, 200, second.body);
  const eventCalls = calls.filter((call) => call.url.includes('/material-events'));
  assert.equal(eventCalls.length, 1);
  assert.equal((eventCalls[0].init?.headers as Record<string, string>).authorization, 'Bearer refresh-token');
  assert.equal(JSON.parse(String(eventCalls[0].init?.body)).documentId, 'doc-recover');
  assert.equal(JSON.parse(String(eventCalls[0].init?.body)).autoContinue, false);
  assert.equal(outbox.pendingAgentProjectEvents(project.id).length, 0);
});

test('刷新 Agent 状态会把 Studio 候选事件投递到专用入口且只投递一次', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Studio 事件恢复', runtime: 'agent' });
  outbox.enqueueStudioReviewProjectEvent(project.id, {
    eventId: `studio-review:${project.id}:review-recover`,
    clientMutationId: 'review-recover',
    requestHash: 'a'.repeat(64),
    workingRevision: 'sha256:working-studio-recover',
    changedPaths: ['objects/inventory.md', 'objects/index.md'],
    elementIds: ['object:inventory'],
    summary: '已形成 1 个编辑的待确认修改，确认后才会保存为新版本',
  });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).includes('/studio-events')) {
      return new Response(JSON.stringify({ accepted: true, continued: true }), {
        status: 202, headers: { 'content-type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ state: { phase: 'knowledge' }, lastSeq: 13 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await app.inject({
      method: 'GET',
      url: `/api/projects/${project.id}/agent/state`,
      headers: { authorization: 'Bearer refresh-token' },
    });
    assert.equal(response.statusCode, 200, response.body);
  }

  const eventCalls = calls.filter((call) => call.url.includes('/studio-events'));
  assert.equal(eventCalls.length, 1);
  const payload = JSON.parse(String(eventCalls[0].init?.body));
  assert.equal(payload.clientMutationId, 'review-recover');
  assert.deepEqual(payload.elementIds, ['object:inventory']);
  assert.equal(outbox.pendingAgentProjectEvents(project.id).length, 0);
});

test('刷新 Agent 状态会把融合候选事件投递到专用入口且只投递一次', async (t) => {
  const project = createProject({ owner: 'alice', name: '融合事件恢复', runtime: 'agent' });
  outbox.enqueueFusionReviewProjectEvent(project.id, {
    eventId: `fusion-review:${project.id}:plan-recover`,
    planId: 'plan-recover',
    candidateId: 'candidate-recover',
    outputName: '多来源融合知识包',
    sourceCount: 3,
    summary: '已生成三个来源知识包的整体融合候选',
  });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).includes('/fusion-events')) {
      return new Response(JSON.stringify({ accepted: true, continued: true }), {
        status: 202, headers: { 'content-type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ state: { phase: 'knowledge' }, lastSeq: 14 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await app.inject({
      method: 'GET',
      url: `/api/projects/${project.id}/agent/state`,
      headers: { authorization: 'Bearer refresh-token' },
    });
    assert.equal(response.statusCode, 200, response.body);
  }

  const eventCalls = calls.filter((call) => call.url.includes('/fusion-events'));
  assert.equal(eventCalls.length, 1);
  const payload = JSON.parse(String(eventCalls[0].init?.body));
  assert.equal(payload.planId, 'plan-recover');
  assert.equal(payload.candidateId, 'candidate-recover');
  assert.equal(payload.sourceCount, 3);
  assert.equal(outbox.pendingAgentProjectEvents(project.id).length, 0);
});

test('刷新 Agent 状态会先投递导入基线事件且只投递一次', async (t) => {
  const project = createProject({ owner: 'alice', name: '导入基线恢复', kind: 'package', runtime: 'agent' });
  outbox.enqueuePackageImportedProjectEvent(project.id, {
    eventId: `package-import:${project.id}:baseline-1`,
    baselineRevision: 'baseline-1',
    packageName: '现有知识包',
    packageVersion: '1.2.0',
    sourceFilename: 'existing-package.zip',
    fileCount: 42,
  });
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url: String(url), init });
    if (String(url).includes('/package-import-events')) {
      return new Response(JSON.stringify({ accepted: true, initialized: true }), {
        status: 202, headers: { 'content-type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ state: { phase: 'increment' }, lastSeq: 2 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  });
  const app = appFor(t);

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const response = await app.inject({
      method: 'GET',
      url: `/api/projects/${project.id}/agent/state`,
      headers: { authorization: 'Bearer refresh-token' },
    });
    assert.equal(response.statusCode, 200, response.body);
  }

  const eventCalls = calls.filter((call) => call.url.includes('/package-import-events'));
  assert.equal(eventCalls.length, 1);
  const payload = JSON.parse(String(eventCalls[0].init?.body));
  assert.equal(payload.baselineRevision, 'baseline-1');
  assert.equal(payload.fileCount, 42);
  assert.equal(outbox.pendingAgentProjectEvents(project.id).length, 0);
});

test('旧 Core outbox 迁移后保留资料事件并接受 Studio、融合与导入事件', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE projects(id TEXT PRIMARY KEY);
    INSERT INTO projects(id) VALUES ('project-migration');
    CREATE TABLE agent_project_event_outbox (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL CHECK (event_type IN ('material_parse')),
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      delivered_at TEXT
    );
    INSERT INTO agent_project_event_outbox VALUES (
      'material:old:terminal', 'project-migration', 'material_parse', '{}', '2026-08-23', NULL
    );`);

  ensureAgentProjectEventKinds(db);
  db.prepare(`INSERT INTO agent_project_event_outbox (
    id, project_id, event_type, payload_json, created_at, delivered_at
  ) VALUES (?,?,?,?,?,NULL)`).run(
    'studio-review:project-migration:review-1',
    'project-migration',
    'studio_review',
    '{}',
    '2026-08-23',
  );
  db.prepare(`INSERT INTO agent_project_event_outbox (
    id, project_id, event_type, payload_json, created_at, delivered_at
  ) VALUES (?,?,?,?,?,NULL)`).run(
    'fusion-review:project-migration:plan-1',
    'project-migration',
    'fusion_review',
    '{}',
    '2026-08-23',
  );
  db.prepare(`INSERT INTO agent_project_event_outbox (
    id, project_id, event_type, payload_json, created_at, delivered_at
  ) VALUES (?,?,?,?,?,NULL)`).run(
    'package-import:project-migration:baseline-1',
    'project-migration',
    'package_import',
    '{}',
    '2026-08-23',
  );
  const rows = db.prepare(`SELECT event_type FROM agent_project_event_outbox ORDER BY id`)
    .all() as Array<{ event_type: string }>;
  assert.deepEqual(rows.map((row) => row.event_type).sort(), [
    'fusion_review', 'material_parse', 'package_import', 'studio_review',
  ]);
  db.close();
});

test('浏览器断开 SSE 时代理收口上游 AbortError，不退出 core 进程', async (t) => {
  const project = createProject({ owner: 'alice', name: 'SSE 断开' });
  let cancelled = false;
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: progress\ndata: {"text":"处理中"}\n\n'));
    },
    cancel() { cancelled = true; },
  }), { status: 200, headers: { 'content-type': 'text/event-stream' } }));
  const app = appFor(t);
  const address = await app.listen({ host: '127.0.0.1', port: 0 });

  const response = await new Promise<import('node:http').IncomingMessage>((resolve, reject) => {
    const request = httpRequest(`${address}/api/projects/${project.id}/agent/stream`, resolve);
    request.on('error', reject);
    request.end();
  });
  response.once('data', () => response.destroy());
  await once(response, 'close');
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(cancelled, true);
  assert.equal(app.server.listening, true);
});

test('SSE 代理逐段转发 Agent 增量，不等待完整回答', async (t) => {
  const project = createProject({ owner: 'alice', name: 'SSE 增量转发' });
  t.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      controller.enqueue(encoder.encode('event: message_delta\ndata: {"messageId":"m1","delta":"第一段"}\n\n'));
      setTimeout(() => {
        controller.enqueue(encoder.encode('event: message_delta\ndata: {"messageId":"m1","delta":"第二段"}\n\n'));
        controller.enqueue(encoder.encode('event: message_done\ndata: {"messageId":"m1"}\n\n'));
        controller.close();
      }, 40);
    },
  }), { status: 200, headers: { 'content-type': 'text/event-stream' } }));
  const app = appFor(t);
  const address = await app.listen({ host: '127.0.0.1', port: 0 });

  const response = await new Promise<import('node:http').IncomingMessage>((resolve, reject) => {
    const request = httpRequest(`${address}/api/projects/${project.id}/agent/stream`, resolve);
    request.on('error', reject);
    request.end();
  });
  const chunks: string[] = [];
  response.setEncoding('utf8');
  response.on('data', (chunk) => chunks.push(String(chunk)));
  await once(response, 'end');

  assert.equal(response.headers['x-accel-buffering'], 'no');
  assert.ok(chunks.length >= 2, `增量被合并成 ${chunks.length} 段`);
  assert.match(chunks[0], /第一段/);
  assert.doesNotMatch(chunks[0], /第二段/);
  assert.match(chunks.join(''), /第二段/);
  assert.match(chunks.join(''), /message_done/);
});
