import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test, { after, mock } from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-project-goal-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';

const [
  { registerRoutes },
  { registerAgentCoreRoutes },
  { confirmProjectGoal, createProject, getProject, ProjectGoalNotPendingError },
  { getAuthClient },
  { config },
] = await Promise.all([
  import('../src/routes.js'),
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/auth/client.js'),
  import('../src/config.js'),
]);

// 合成目标，长度超过 200 字；两条流程分别在开头和结尾，用来确认原文没有被截短
const LONG_GOAL = '整理合成仓储场景的库存知识包。流程甲：入库登记与质检。'
  + '补充说明：只覆盖示例仓库的标准作业。'.repeat(12)
  + '流程乙：出库复核与盘点。';

// 全量路由 + 合成 JWT：只替换验签，鉴权链路其余部分照常执行
const app = Fastify();
await registerRoutes(app);
after(() => app.close());
mock.method(getAuthClient(), 'verify', async (token: string) => {
  const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString());
  return { username: claims.sub, role: 'modeler' };
});

function headersFor(username: string) {
  const claims = Buffer.from(JSON.stringify({
    sub: username, exp: Math.floor(Date.now() / 1000) + 600,
  })).toString('base64url');
  return { authorization: `Bearer eyJhbGciOiJIUzI1NiJ9.${claims}.synthetic-goal-signature` };
}

const alice = headersFor('alice');

function pendingProject(name: string, background = LONG_GOAL) {
  return createProject({ owner: 'alice', name, runtime: 'agent', background, requireGoalConfirmation: true });
}

/** 记录发往建模助手的请求；确认前任何一条都不应该出现。 */
function recordAgentRequests(t: test.TestContext) {
  const calls: string[] = [];
  t.mock.method(globalThis, 'fetch', async (input: unknown) => {
    const url = String(input);
    if (url.startsWith(config.agentBaseUrl)) calls.push(url);
    return new Response(JSON.stringify({ run: { id: 'run-goal-confirmed', status: 'running' } }), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  });
  return calls;
}

function coreAppFor(t: test.TestContext) {
  const core = Fastify();
  core.addHook('preHandler', async (request) => {
    request.user = { username: 'alice', role: 'modeler' };
  });
  registerAgentCoreRoutes(core);
  t.after(() => core.close());
  return core;
}

function claim(core: ReturnType<typeof Fastify>, projectId: string) {
  return core.inject({
    method: 'POST',
    url: '/internal/agent/tools/claim_candidate_workspace',
    headers: {
      'x-modeling-project': projectId,
      'x-modeling-run': `run-goal-${projectId}`,
      authorization: 'Bearer agent-test-token',
    },
    payload: { arguments: {} },
  });
}

test('创建页填了目标的项目先待确认，并完整保存超过 200 字的目标', () => {
  assert.ok(LONG_GOAL.length > 200);
  const project = pendingProject('目标待确认');

  assert.equal(project.goalStatus, 'pending');
  assert.equal(getProject(project.id)?.background, LONG_GOAL);
});

test('没有目标或调用方未要求确认时不进入待确认', () => {
  assert.equal(pendingProject('空目标', '  \n ').goalStatus, '');
  assert.equal(createProject({ owner: 'alice', name: '其他入口', background: '其他入口的目标' }).goalStatus, '');
});

test('确认原目标或改写后的目标，空目标被拒绝且仍待确认', () => {
  const original = pendingProject('确认原目标', `  ${LONG_GOAL}\n`);
  const confirmed = confirmProjectGoal(original.id);
  assert.equal(confirmed?.goalStatus, 'confirmed');
  assert.equal(confirmed?.background, LONG_GOAL);

  const rewritten = pendingProject('改写目标');
  assert.throws(() => confirmProjectGoal(rewritten.id, '   '), /请填写建模目标/u);
  assert.equal(getProject(rewritten.id)?.goalStatus, 'pending');
  const updated = confirmProjectGoal(rewritten.id, '  新目标：只覆盖流程乙。\n');
  assert.equal(updated?.goalStatus, 'confirmed');
  assert.equal(updated?.background, '新目标：只覆盖流程乙。');
});

test('只有待确认的目标可以确认', () => {
  const project = pendingProject('重复确认');
  confirmProjectGoal(project.id);
  assert.throws(() => confirmProjectGoal(project.id, '再改一次'), ProjectGoalNotPendingError);
  assert.equal(getProject(project.id)?.background, LONG_GOAL);

  const legacy = createProject({ owner: 'alice', name: '存量项目', background: '存量项目目标' });
  assert.throws(() => confirmProjectGoal(legacy.id), ProjectGoalNotPendingError);
  assert.equal(confirmProjectGoal('pj_missing_goal'), null);
});

test('创建接口只在显式要求时让目标进入待确认', async () => {
  const pending = await app.inject({
    method: 'POST', url: '/api/projects', headers: alice,
    payload: { name: '接口待确认', runtime: 'agent', background: LONG_GOAL, requireGoalConfirmation: true },
  });
  assert.equal(pending.statusCode, 201, pending.body);
  assert.equal(pending.json().project.goalStatus, 'pending');
  assert.equal(pending.json().project.background, LONG_GOAL);

  const plain = await app.inject({
    method: 'POST', url: '/api/projects', headers: alice,
    payload: { name: '接口无需确认', runtime: 'agent', background: LONG_GOAL },
  });
  assert.equal(plain.statusCode, 201, plain.body);
  assert.equal(plain.json().project.goalStatus, '');
});

test('目标确认前不上传资料、不启动或续跑建模助手', async (t) => {
  const project = pendingProject('确认前拦截');
  const agentRequests = recordAgentRequests(t);
  const blocked = [
    { method: 'POST' as const, url: `/api/projects/${project.id}/documents` },
    { method: 'POST' as const, url: `/api/projects/${project.id}/agent/runs`, payload: { message: '开始建模' } },
    { method: 'POST' as const, url: `/api/projects/${project.id}/agent/steer`, payload: { message: '开始建模' } },
  ];

  for (const request of blocked) {
    const response = await app.inject({ ...request, headers: alice });
    assert.equal(response.statusCode, 409, `${request.url} ${response.body}`);
    assert.deepEqual(response.json(), {
      code: 'PROJECT_GOAL_CONFIRMATION_REQUIRED',
      error: '请先确认建模目标',
    });
  }
  assert.deepEqual(agentRequests, []);
  assert.equal(getProject(project.id)?.goalStatus, 'pending');
});

test('确认接口校验输入与权限，确认后对话照常转发给建模助手', async (t) => {
  const project = pendingProject('接口确认');
  const url = `/api/projects/${project.id}/goal`;

  const notText = await app.inject({ method: 'PUT', url, headers: alice, payload: { background: 123 } });
  assert.equal(notText.statusCode, 400, notText.body);
  assert.equal(notText.json().code, 'INVALID_INPUT');
  const blank = await app.inject({ method: 'PUT', url, headers: alice, payload: { background: '  ' } });
  assert.equal(blank.statusCode, 400, blank.body);
  assert.equal(blank.json().error, '请填写建模目标');
  const stranger = await app.inject({ method: 'PUT', url, headers: headersFor('bob'), payload: {} });
  assert.equal(stranger.statusCode, 404, stranger.body);
  assert.equal(getProject(project.id)?.goalStatus, 'pending');

  const confirmed = await app.inject({ method: 'PUT', url, headers: alice, payload: {} });
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  assert.equal(confirmed.json().project.goalStatus, 'confirmed');
  assert.equal(confirmed.json().project.background, LONG_GOAL);
  const again = await app.inject({ method: 'PUT', url, headers: alice, payload: { background: '再改一次' } });
  assert.equal(again.statusCode, 409, again.body);
  assert.equal(again.json().code, 'PROJECT_GOAL_NOT_PENDING');

  const agentRequests = recordAgentRequests(t);
  const steer = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/agent/steer`, headers: alice,
    payload: { message: '请开始建模' },
  });
  assert.equal(steer.statusCode, 200, steer.body);
  assert.deepEqual(agentRequests, [`${config.agentBaseUrl}/v1/projects/${project.id}/steer`]);
});

test('助手接管工作区：确认前拒绝，确认后带上确认过的目标', async (t) => {
  const core = coreAppFor(t);
  const project = pendingProject('接管带目标');

  const blocked = await claim(core, project.id);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'PROJECT_GOAL_CONFIRMATION_REQUIRED');
  assert.equal(blocked.json().requires_user_decision, true);

  confirmProjectGoal(project.id);
  const claimed = await claim(core, project.id);
  assert.equal(claimed.statusCode, 200, claimed.body);
  assert.equal(claimed.json().projectGoal, LONG_GOAL);
});

test('未经确认卡的项目接管时不带创建目标', async (t) => {
  const core = coreAppFor(t);
  const legacy = createProject({ owner: 'alice', name: '存量接管', runtime: 'agent', background: '存量项目目标' });

  const claimed = await claim(core, legacy.id);

  assert.equal(claimed.statusCode, 200, claimed.body);
  assert.equal(claimed.json().projectGoal, '');
});
