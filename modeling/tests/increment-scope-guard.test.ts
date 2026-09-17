import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-increment-scope-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';

const [
  { registerAgentCoreRoutes },
  { incrementPlanScopeViolations },
  { createProject },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/agent/core-service.js'),
  import('../src/projects/repo.js'),
]);

// ---------- 纯函数：违例计算 ----------
// 「只动受影响部分」过去只写在提示词里——增量隔离是偏好不是不变量。
// 这里守住的规则：修改既有资源必须落在已界定影响闭包内；新建放行。

const workingSpec = () => ({
  objects: [{ key: 'baseline-covered' }, { key: 'baseline-outside' }],
  operations: [],
  rules: [{ key: 'baseline-rule' }],
  metrics: [],
  scripts: [],
}) as any;

const resolvedImpact = (affected: Array<{ kind: string; key: string }>) => ({
  semanticChanged: true,
  scopeResolved: true,
  affectedResources: affected,
}) as any;

const task = (kind: string, key: string) => ({
  client_key: `t-${key}`, title: `处理 ${key}`, resource_kind: kind, target_key: key,
  allowed_tools: ['patch_object'], depends_on: [], acceptance: '一致', required: true,
});

test('修改不在影响闭包内的既有资源被判违例', () => {
  const violations = incrementPlanScopeViolations(
    [task('object', 'baseline-outside')],
    workingSpec(),
    [resolvedImpact([{ kind: 'object', key: 'baseline-covered' }])],
  );
  assert.deepEqual(violations, ['object:baseline-outside']);
});

test('影响闭包内的修改与全新资源都放行', () => {
  const violations = incrementPlanScopeViolations(
    [task('object', 'baseline-covered'), task('object', 'brand-new-object')],
    workingSpec(),
    [resolvedImpact([{ kind: 'object', key: 'baseline-covered' }])],
  );
  assert.deepEqual(violations, []);
});

test('没有任何已界定影响时，修改任何既有资源都违例', () => {
  const violations = incrementPlanScopeViolations(
    [task('rule', 'baseline-rule')],
    workingSpec(),
    [],
  );
  assert.deepEqual(violations, ['rule:baseline-rule']);
});

test('未界定（scopeResolved=false）的影响不构成闭包，不能据此放行', () => {
  const violations = incrementPlanScopeViolations(
    [task('object', 'baseline-outside')],
    workingSpec(),
    [{ semanticChanged: true, scopeResolved: false, affectedResources: [] } as any],
  );
  assert.deepEqual(violations, ['object:baseline-outside']);
});

test('binding/other 类任务与平台注入类不在守卫范围', () => {
  const violations = incrementPlanScopeViolations(
    [
      { ...task('binding', 'baseline-covered'), allowed_tools: ['patch_object_source_binding'] },
      { ...task('other', 'baseline-outside') },
    ],
    workingSpec(),
    [],
  );
  assert.deepEqual(violations, []);
});

test('target_key 带 kind 前缀时按归一化后的键判断', () => {
  const violations = incrementPlanScopeViolations(
    [task('rule', 'rule:baseline-rule')],
    workingSpec(),
    [],
  );
  assert.deepEqual(violations, ['rule:baseline-rule']);
});

// ---------- HTTP：触发条件（发布事实）防误伤 ----------
// 守卫只在项目正式发布过之后启用；全量建模项目继续完善既有资源不受限。
// 已发布路径的拦截行为由上面的纯函数用例覆盖：在测试里伪造一条发布修订
// 会破坏工件存储的 git↔SQLite 一致性，不值得为此绕过一致性守卫。

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
  args: Record<string, unknown> = {}, runId = `run-isg-${projectId}`) {
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

function confirmationReceipt(preflight: { json(): any }) {
  const payload = preflight.json();
  return {
    expected_candidate_revision: payload.candidate_revision,
    expected_candidate_hash: payload.validation_receipt.candidateHash,
    expected_validator_version: payload.validation_receipt.validatorVersion,
  };
}

async function confirmPrerequisites(app: ReturnType<typeof Fastify>, projectId: string) {
  for (const [group, path, content] of [
    [{ id: 'seed-goal', tier: 'caliber', title: '确认建模目标' },
      '.modeling/goal.md', '# 建模目标\n\n确认当前业务范围。\n'],
    [{ id: 'seed-process', tier: 'caliber', title: '确认业务流程' },
      'diagrams/seed.mmd', 'flowchart TD\n  A[开始] --> B[结束]\n'],
  ] as const) {
    const written = await tool(app, projectId, 'write_file', {
      path, content, change_group: group.id, evidence: ['user-decision:test-seed'],
    });
    assert.equal(written.statusCode, 200, written.body);
    const preflight = await tool(app, projectId, 'request_confirmation_preflight', {
      summary: group.title, change_groups: [group],
    });
    assert.equal(preflight.statusCode, 200, preflight.body);
    const confirmed = await tool(app, projectId, 'request_confirmation', {
      summary: group.title, change_groups: [group],
      ...confirmationReceipt(preflight),
      review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
    });
    assert.equal(confirmed.statusCode, 200, confirmed.body);
  }
}

test('没有发布过的全量项目不受增量范围守卫影响', async (t) => {
  const project = createProject({ owner: 'alice', name: '全量项目不受限', runtime: 'agent' });
  const app = appFor(t);
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {});
  assert.equal(claimed.statusCode, 200, claimed.body);
  await confirmPrerequisites(app, project.id);
  const written = await tool(app, project.id, 'upsert_object', {
    key: 'fullbuild-object',
    title: '合成对象',
    description: '全量建模期的既有资源。',
    query_boundary: '只使用用户明确输入的数值。',
    fields: [{ key: 'value', cn_name: '数值', type: 'number', required: true }],
    relations: [],
    change_group: 'seed-fullbuild',
    evidence: ['user-decision:test-baseline'],
  });
  assert.equal(written.statusCode, 200, written.body);
  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' });
  assert.equal(listed.statusCode, 200, listed.body);

  const allowed = await tool(app, project.id, 'create_change_plan', {
    goal: '全量建模继续完善既有对象', mode: 'expert_edit',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'task-full', title: '完善既有对象', resource_kind: 'object',
      target_key: 'fullbuild-object', allowed_tools: ['patch_object'],
      depends_on: [], acceptance: '对象口径与资料一致', required: true,
    }],
  });
  assert.equal(allowed.statusCode, 200, allowed.body);
});
