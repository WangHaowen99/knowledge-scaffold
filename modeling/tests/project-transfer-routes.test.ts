import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import multipart from '@fastify/multipart';
import Fastify from 'fastify';

import { buildProjectTransferArchive } from '../src/projects/project-transfer.js';
import {
  HttpAgentProjectTransferClient,
  ProjectTransferJobManager,
} from '../src/projects/project-transfer-jobs.js';
import { registerProjectTransferRoutes } from '../src/projects/project-transfer-routes.js';

function createDatabase(path: string): DatabaseSync {
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA foreign_keys = ON;
    CREATE TABLE projects (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      kind TEXT NOT NULL,
      runtime TEXT NOT NULL,
      name TEXT NOT NULL,
      stage TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );`);
  return db;
}

function multipartArchive(archive: Uint8Array, projectName = ''): { body: Buffer; contentType: string } {
  const boundary = 'project-transfer-test-boundary';
  const parts: Buffer[] = [];
  if (projectName) {
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="project_name"\r\n\r\n${projectName}\r\n`,
    ));
  }
  parts.push(Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="project-transfer.zip"\r\n`
    + 'Content-Type: application/zip\r\n\r\n',
  ));
  parts.push(Buffer.from(archive));
  parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));
  return {
    body: Buffer.concat(parts),
    contentType: `multipart/form-data; boundary=${boundary}`,
  };
}

function ownedProjectNames(db: DatabaseSync): (owner: string) => string[] {
  return (owner) => (db.prepare('SELECT name FROM projects WHERE owner = ?').all(owner) as { name: string }[])
    .map((row) => row.name);
}

test('网页导入 API 创建后台任务并隔离任务账号', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'project-transfer-routes-'));
  const sourceDir = join(root, 'source');
  const targetDir = join(root, 'target');
  await mkdir(sourceDir, { recursive: true });
  await mkdir(targetDir, { recursive: true });
  const source = createDatabase(join(sourceDir, 'source.db'));
  const target = createDatabase(join(targetDir, 'target.db'));
  t.after(() => source.close());
  t.after(() => target.close());
  const createdAt = '2026-01-02T03:04:05.000Z';
  source.prepare(`INSERT INTO projects
    (id, owner, kind, runtime, name, stage, created_at, updated_at)
    VALUES ('pj_source_route', 'operator_a', 'modeling', 'agent', 'project_alpha', 'review', ?, ?)`)
    .run(createdAt, createdAt);
  const archive = await buildProjectTransferArchive({
    db: source,
    dataDir: sourceDir,
    projectId: 'pj_source_route',
    owner: 'operator_a',
  });

  const app = Fastify({ logger: false });
  await app.register(multipart);
  app.addHook('onRequest', (request, _reply, done) => {
    request.user = { username: String(request.headers['x-test-user'] || 'operator_b') } as any;
    done();
  });
  registerProjectTransferRoutes(
    app,
    new ProjectTransferJobManager({ db: target, dataDir: targetDir }),
    ownedProjectNames(target),
  );
  await app.ready();
  t.after(() => app.close());
  // 临时目录必须在数据库句柄关闭之后删除，否则 Windows 上 SQLite 文件仍被占用
  t.after(() => rm(root, { recursive: true, force: true }));

  const multipartBody = multipartArchive(archive);
  const started = await app.inject({
    method: 'POST',
    url: '/api/project-transfers/imports',
    headers: {
      'content-type': multipartBody.contentType,
      'x-test-user': 'operator_b',
      authorization: 'Bearer synthetic-test-token',
    },
    payload: multipartBody.body,
  });
  assert.equal(started.statusCode, 202);
  const jobId = started.json().job.id;

  const hidden = await app.inject({
    method: 'GET',
    url: `/api/project-transfers/jobs/${jobId}`,
    headers: { 'x-test-user': 'operator_c' },
  });
  assert.equal(hidden.statusCode, 404);

  let completed: ReturnType<typeof started.json> | undefined;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const status = await app.inject({
      method: 'GET',
      url: `/api/project-transfers/jobs/${jobId}`,
      headers: { 'x-test-user': 'operator_b' },
    });
    assert.equal(status.statusCode, 200);
    const payload = status.json();
    if (payload.job.status === 'completed' || payload.job.status === 'failed') {
      completed = payload;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(completed?.job.status, 'completed', completed?.job.error);
  assert.equal(completed?.job.project.name, 'project_alpha');
  assert.equal(
    target.prepare('SELECT owner FROM projects WHERE id = ?').get(completed?.job.project.projectId)!.owner,
    'operator_b',
  );
});

test('重名项目迁移包必须改名后才能导入', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'project-transfer-conflict-'));
  const sourceDir = join(root, 'source');
  const targetDir = join(root, 'target');
  await mkdir(sourceDir, { recursive: true });
  await mkdir(targetDir, { recursive: true });
  const source = createDatabase(join(sourceDir, 'source.db'));
  const target = createDatabase(join(targetDir, 'target.db'));
  t.after(() => source.close());
  t.after(() => target.close());
  const createdAt = '2026-01-02T03:04:05.000Z';
  source.prepare(`INSERT INTO projects
    (id, owner, kind, runtime, name, stage, created_at, updated_at)
    VALUES ('pj_source_conflict', 'operator_a', 'modeling', 'agent', 'project_alpha', 'review', ?, ?)`)
    .run(createdAt, createdAt);
  target.prepare(`INSERT INTO projects
    (id, owner, kind, runtime, name, stage, created_at, updated_at)
    VALUES ('pj_existing', 'operator_b', 'modeling', 'agent', 'project_alpha', 'review', ?, ?)`)
    .run(createdAt, createdAt);
  const archive = await buildProjectTransferArchive({
    db: source,
    dataDir: sourceDir,
    projectId: 'pj_source_conflict',
    owner: 'operator_a',
  });

  const app = Fastify({ logger: false });
  await app.register(multipart);
  app.addHook('onRequest', (request, _reply, done) => {
    request.user = { username: String(request.headers['x-test-user'] || 'operator_b') } as any;
    done();
  });
  registerProjectTransferRoutes(
    app,
    new ProjectTransferJobManager({ db: target, dataDir: targetDir }),
    ownedProjectNames(target),
  );
  await app.ready();
  t.after(() => app.close());
  // 临时目录必须在数据库句柄关闭之后删除，否则 Windows 上 SQLite 文件仍被占用
  t.after(() => rm(root, { recursive: true, force: true }));

  const importArchive = (projectName = '') => {
    const body = multipartArchive(archive, projectName);
    return app.inject({
      method: 'POST',
      url: '/api/project-transfers/imports',
      headers: {
        'content-type': body.contentType,
        'x-test-user': 'operator_b',
        authorization: 'Bearer synthetic-test-token',
      },
      payload: body.body,
    });
  };

  const conflicted = await importArchive();
  assert.equal(conflicted.statusCode, 409, conflicted.body);
  assert.deepEqual(conflicted.json(), {
    error: '项目名称「project_alpha」已存在，不可重复导入。请选择候选名称或输入新名称后继续导入。',
    code: 'PROJECT_NAME_CONFLICT',
    conflict: {
      originalName: 'project_alpha',
      attemptedName: 'project_alpha',
      candidates: ['project_alpha（2）', 'project_alpha（3）', 'project_alpha（4）'],
    },
  });
  assert.equal(
    (target.prepare('SELECT COUNT(*) AS count FROM projects').get() as { count: number }).count,
    1,
  );

  const renamed = await importArchive('project_alpha（2）');
  assert.equal(renamed.statusCode, 202, renamed.body);
  const jobId = renamed.json().job.id;
  let completed: any;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const status = await app.inject({
      method: 'GET',
      url: `/api/project-transfers/jobs/${jobId}`,
      headers: { 'x-test-user': 'operator_b' },
    });
    const payload = status.json();
    if (payload.job.status === 'completed' || payload.job.status === 'failed') {
      completed = payload;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(completed?.job.status, 'completed', completed?.job.error);
  assert.equal(completed?.job.project.name, 'project_alpha（2）');
  assert.equal(
    target.prepare('SELECT name FROM projects WHERE id = ?').get(completed?.job.project.projectId)!.name,
    'project_alpha（2）',
  );
});

test('主服务通过内部认证迁移 Agent 数据并接收共享 ID 映射', async (t) => {
  const requests: Array<{ method?: string; authorization?: string; serviceToken?: string; body: string }> = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    request.on('end', () => {
      requests.push({
        method: request.method,
        authorization: String(request.headers.authorization || ''),
        serviceToken: String(request.headers['x-modeling-service-token'] || ''),
        body: Buffer.concat(chunks).toString('utf8'),
      });
      response.setHeader('content-type', 'application/json');
      if (request.method === 'GET') {
        response.end(JSON.stringify({ format: 'modeling-agent-project-transfer', version: 1 }));
      } else if (request.method === 'POST') {
        response.statusCode = 201;
        response.end(JSON.stringify({ ok: true, idMap: { run_source: 'run_target' } }));
      } else {
        response.end(JSON.stringify({ ok: true }));
      }
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  const client = new HttpAgentProjectTransferClient(
    `http://127.0.0.1:${address.port}`,
    'synthetic-service-token-00000000',
  );

  const payload = await client.exportProject('pj_source', 'Bearer synthetic-user-token');
  assert.equal(payload.format, 'modeling-agent-project-transfer');
  const idMap = await client.importProject('pj_target', payload, 'Bearer synthetic-user-token');
  assert.deepEqual(idMap, { run_source: 'run_target' });
  await client.removeProject('pj_target', 'Bearer synthetic-user-token');

  assert.deepEqual(requests.map((request) => request.method), ['GET', 'POST', 'DELETE']);
  assert.ok(requests.every((request) => request.authorization === 'Bearer synthetic-user-token'));
  assert.ok(requests.every((request) => request.serviceToken === 'synthetic-service-token-00000000'));
  assert.deepEqual(JSON.parse(requests[1].body), { payload });
});
