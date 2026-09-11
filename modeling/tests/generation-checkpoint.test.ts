import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'generation-checkpoint-'));

const { createProject } = await import('../src/projects/repo.js');
const { getPiAuthoringAdapter } = await import('../src/authoring/pi-adapter.js');
const { GenerationCheckpointStore } = await import('../src/workflow/generation-checkpoint.js');

test('知识生成检查点保存批次产物并忽略不匹配的计划单元', () => {
  const project = createProject({
    owner: 'tester', name: '知识生成断点', domain: '银行营销', background: '产品推荐',
  });
  const run = getPiAuthoringAdapter().start(project.id, '生成知识包', 'base-1', {}).run;
  const store = new GenerationCheckpointStore();
  store.saveCompleted({
    projectId: project.id,
    runId: run.id,
    planHash: 'plan-a',
    phase: 'knowledge_batch_1',
    batchKind: 'knowledge',
    batchIndex: 1,
    output: {
      rules: [{ key: 'customer_active', title: '活跃客户', content: '状态为活跃' }],
      metrics: [],
    },
    attemptCount: 1,
  });

  const restored = store.list(project.id, run.id, 'plan-a');
  assert.equal(restored.length, 1);
  assert.equal(restored[0].status, 'completed');
  assert.deepEqual(restored[0].output, {
    rules: [{ key: 'customer_active', title: '活跃客户', content: '状态为活跃' }],
    metrics: [],
  });
  assert.deepEqual(store.list(project.id, run.id, 'plan-b'), []);
});

test('计划整体变化时只恢复依赖指纹未变化的单元', () => {
  const project = createProject({ owner: 'tester', name: '局部恢复', domain: '测试', background: '' });
  const adapter = getPiAuthoringAdapter();
  const first = adapter.start(project.id, '生成知识包', 'base-1', {}).run;
  const second = adapter.start(project.id, '生成知识包', 'base-1', {}).run;
  const store = new GenerationCheckpointStore();
  for (const [phase, dependencyHash] of [['knowledge_batch_1', 'same'], ['knowledge_batch_2', 'changed']] as const) {
    store.saveCompleted({
      projectId: project.id, runId: first.id, planHash: 'plan-old', phase,
      batchKind: 'knowledge', batchIndex: Number(phase.at(-1)), dependencyHash,
      output: { rules: [], metrics: [] }, attemptCount: 1,
    });
  }
  const expected = new Map([['knowledge_batch_1', 'same'], ['knowledge_batch_2', 'new']]);
  store.copyToRun({
    projectId: project.id, fromRunId: first.id, toRunId: second.id,
    planHash: 'plan-new', dependencyHashes: expected,
  });
  assert.deepEqual(
    store.list(project.id, second.id, 'plan-new', expected).map((item) => item.phase),
    ['knowledge_batch_1'],
  );
});

test('生成检查点保存稳定 unitId 和目标资源', () => {
  const project = createProject({ owner: 'tester', name: '生成单元标识' });
  const run = getPiAuthoringAdapter().start(project.id, '生成知识包', 'base-unit', {}).run;
  const store = new GenerationCheckpointStore();
  store.saveCompleted({
    projectId: project.id, runId: run.id, planHash: 'plan-unit', phase: 'object_batch_1',
    batchKind: 'object', batchIndex: 1, unitId: 'object-unit-a', targetIds: ['candidate-a'],
    output: { objects: [] }, attemptCount: 1,
  });
  const restored = store.list(project.id, run.id, 'plan-unit');
  assert.equal(restored[0].unitId, 'object-unit-a');
  assert.deepEqual(restored[0].targetIds, ['candidate-a']);
});

test('生成门禁恢复时可把已完成批次复制到新运行', () => {
  const project = createProject({
    owner: 'tester', name: '知识生成跨运行恢复', domain: '供应链', background: '库存分析',
  });
  const adapter = getPiAuthoringAdapter();
  const first = adapter.start(project.id, '生成知识包', 'base-1', {}).run;
  const second = adapter.start(project.id, '生成知识包', 'base-1', {}).run;
  const store = new GenerationCheckpointStore();
  store.saveCompleted({
    projectId: project.id,
    runId: first.id,
    planHash: 'plan-a',
    phase: 'object_batch_1',
    batchKind: 'object',
    batchIndex: 1,
    output: { objects: [] },
    attemptCount: 1,
  });

  store.copyToRun({
    projectId: project.id,
    fromRunId: first.id,
    toRunId: second.id,
    planHash: 'plan-a',
  });

  assert.deepEqual(
    store.list(project.id, second.id, 'plan-a').map((item) => ({
      phase: item.phase, status: item.status, output: item.output,
    })),
    [{ phase: 'object_batch_1', status: 'completed', output: { objects: [] } }],
  );
});

test('只恢复同一基线下因服务重启中断且已有完成批次的最近运行', () => {
  const project = createProject({ owner: 'tester', name: '重启断点恢复', domain: '测试', background: '' });
  const adapter = getPiAuthoringAdapter();
  const run = adapter.start(project.id, '生成知识包', 'base-1', {}).run;
  const store = new GenerationCheckpointStore();
  store.saveCompleted({
    projectId: project.id, runId: run.id, planHash: 'plan-1', phase: 'object_batch_1',
    batchKind: 'object', batchIndex: 1, output: { objects: [] }, attemptCount: 1,
  });
  adapter.fail(project.id, run.id, '服务重启，任务已中断，请重新执行当前步骤');
  assert.equal(store.latestInterruptedRun(project.id, 'base-1'), run.id);
  assert.equal(store.latestInterruptedRun(project.id, 'base-2'), undefined);

  const other = adapter.start(project.id, '生成知识包', 'base-1', {}).run;
  adapter.fail(project.id, other.id, '模型调用失败');
  assert.equal(store.latestInterruptedRun(project.id, 'base-1'), run.id);
});
