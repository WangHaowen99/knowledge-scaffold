import { test } from 'node:test';
import assert from 'node:assert/strict';
import { optimizationProgress } from '../frontend/src/optimizationProgress.js';

test('优化应用进度展示当前步骤、后续步骤、已耗时和预计剩余时间', () => {
  const node = {
    id: 'nd-progress', status: 'running', createdAt: '2026-07-16T12:00:00.000Z',
    output: {
      taskType: 'optimization_apply', phase: 'revision', attempt: 1, maxAttempts: 3,
      selectedCount: 3, resourceCount: 30,
      startedAt: '2026-07-16T12:00:00.000Z', phaseStartedAt: '2026-07-16T12:00:00.000Z',
    },
  };

  assert.deepEqual(optimizationProgress(node, Date.parse('2026-07-16T12:01:00.000Z')), {
    status: 'running',
    completedSteps: 1,
    totalSteps: 4,
    percent: 25,
    currentStep: '模型正在修改知识包（第 1/3 次）',
    nextSteps: ['校验修改结果', '保存待确认候选修订'],
    elapsedSeconds: 60,
    elapsedLabel: '1 分钟',
    estimatedSeconds: 865,
    estimatedLabel: '约 15 分钟',
  });
});

test('模型响应超过预估后不展示虚假的短倒计时', () => {
  const node = {
    id: 'nd-overdue', status: 'running', createdAt: '2026-07-16T12:00:00.000Z',
    output: {
      taskType: 'optimization_apply', phase: 'revision', attempt: 1, maxAttempts: 3,
      selectedCount: 3, resourceCount: 30,
      startedAt: '2026-07-16T12:00:00.000Z', phaseStartedAt: '2026-07-16T12:00:00.000Z',
    },
  };

  const progress = optimizationProgress(node, Date.parse('2026-07-16T12:16:00.000Z'));
  assert.equal(progress?.estimatedSeconds, 0);
  assert.equal(progress?.estimatedLabel, '模型响应较慢，暂无法准确估算');
});

test('校验阶段展示可能发生的剩余自动修正次数', () => {
  const node = {
    id: 'nd-validation', status: 'running', createdAt: '2026-07-16T12:00:00.000Z',
    output: {
      taskType: 'optimization_apply', phase: 'validation', attempt: 2, maxAttempts: 3,
      selectedCount: 2, startedAt: '2026-07-16T12:00:00.000Z', phaseStartedAt: '2026-07-16T12:02:00.000Z',
    },
  };

  const progress = optimizationProgress(node, Date.parse('2026-07-16T12:02:05.000Z'));
  assert.deepEqual(progress?.nextSteps, ['校验未通过时自动修正（最多剩余 1 次）', '保存待确认候选修订']);
  assert.equal(progress?.currentStep, '正在校验修改结果（第 2/3 次）');
});
