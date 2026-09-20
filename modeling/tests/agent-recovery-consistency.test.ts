import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-recovery-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';

const [
  { registerAgentCoreRoutes },
  { createProject },
  { getDb },
  { ChangeControlStore },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/agent/change-control.js'),
]);

function appFor(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    request.user = { username, role: 'modeler' };
  });
  registerAgentCoreRoutes(app);
  t.after(() => app.close());
  return app;
}

function tool(app: ReturnType<typeof Fastify>, projectId: string, name: string,
  args: Record<string, unknown> = {}, runId = 'run-agent-1') {
  return app.inject({
    method: 'POST',
    url: `/internal/agent/tools/${name}`,
    headers: {
      'x-modeling-project': projectId,
      'x-modeling-run': runId,
      authorization: 'Bearer agent-test-token',
    },
    payload: { arguments: args },
  });
}

function safeTerminate(app: ReturnType<typeof Fastify>, projectId: string, planId: string) {
  return app.inject({
    method: 'POST',
    url: `/api/projects/${projectId}/agent/change-plans/${planId}/safe-terminate`,
    payload: {},
  });
}

function planArgs(workingRevision: string) {
  return {
    goal: '为项目演示建模目标',
    mode: 'expert_edit',
    expected_revision: workingRevision,
    tasks: [
      {
        client_key: 'task-a',
        title: '确认库存口径',
        resource_kind: 'rule',
        target_key: 'StockRule',
        allowed_tools: ['patch_rule'],
        depends_on: [],
        acceptance: '库存口径确认完成',
        required: true,
      },
    ],
  };
}

async function currentRevision(app: ReturnType<typeof Fastify>, projectId: string, runId: string) {
  const listed = await tool(app, projectId, 'list_domain_resources', { kind: 'object' }, runId);
  assert.equal(listed.statusCode, 200, listed.body);
  return String(listed.json().working_revision ?? '');
}

test('工作区接续时把进行中的计划转移到新 run，旧 run 不再读到计划', async (t) => {
  const project = createProject({ owner: 'alice', name: '恢复一致性演示' });
  const app = appFor(t);
  const firstRunId = `run-${project.id}`;
  const revision = await currentRevision(app, project.id, firstRunId);
  const firstClaimed = await tool(app, project.id, 'claim_candidate_workspace', {}, firstRunId);
  assert.equal(firstClaimed.statusCode, 200, firstClaimed.body);
  const created = await tool(app, project.id, 'create_change_plan', planArgs(revision), firstRunId);
  assert.equal(created.statusCode, 200, created.body);
  const planId = created.json().plan.id;

  const nextRunId = `run-${project.id}-next`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, nextRunId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  assert.equal(claimed.json().resumed, true);

  const readable = await tool(app, project.id, 'get_change_plan', { plan_id: planId }, nextRunId);
  assert.equal(readable.statusCode, 200, readable.body);
  assert.equal(readable.json().plan.id, planId);

  const stale = await tool(app, project.id, 'get_change_plan', { plan_id: planId }, firstRunId);
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'CHANGE_PLAN_STALE_RUN');
});

test('计划挂在旧 run 时返回可恢复诊断而不是裸 404，安全终止后可重新规划', async (t) => {
  const project = createProject({ owner: 'alice', name: '幽灵计划诊断' });
  const app = appFor(t);
  const firstRunId = `run-${project.id}`;
  const revision = await currentRevision(app, project.id, firstRunId);
  const created = await tool(app, project.id, 'create_change_plan', planArgs(revision), firstRunId);
  assert.equal(created.statusCode, 200, created.body);
  const planId = created.json().plan.id;

  // 模拟半恢复：计划仍挂在旧 run，但 agent 已在新 run 上接续。
  const nextRunId = `run-${project.id}-next`;
  const staleRunId = `run-${project.id}-stale`;
  getDb().prepare(`UPDATE agent_change_plans SET run_id = ? WHERE id = ?`).run(staleRunId, planId);

  const diagnosed = await tool(app, project.id, 'get_change_plan', { plan_id: planId }, nextRunId);
  assert.equal(diagnosed.statusCode, 409, diagnosed.body);
  assert.equal(diagnosed.json().code, 'CHANGE_PLAN_STALE_RUN');
  assert.equal(diagnosed.json().current_run_id, nextRunId);
  assert.equal(diagnosed.json().expected_run_id, staleRunId);

  const terminated = await safeTerminate(app, project.id, planId);
  assert.equal(terminated.statusCode, 200, terminated.body);
  assert.equal(terminated.json().terminated, true);

  const closed = await tool(app, project.id, 'get_change_plan', { plan_id: planId }, staleRunId);
  assert.equal(closed.statusCode, 200, closed.body);
  assert.equal(closed.json().plan.status, 'cancelled');

  const replanned = await tool(app, project.id, 'create_change_plan', planArgs(revision), nextRunId);
  assert.equal(replanned.statusCode, 200, replanned.body);
  assert.notEqual(replanned.json().plan.id, planId);
});

test('Python stale-run 终止后 Node 活动计划一并取消，新 run 可重新规划', async (t) => {
  const project = createProject({ owner: 'alice', name: '幽灵计划双向清理' });
  const app = appFor(t);
  const firstRunId = `run-${project.id}`;
  const revision = await currentRevision(app, project.id, firstRunId);
  const created = await tool(app, project.id, 'create_change_plan', planArgs(revision), firstRunId);
  assert.equal(created.statusCode, 200, created.body);
  const planId = created.json().plan.id;

  // Python 侧 safe_terminate_stale 只清 Python 状态；Node 侧必须同步取消活动计划。
  const cancelled = new ChangeControlStore(getDb()).safeTerminateActivePlansForProject({
    projectId: project.id,
    actor: 'alice',
  });
  assert.equal(cancelled.terminated, 1);
  assert.deepEqual(cancelled.planIds, [planId]);

  const nextRunId = `run-${project.id}-next`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, nextRunId);
  assert.equal(claimed.statusCode, 200, claimed.body);

  const replanned = await tool(app, project.id, 'create_change_plan', planArgs(revision), nextRunId);
  assert.equal(replanned.statusCode, 200, replanned.body);
  assert.notEqual(replanned.json().plan.id, planId);
});

test('取消栅栏写入后 Core 拒绝同一 run 的新候选写入', async (t) => {
  const project = createProject({ owner: 'alice', name: '取消栅栏回归' });
  const app = appFor(t);
  const store = new ChangeControlStore(getDb());
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);

  const fence = store.requestRunCancellation(project.id, runId, 'synthetic cancellation');
  assert.equal(fence.runId, runId);
  assert.equal(store.isRunCancellationRequested(project.id, runId), true);

  const rejected = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(rejected.statusCode, 409, rejected.body);
  assert.equal(rejected.json().code, 'RUN_CANCELLING');
});

test('Core 停止结算暂停计划并保留任务，只有显式 planId 可以迁移到新 run', async (t) => {
  const project = createProject({ owner: 'alice', name: '取消接口回归' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const revision = await currentRevision(app, project.id, runId);
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const created = await tool(app, project.id, 'create_change_plan', planArgs(revision), runId);
  assert.equal(created.statusCode, 200, created.body);
  const planId = created.json().plan.id;
  getDb().prepare(`UPDATE agent_change_plan_tasks
    SET status = 'staged', change_set_id = 'synthetic-staged-result'
    WHERE plan_id = ?`).run(planId);
  getDb().prepare(`INSERT INTO agent_change_plan_tasks (
    id, plan_id, client_key, ordinal, title, resource_kind, target_key,
    allowed_tools_json, depends_on_json, acceptance, required, status, updated_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
    `${planId}_pending`, planId, 'task-pending', 2, '尚未提取的规则', 'rule',
    'PendingRule', JSON.stringify(['patch_rule']), JSON.stringify([]),
    '规则提取完成', 1, 'pending', new Date().toISOString(),
  );
  const cancel = await app.inject({
    method: 'POST',
    url: `/internal/agent/runs/${runId}/cancel`,
    headers: {
      'x-modeling-project': project.id,
      'x-modeling-run': runId,
      authorization: 'Bearer agent-test-token',
    },
    payload: {},
  });
  assert.equal(cancel.statusCode, 200, cancel.body);
  assert.equal(cancel.json().quiescent, true);
  assert.equal(cancel.json().requested.runId, runId);
  assert.equal(
    (getDb().prepare('SELECT status FROM agent_change_plans WHERE id = ?').get(planId) as any).status,
    'active',
  );

  const repeated = await app.inject({
    method: 'POST',
    url: `/internal/agent/runs/${runId}/cancel`,
    headers: {
      'x-modeling-project': project.id,
      'x-modeling-run': runId,
      authorization: 'Bearer agent-test-token',
    },
    payload: { drain: true },
  });
  assert.equal(repeated.statusCode, 200, repeated.body);
  assert.equal(repeated.json().quiescent, true);
  assert.deepEqual(repeated.json().pausedPlanIds, [planId]);
  assert.equal(
    (getDb().prepare('SELECT status FROM agent_change_plans WHERE id = ?').get(planId) as any).status,
    'paused',
  );
  const pausedTasks = getDb().prepare(`SELECT status FROM agent_change_plan_tasks
    WHERE plan_id = ? ORDER BY ordinal`).all(planId) as Array<{ status: string }>;
  assert.deepEqual(pausedTasks.map((item) => item.status), ['staged', 'pending']);

  const fencedRunResumed = await app.inject({
    method: 'POST',
    url: `/internal/agent/runs/${runId}/resume`,
    headers: {
      'x-modeling-project': project.id,
      'x-modeling-run': runId,
      authorization: 'Bearer agent-test-token',
    },
    payload: {},
  });
  assert.equal(fencedRunResumed.statusCode, 200, fencedRunResumed.body);
  assert.equal(new ChangeControlStore(getDb()).isRunCancellationRequested(project.id, runId), false);

  const nextRunId = `${runId}-next`;
  const ambiguous = await tool(app, project.id, 'claim_candidate_workspace', {}, nextRunId);
  assert.equal(ambiguous.statusCode, 409, ambiguous.body);
  assert.equal(ambiguous.json().code, 'PAUSED_CHANGE_PLAN_DECISION_REQUIRED');
  const ownership = getDb().prepare(`SELECT
      plan.run_id AS plan_run_id, workspace.run_id AS workspace_run_id
    FROM agent_change_plans plan
    JOIN agent_candidate_workspaces workspace ON workspace.project_id = plan.project_id
    WHERE plan.id = ?`).get(planId) as any;
  assert.equal(ownership.plan_run_id, runId);
  assert.equal(ownership.workspace_run_id, runId);

  const resumed = await tool(app, project.id, 'claim_candidate_workspace', {
    resume_plan_id: planId,
  }, nextRunId);
  assert.equal(resumed.statusCode, 200, resumed.body);
  assert.equal(resumed.json().resumedPlan.id, planId);
  assert.equal(resumed.json().resumedPlan.status, 'active');
  assert.deepEqual(
    resumed.json().resumedPlan.tasks.map((item: any) => item.status),
    ['staged', 'pending'],
  );
  assert.equal(
    (getDb().prepare('SELECT run_id FROM agent_change_plans WHERE id = ?').get(planId) as any).run_id,
    nextRunId,
  );
});

test('暂停计划的必选任务会阻断知识确认门禁', async (t) => {
  const project = createProject({ owner: 'alice', name: '暂停计划门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const revision = await currentRevision(app, project.id, runId);
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const store = new ChangeControlStore(getDb());
  const created = await tool(app, project.id, 'create_change_plan', planArgs(revision), runId);
  assert.equal(created.statusCode, 200, created.body);
  const plan = created.json().plan;
  store.requestRunCancellation(project.id, runId, 'synthetic cancellation');
  const settled = store.reconcileRunCancellation(project.id, runId, 'test', true);
  assert.deepEqual(settled.pausedPlanIds, [plan.id]);

  const progress = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/progress`,
  });
  assert.equal(progress.statusCode, 200, progress.body);
  const gate = progress.json().gates.find((item: any) => item.id === 'knowledge_confirmed');
  assert.equal(gate.pass, false);
  assert.equal(gate.blockers.some((item: string) => item.includes('暂停的建模计划')), true);
});
