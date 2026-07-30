import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'optimization-expert-review-'));

const { createProject } = await import('../src/projects/repo.js');
const {
  latestOptimizationApplyTask,
  resolveOptimizationExpertReview,
  startOptimizationApplyTask,
  waitOptimizationExpertReview,
} = await import('../src/evaluation/optimization-apply-progress.js');
const { diagnosisResolutionPlan } = await import('../src/evaluation/diagnosis-resolution.js');

test('优化修订两次失败后持久化为等待专家决策', () => {
  const project = createProject({ owner: 'expert-review-user', name: 'expert-review', domain: 'demo', background: '' });
  const task = startOptimizationApplyTask(project.id, ['sg-a', 'sg-b'], 5);

  waitOptimizationExpertReview(task.id, '修订未落实', [
    '第 1 次：target 无法定位',
    '第 2 次：修订与原规格完全相同',
  ]);

  let stored = latestOptimizationApplyTask(project.id)!;
  assert.equal(stored.node.status, 'waiting_user');
  assert.equal((stored.node.output as any).phase, 'expert_review');
  assert.deepEqual((stored.node.output as any).suggestionIds, ['sg-a', 'sg-b']);
  assert.equal((stored.node.output as any).maxAttempts, 2);

  resolveOptimizationExpertReview(task.id, 'retry', '修改 operation 路由，不要新增脚本');
  stored = latestOptimizationApplyTask(project.id)!;
  assert.equal(stored.node.status, 'done');
  assert.equal((stored.node.output as any).expertDecision, 'retry');
  assert.match((stored.node.output as any).expertNote, /operation/);
});

test('专家处置只将受影响的下游步骤标记为需重做', () => {
  assert.deepEqual(diagnosisResolutionPlan('clarify_semantics'), {
    nextSteps: ['optimize', 'evaluate'],
    staleSteps: ['evaluate', 'optimize'],
  });
  assert.deepEqual(diagnosisResolutionPlan('data_updated'), {
    nextSteps: ['data_source', 'evaluate'],
    staleSteps: ['data_source', 'evaluate', 'optimize'],
  });
  assert.deepEqual(diagnosisResolutionPlan('defer'), { nextSteps: ['evaluate'], staleSteps: [] });
});
