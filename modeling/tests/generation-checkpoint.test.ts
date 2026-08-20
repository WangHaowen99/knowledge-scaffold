import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'generation-checkpoint-'));

const { createProject } = await import('../src/projects/repo.js');
const { getPiAuthoringAdapter } = await import('../src/authoring/pi-adapter.js');
const { GenerationCheckpointStore } = await import('../src/workflow/generation-checkpoint.js');

test('知识生成检查点保存批次产物并拒绝错误计划复用', () => {
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
  assert.throws(
    () => store.list(project.id, run.id, 'plan-b'),
    /知识生成输入或批次计划已变化/u,
  );
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
