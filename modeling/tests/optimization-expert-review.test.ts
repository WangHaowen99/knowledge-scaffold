import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'optimization-expert-review-'));

const { createProject } = await import('../src/projects/repo.js');
const {
  latestOptimizationApplyTask,
  recordOptimizationApplyProgress,
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

test('目标化优化任务持久化建议合并后的原子变更范围', () => {
  const project = createProject({ owner: 'targeted-progress-user', name: 'targeted-progress', domain: 'demo', background: '' });
  const task = startOptimizationApplyTask(project.id, ['sg-a', 'sg-b', 'sg-c'], 12);
  recordOptimizationApplyProgress(task.id, {
    phase: 'scope_prepared', attempt: 0, maxAttempts: 2,
    protocol: 'targeted_incremental', changeSetCount: 1,
    targetResourceKeys: ['script:calculate', 'operation:calculate'],
  });
  recordOptimizationApplyProgress(task.id, {
    phase: 'target_plan_ready', attempt: 1, maxAttempts: 2, shardCount: 3,
  });

  const stored = latestOptimizationApplyTask(project.id)!;
  assert.equal((stored.node.output as any).protocol, 'targeted_incremental');
  assert.equal((stored.node.output as any).changeSetCount, 1);
  assert.equal((stored.node.output as any).shardCount, 3);
  assert.deepEqual((stored.node.output as any).targetResourceKeys, [
    'script:calculate', 'operation:calculate',
  ]);
  assert.match(stored.events.at(-2)?.summary ?? '', /1 个原子变更组，涉及 2 个资源/u);
  assert.match(stored.events.at(-1)?.title ?? '', /并行生成 3 个目标符号补丁/u);
});

test('依赖敏感脚本的生成进度明确标记为按依赖顺序', () => {
  const project = createProject({ owner: 'dependency-progress-user', name: 'dependency-progress', domain: 'demo', background: '' });
  const task = startOptimizationApplyTask(project.id, ['sg-a', 'sg-b'], 4);
  recordOptimizationApplyProgress(task.id, {
    phase: 'target_plan_ready', attempt: 1, maxAttempts: 2, shardCount: 4,
    shardOrdering: 'dependency_serial',
  });

  const stored = latestOptimizationApplyTask(project.id)!;
  assert.equal((stored.node.output as any).shardOrdering, 'dependency_serial');
  assert.match(stored.events.at(-1)?.title ?? '', /按依赖顺序生成 4 个目标符号补丁/u);
});

test('专家处置只返回真实下一步提示，不宣称已修改工作流状态', () => {
  assert.deepEqual(diagnosisResolutionPlan('clarify_semantics'), {
    nextSteps: ['generate_optimization_suggestions', 'rerun_evaluation'],
  });
  assert.deepEqual(diagnosisResolutionPlan('data_updated'), {
    nextSteps: ['review_data_source', 'rerun_evaluation'],
  });
  assert.deepEqual(diagnosisResolutionPlan('defer'), { nextSteps: ['rerun_evaluation'] });
});
