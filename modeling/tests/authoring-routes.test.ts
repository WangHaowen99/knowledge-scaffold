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

function routeApp(t: test.TestContext, username = 'alice', options: Record<string, unknown> = {}) {
  const app = Fastify();
  app.addHook('preHandler', async (req) => {
    req.user = { username, role: 'modeler' };
  });
  registerAuthoringRoutes(app, options as any);
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

test('完成建模确认时固化验证候选并让任务图引用候选修订', async (t) => {
  const project = createProject({ owner: 'alice', name: '验证候选生命周期' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [], 'pkg-draft');

  let savedInput: any;
  let appliedCommand: any;
  const packages = {
    async current() {
      return {
        revision: { oid: 'pkg-draft' },
        packageVersion: '0.1.0',
        spec: { status: 'draft', objects: [], operations: [] },
      };
    },
    async saveCurrentAsPackageVersion(_projectId: string, input: any) {
      savedInput = input;
      return {
        revision: { oid: 'pkg-rc-1' },
        packageVersion: input.version,
        spec: { status: input.status, objects: [], operations: [] },
      };
    },
  };
  const graph = {
    snapshot: () => ({ revision: 6 }),
    apply: (_projectId: string, command: any) => {
      appliedCommand = command;
      return { revision: 7, status: 'completed' };
    },
  };
  const orchestrator = {
    latestMaterialAnalysis: () => null,
    latestConfirmedModelingBrief: () => null,
    completeProcessReview: () => { throw new Error('测试不应进入流程确认'); },
  };
  const app = routeApp(t, 'alice', { packages, graph, orchestrator });

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/commands`,
    payload: {
      command: {
        type: 'confirm_milestone',
        expectedRevision: 6,
        milestoneId: 'construction_completed',
        packageRevisionOid: 'pkg-draft',
        actor: { username: 'ignored', role: 'validator' },
      },
    },
  });

  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(response.json().package, {
    version: '1.0.0-rc.1', revision: 'pkg-rc-1', status: 'review',
  });
  assert.equal(savedInput.expectedRevision, 'pkg-draft');
  assert.equal(savedInput.version, '1.0.0-rc.1');
  assert.equal(savedInput.status, 'review');
  assert.equal(appliedCommand.packageRevisionOid, 'pkg-rc-1');
  assert.equal(appliedCommand.actor.username, 'alice');
  assert.equal(response.json().cursor, 2);
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

test('可编辑流程图从草稿保存到原子提交形成单一事实源', async (t) => {
  const project = createProject({ owner: 'alice', name: '流程图确认' });
  const state = getAuthoringState();
  const run = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认业务流程', baseRevision: 'pkg-base',
  });
  assert.equal(run.kind, 'run');
  if (run.kind !== 'run') throw new Error('未创建流程图任务');
  const pending = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: run.run.id,
    type: 'flowchart_review',
    baseRevision: 'pkg-base',
    payload: { purpose: 'process_review', taskId: 'task-process-review', diagrams: [] },
  });
  assert.equal(pending.kind, 'interaction');
  if (pending.kind !== 'interaction') throw new Error('未创建流程图待办');

  let saveInput: any;
  const packages = {
    async current() {
      return {
        revision: { oid: 'pkg-base' }, packageVersion: '0.1.0', spec: {}, coverage: {}, files: [],
      };
    },
    async readPackageFiles() {
      return {
        revision: { oid: 'pkg-base' }, spec: {}, coverage: {},
        files: { 'diagrams/process_9.mmd': 'stale' },
      };
    },
    async saveFiles(_projectId: string, input: any) {
      saveInput = input;
      return {
        revision: { oid: 'pkg-flowchart' }, packageVersion: '0.1.0', spec: {}, coverage: {},
        files: Object.keys(input.files),
      };
    },
  };
  const orchestrator = {
    latestMaterialAnalysis: () => null,
    latestConfirmedModelingBrief: () => null,
    completeProcessReview: () => ({
      aiConstruction: { revision: 8 },
      message: { id: 'message-flowchart' },
    }),
  };
  const app = routeApp(t, 'alice', { packages, orchestrator });
  const base = `/api/projects/${project.id}/interactions/${pending.interaction.id}`;

  const created = await app.inject({ method: 'GET', url: `${base}/flowchart-draft` });
  assert.equal(created.statusCode, 200, created.body);
  assert.equal(created.json().draftRevision, 1);
  assert.deepEqual(created.json().document, { diagrams: [] });

  const emptySubmit = await app.inject({
    method: 'POST', url: `${base}/respond`,
    payload: { requestId: 'flowchart-empty', response: { action: 'accept', draftRevision: 1 } },
  });
  assert.equal(emptySubmit.statusCode, 409, emptySubmit.body);
  assert.equal(emptySubmit.json().code, 'FLOWCHART_EMPTY_DOCUMENT');

  const document = {
    diagrams: [{
      id: 'approval', title: '授信审批',
      processMeta: { actors: ['客户经理'], inputs: ['申请材料'], exceptions: [], handoffs: [] },
      nodes: [
        { id: 'start', label: '收到申请', kind: 'start' },
        { id: 'review', label: '审核材料', kind: 'action' },
        { id: 'end', label: '形成结论', kind: 'end' },
      ],
      edges: [
        { id: 'start-review', from: 'start', to: 'review', kind: 'sequence' },
        { id: 'review-end', from: 'review', to: 'end', kind: 'sequence' },
      ],
    }],
  };
  const saved = await app.inject({
    method: 'PUT', url: `${base}/flowchart-draft`,
    payload: { expectedRevision: 1, document },
  });
  assert.equal(saved.statusCode, 200, saved.body);
  assert.equal(saved.json().draftRevision, 2);
  assert.equal(saved.json().warnings.length, 0);

  const answer = {
    method: 'POST' as const,
    url: `${base}/respond`,
    payload: { requestId: 'flowchart-accept', response: { action: 'accept', draftRevision: 2 } },
  };
  const submitted = await app.inject(answer);
  assert.equal(submitted.statusCode, 200, submitted.body);
  assert.equal(submitted.json().packageRevision, 'pkg-flowchart');
  assert.equal(saveInput.expectedRevision, 'pkg-base');
  assert.deepEqual(saveInput.deletePaths, ['package/diagrams/process_9.mmd']);
  assert.deepEqual(JSON.parse(saveInput.files['package/diagrams/processes.json']).diagrams[0].id, 'approval');
  assert.match(saveInput.files['package/diagrams/process_1.mmd'], /^flowchart TD/m);

  const retry = await app.inject(answer);
  assert.equal(retry.statusCode, 200, retry.body);
  assert.equal(retry.json().replayed, true);
  assert.equal(getDb().prepare(`SELECT status FROM flowchart_review_finalizations
    WHERE interaction_id = ?`).get(pending.interaction.id)?.status, 'completed');
});
