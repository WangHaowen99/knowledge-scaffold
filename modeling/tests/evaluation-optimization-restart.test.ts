import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-optimization-restart-'));

const { createProject } = await import('../src/projects/repo.js');
const m3repo = await import('../src/workflow/m3-repo.js');
const optimizationProgress = await import('../src/evaluation/optimization-apply-progress.js');

test('服务重启会把运行中的评测轮次结束为可追溯失败', () => {
  const project = createProject({ owner: 'alice', name: '评测重启恢复' });
  const round = m3repo.createRound(project.id, '1.0.0', 2, 'rev-eval');

  assert.equal(m3repo.failInterruptedEvaluationRounds('服务重启，评测已中断'), 1);
  const recovered = m3repo.getRound(round.id)!;
  assert.equal(recovered.status, 'failed');
  assert.equal(recovered.phase, 'failed');
  assert.match(recovered.log, /服务重启，评测已中断/);
  assert.ok(recovered.finishedAt);
});

test('服务重启会把运行中的优化候选任务结束为失败', () => {
  const project = createProject({ owner: 'alice', name: '优化重启恢复' });
  const task = optimizationProgress.startOptimizationApplyTask(project.id, ['sg-1'], 3);

  assert.equal(optimizationProgress.failInterruptedOptimizationApplyTasks(), 1);
  const recovered = optimizationProgress.latestOptimizationApplyTask(project.id)!;
  assert.equal(recovered.node.id, task.id);
  assert.equal(recovered.node.status, 'failed');
  assert.equal((recovered.node.output as any).phase, 'failed');
  assert.match((recovered.node.output as any).error, /服务重启/);
});
