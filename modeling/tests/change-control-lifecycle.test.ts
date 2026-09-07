import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-change-control-lifecycle-'));

const [
  { ChangeControlError, ChangeControlStore },
  { getDb },
  { createProject },
] = await Promise.all([
  import('../src/agent/change-control.js'),
  import('../src/db/index.js'),
  import('../src/projects/repo.js'),
]);

test('可选任务必须执行或经用户批准后显式跳过才能进入校验', () => {
  const project = createProject({ owner: 'modeler_a', name: '合成计划状态测试', runtime: 'agent' });
  const store = new ChangeControlStore(getDb());
  const runId = `run-${project.id}`;
  const plan = store.createPlan({
    projectId: project.id,
    runId,
    goal: '验证计划任务不会静默丢失',
    mode: 'expert_edit',
    expectedRevision: 'revision-1',
    currentRevision: 'revision-1',
    tasks: [{
      client_key: 'required-task', title: '执行必选任务', resource_kind: 'binding',
      target_key: '__schema__', allowed_tools: ['sync_schema_objects'],
      acceptance: '形成候选', required: true,
    }, {
      client_key: 'optional-task', title: '处理可选任务', resource_kind: 'other',
      target_key: '__schema_semantics__', allowed_tools: ['confirm_schema_semantics'],
      acceptance: '完成或明确跳过', required: false,
    }],
  });
  store.completeDirectTask({
    projectId: project.id,
    runId,
    taskId: plan.tasks[0].id,
    tool: 'sync_schema_objects',
    receiptId: 'synthetic-receipt',
    resultingRevision: 'revision-2',
  });

  assert.throws(
    () => store.readyForValidation(project.id, runId, plan.id),
    (error: unknown) => error instanceof ChangeControlError
      && error.code === 'CHANGE_PLAN_INCOMPLETE'
      && (error.details.incomplete_tasks as Array<Record<string, unknown>>)[0].required === false,
  );
  const progress = store.snapshot(project.id, runId, plan.id);
  assert.equal(progress.executed_count, 1);
  assert.equal(progress.pending_execution_count, 1);
  assert.equal(progress.awaiting_settlement_count, 1);
  const revised = store.revisePlan({
    projectId: project.id, runId, planId: plan.id, expectedVersion: 2,
    reason: '修订计划后交由用户重新批准', addTasks: [], replaceTasks: [],
    skipTaskIds: [plan.tasks[1].id],
  });
  assert.equal(revised.tasks[1].status, 'skipped');
  assert.equal(revised.approval_status, 'pending');
  assert.throws(
    () => store.readyForValidation(project.id, runId, plan.id),
    (error: unknown) => error instanceof ChangeControlError
      && error.code === 'CHANGE_PLAN_APPROVAL_REQUIRED',
  );
  store.respondPlan({ projectId: project.id, planId: plan.id, username: 'modeler_a', approved: true });
  assert.doesNotThrow(() => store.readyForValidation(project.id, runId, plan.id));
});

test('错误的 pending 任务可原位替换并保留已执行任务和稳定任务 ID', () => {
  const project = createProject({ owner: 'modeler_b', name: '合成任务替换测试', runtime: 'agent' });
  const store = new ChangeControlStore(getDb());
  const runId = `run-${project.id}`;
  const plan = store.createPlan({
    projectId: project.id,
    runId,
    goal: '验证修正错误目标时不丢弃已有候选',
    mode: 'generation',
    expectedRevision: 'revision-1',
    currentRevision: 'revision-1',
    tasks: [{
      client_key: 'schema', title: '同步合成 Schema', resource_kind: 'binding',
      target_key: '__schema__', allowed_tools: ['sync_schema_objects'],
      acceptance: '形成物理对象', required: true,
    }, {
      client_key: 'relation', title: '补充泛化关系', resource_kind: 'object',
      target_key: 'all_objects', allowed_tools: ['patch_object'], depends_on: ['schema'],
      acceptance: '补充对象关系', required: true,
    }],
  });
  store.respondPlan({ projectId: project.id, planId: plan.id, username: 'modeler_b', approved: true });
  const approved = store.snapshot(project.id, runId, plan.id);
  store.completeDirectTask({
    projectId: project.id,
    runId,
    taskId: approved.tasks[0].id,
    tool: 'sync_schema_objects',
    receiptId: 'schema-result',
    resultingRevision: 'revision-2',
  });
  const before = store.snapshot(project.id, runId, plan.id);
  const revised = store.revisePlan({
    projectId: project.id,
    runId,
    planId: plan.id,
    expectedVersion: before.version,
    reason: '把泛化目标替换为具体合成对象',
    addTasks: [],
    skipTaskIds: [],
    replaceTasks: [{
      task_id: before.tasks[1].id,
      title: '补充合成客户关系',
      resource_kind: 'object',
      target_key: 'synthetic_customer',
      allowed_tools: ['patch_object'],
      depends_on_task_ids: [before.tasks[0].id],
      acceptance: '只修改合成客户对象关系',
    }],
  });

  assert.equal(revised.approval_status, 'pending');
  assert.equal(revised.tasks[0].status, 'staged');
  assert.equal(revised.tasks[0].change_set_id, 'direct:schema-result');
  assert.equal(revised.tasks[1].id, before.tasks[1].id);
  assert.equal(revised.tasks[1].target_key, 'synthetic_customer');
  assert.deepEqual(revised.tasks[1].depends_on, [before.tasks[0].id]);
});

test('无实际变化的计划可以完成且不需要伪造候选', () => {
  const project = createProject({ owner: 'modeler_a', name: '合成无变化计划', runtime: 'agent' });
  const store = new ChangeControlStore(getDb());
  const runId = `run-${project.id}`;
  const plan = store.createPlan({
    projectId: project.id,
    runId,
    goal: '验证无变化结果直接完成',
    mode: 'expert_edit',
    expectedRevision: 'revision-1',
    currentRevision: 'revision-1',
    tasks: [{
      client_key: 'check', title: '核对现有结果', resource_kind: 'binding',
      target_key: '__schema__', allowed_tools: ['sync_schema_objects'],
      acceptance: '结果与现有版本一致', required: true,
    }],
  });
  store.completeDirectTask({
    projectId: project.id, runId, taskId: plan.tasks[0].id,
    tool: 'sync_schema_objects', receiptId: 'no-change', resultingRevision: 'revision-1',
  });

  const completed = store.completeNoopPlan(project.id, runId, plan.id);
  assert.equal(completed.status, 'completed');
  assert.equal(completed.tasks[0].status, 'accepted');
  assert.equal(completed.completed_count, 1);
});
