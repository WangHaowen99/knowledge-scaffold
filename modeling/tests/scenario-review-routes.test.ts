import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-scenario-review-routes-'));

const [
  { registerWorkflowExtraRoutes },
  { getAuthoringState },
  { WorkflowStepProgressRepo },
  { createProject },
  { getDb },
  { getPackageArtifactStore },
  { PackageArtifactStoreError },
] = await Promise.all([
  import('../src/authoring/workflow-extra-routes.js'),
  import('../src/authoring/state.js'),
  import('../src/authoring/workflow-progress.js'),
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/artifacts/package-store.js'),
]);

function routeApp(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (req) => {
    req.user = { username, role: 'modeler' };
  });
  registerWorkflowExtraRoutes(app);
  t.after(() => app.close());
  return app;
}

test('场景启动将保存忙碌映射为 409，未知 500 不回显后端详情', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '场景错误映射' });
  const artifacts = getPackageArtifactStore();
  const originalCurrent = artifacts.current;
  t.after(() => { artifacts.current = originalCurrent; });
  const app = routeApp(t);

  artifacts.current = async () => {
    throw new PackageArtifactStoreError(
      'commit_in_progress',
      'synthetic backend detail\nError: internal stack',
    );
  };
  const busy = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/workflow/scenario/start`,
  });
  assert.equal(busy.statusCode, 409, busy.body);
  assert.deepEqual(busy.json(), {
    error: '知识包正在保存，请稍后重试',
    code: 'commit_in_progress',
  });

  artifacts.current = async () => {
    throw new Error('synthetic backend detail\nError: internal stack');
  };
  const internal = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/workflow/scenario/start`,
  });
  assert.equal(internal.statusCode, 500, internal.body);
  assert.deepEqual(internal.json(), {
    error: '服务暂时不可用，请稍后重试',
    code: 'INTERNAL_ERROR',
  });
});

test('场景专用审核逐项收口、完成运行且重试不重复记录', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '场景审核闭环' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '场景推演', state: { stepId: 'scenario' },
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建场景推演运行');
  const first = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'scenario_review', payload: { title: '场景一' },
  });
  const second = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'scenario_review', payload: { title: '场景二' },
  });
  assert.equal(first.kind, 'interaction');
  assert.equal(second.kind, 'interaction');
  if (first.kind !== 'interaction' || second.kind !== 'interaction') {
    throw new Error('未创建场景审核待办');
  }
  new WorkflowStepProgressRepo().save({
    projectId: project.id,
    stepId: 'scenario',
    status: 'waiting_review',
    output: { runId: started.run.id, scenarios: [{ name: '场景一' }, { name: '场景二' }], decisions: [] },
    revision: 'revision-a',
  });

  const app = routeApp(t);
  const firstAnswer = {
    method: 'POST' as const,
    url: `/api/projects/${project.id}/workflow/scenario/reviews/${first.interaction.id}/respond`,
    payload: { requestId: 'scenario-answer-1', response: { action: 'accept' } },
  };
  const accepted = await app.inject(firstAnswer);
  assert.equal(accepted.statusCode, 200, accepted.body);
  assert.equal(accepted.json().replayed, false);
  assert.equal(accepted.json().remainingCount, 1);
  assert.equal(accepted.json().completed, false);
  assert.equal(accepted.json().progress.status, 'waiting_review');
  assert.deepEqual(accepted.json().progress.output.decisions, [{
    interactionId: first.interaction.id,
    action: 'accept',
  }]);

  const replayed = await app.inject(firstAnswer);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().replayed, true);
  assert.equal(replayed.json().progress.output.decisions.length, 1);
  assert.equal(state.replay(project.id).events.filter((event) => (
    event.type === 'workflow.step.updated'
  )).length, 1);

  const completed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/workflow/scenario/reviews/${second.interaction.id}/respond`,
    payload: { requestId: 'scenario-answer-2', response: { action: 'accept', note: '结果符合预期' } },
  });
  assert.equal(completed.statusCode, 200, completed.body);
  assert.equal(completed.json().remainingCount, 0);
  assert.equal(completed.json().completed, true);
  assert.equal(completed.json().failed, false);
  assert.equal(completed.json().progress.status, 'completed');
  assert.equal(completed.json().progress.output.decisions.length, 2);

  const snapshot = state.snapshot(project.id);
  assert.equal(snapshot.pendingInteractions.length, 0);
  assert.equal(snapshot.activeRuns.some((run) => run.id === started.run.id), false);
  const run = getDb().prepare('SELECT status, result_json FROM authoring_runs WHERE id = ?')
    .get(started.run.id) as { status: string; result_json: string };
  assert.equal(run.status, 'completed');
  assert.deepEqual(JSON.parse(run.result_json), { accepted: true });
  assert.deepEqual(state.replay(project.id).events.filter((event) => (
    event.type === 'workflow.step.updated'
  )).map((event) => event.payload.status), ['waiting_review', 'completed']);
});

test('场景专用审核拒绝其他待办类型且不提前关闭待办', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '场景审核类型保护' });
  const state = getAuthoringState();
  const started = state.apply({ kind: 'start_run', projectId: project.id, intent: '普通确认' });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建运行');
  const decision = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'decision', payload: { title: '普通待办' },
  });
  assert.equal(decision.kind, 'interaction');
  if (decision.kind !== 'interaction') throw new Error('未创建普通待办');

  const app = routeApp(t);
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/workflow/scenario/reviews/${decision.interaction.id}/respond`,
    payload: { requestId: 'wrong-kind', response: { action: 'accept' } },
  });
  assert.equal(response.statusCode, 400, response.body);
  assert.equal(response.json().code, 'INVALID_INPUT');
  assert.equal(state.interaction(project.id, decision.interaction.id)?.status, 'pending');
});

test('场景未通过时结束本轮运行并保留失败决定', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '场景审核未通过' });
  const state = getAuthoringState();
  const started = state.apply({ kind: 'start_run', projectId: project.id, intent: '场景推演' });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建运行');
  const review = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'scenario_review', payload: { title: '异常场景' },
  });
  assert.equal(review.kind, 'interaction');
  if (review.kind !== 'interaction') throw new Error('未创建场景待办');
  new WorkflowStepProgressRepo().save({
    projectId: project.id, stepId: 'scenario', status: 'waiting_review',
    output: { runId: started.run.id, scenarios: [{ name: '异常场景' }], decisions: [] },
  });

  const app = routeApp(t);
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/workflow/scenario/reviews/${review.interaction.id}/respond`,
    payload: {
      requestId: 'scenario-rejected',
      response: { action: 'revise', note: '缺少异常分支说明' },
    },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().completed, false);
  assert.equal(response.json().failed, true);
  assert.equal(response.json().remainingCount, 0);
  assert.equal(response.json().progress.status, 'failed');
  assert.deepEqual(response.json().progress.output.decisions, [{
    interactionId: review.interaction.id,
    action: 'revise',
    note: '缺少异常分支说明',
  }]);
  const run = getDb().prepare('SELECT status, result_json FROM authoring_runs WHERE id = ?')
    .get(started.run.id) as { status: string; result_json: string };
  assert.equal(run.status, 'completed');
  assert.deepEqual(JSON.parse(run.result_json), { accepted: false });
});
