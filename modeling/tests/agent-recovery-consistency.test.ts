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
        allowed_tools: ['create_rule'],
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
