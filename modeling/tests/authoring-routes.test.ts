import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-authoring-routes-'));

const [{ registerAuthoringRoutes }, { getAuthoringState }, { createProject }, { getDb }] = await Promise.all([
  import('../src/authoring/routes.js'),
  import('../src/authoring/state.js'),
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
]);

function routeApp(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (req) => {
    req.user = { username, role: 'modeler' };
  });
  registerAuthoringRoutes(app);
  t.after(() => app.close());
  return app;
}

test('会话快照、任务图命令和事件回放保持同一游标', async (t) => {
  const project = createProject({ owner: 'alice', name: '事件工作区' });
  const app = routeApp(t);

  const first = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  assert.equal(first.statusCode, 200);
  const firstBody = first.json();
  assert.equal(firstBody.aiConstruction.revision, 1);
  assert.equal(firstBody.cursor, 0);

  const taskId = firstBody.aiConstruction.tasks[0].id;
  const updated = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/commands`,
    payload: { command: { type: 'start_task', expectedRevision: 1, taskId } },
  });
  assert.equal(updated.statusCode, 200, updated.body);
  assert.equal(updated.json().aiConstruction.revision, 2);
  assert.equal(updated.json().cursor, 1);

  const replay = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/events?after=0`,
  });
  assert.equal(replay.statusCode, 200);
  assert.deepEqual(replay.json().events.map((event: any) => [event.seq, event.type]), [
    [1, 'ai_construction.graph.updated'],
  ]);
  assert.equal(replay.json().events[0].payload.command, 'start_task');
});

test('待办回答支持请求号幂等并在会话中恢复运行态', async (t) => {
  const project = createProject({ owner: 'alice', name: '待办恢复' });
  const state = getAuthoringState();
  const run = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认知识对象', state: { step: 1 },
  });
  assert.equal(run.kind, 'run');
  if (run.kind !== 'run') throw new Error('未创建运行');
  const pending = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: run.run.id,
    type: 'decision',
    payload: { title: '确认对象边界' },
  });
  assert.equal(pending.kind, 'interaction');
  if (pending.kind !== 'interaction') throw new Error('未创建待办');

  const app = routeApp(t);
  const answer = {
    method: 'POST' as const,
    url: `/api/projects/${project.id}/interactions/${pending.interaction.id}/respond`,
    payload: { requestId: 'answer-1', response: { accepted: true } },
  };
  const first = await app.inject(answer);
  assert.equal(first.statusCode, 200, first.body);
  assert.equal(first.json().replayed, false);
  assert.equal(first.json().run.status, 'running');

  const retry = await app.inject(answer);
  assert.equal(retry.statusCode, 200, retry.body);
  assert.equal(retry.json().replayed, true);

  const session = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  assert.equal(session.json().authoring.pendingInteractions.length, 0);
  assert.equal(session.json().authoring.activeRuns[0].status, 'running');
});

test('存量项目读取会话时自动投影到唯一任务图', async (t) => {
  const now = new Date().toISOString();
  getDb().prepare(`INSERT INTO projects
    (id, owner, kind, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?)`)
    .run('pj_legacy_route', 'alice', 'modeling', '存量项目', '', '', 'project_setup', now, now);
  const app = routeApp(t);

  const session = await app.inject({ method: 'GET', url: '/api/projects/pj_legacy_route/session' });
  assert.equal(session.statusCode, 200, session.body);
  assert.equal(session.json().aiConstruction.source, 'legacy_projection');
  assert.equal(session.json().aiConstruction.revision, 1);
});

test('项目会话和任务图接口隔离非项目所有者', async (t) => {
  const project = createProject({ owner: 'alice', name: '权限隔离' });
  const app = routeApp(t, 'bob');

  const session = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  const command = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/commands`,
    payload: { command: { type: 'start_task', expectedRevision: 1, taskId: 'hidden' } },
  });
  assert.equal(session.statusCode, 404);
  assert.equal(command.statusCode, 404);
});
