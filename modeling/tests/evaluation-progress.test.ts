import { test } from 'node:test';
import assert from 'node:assert/strict';

import { evaluationProgress } from '../frontend/src/evaluationProgress.js';

const runningRound = {
  id: 'round-current',
  status: 'running',
  packageVersion: '1.2.0',
  progressDone: 0,
  progressTotal: 4,
  passCount: 0,
  createdAt: '2026-07-22T12:00:00.000Z',
  phase: 'workspace',
  phaseStartedAt: '2026-07-22T12:00:00.000Z',
};

test('评测准备阶段展示真实步骤、后续步骤和剩余时长', () => {
  const progress = evaluationProgress(
    runningRound,
    [],
    Date.parse('2026-07-22T12:00:05.000Z'),
  );

  assert.equal(progress?.currentStep, '准备评测工作空间');
  assert.equal(progress?.completedUnits, 0);
  assert.equal(progress?.totalUnits, 8);
  assert.equal(progress?.elapsedLabel, '5 秒');
  assert.equal(progress?.estimatedSeconds, 365);
  assert.equal(progress?.estimatedLabel, '约 7 分钟');
  assert.deepEqual(progress?.nextSteps, [
    '准备评测数据环境',
    '导入并激活知识包',
    '绑定评测数据源',
    '运行 4 条评测用例',
  ]);
});

test('评测用例阶段结合本轮速度与历史轮次估计 ETA', () => {
  const round = {
    ...runningRound,
    phase: 'cases',
    phaseStartedAt: '2026-07-22T12:01:00.000Z',
    currentItemStartedAt: '2026-07-22T12:02:30.000Z',
    progressDone: 2,
    progressTotal: 5,
  };
  const history = [{
    ...runningRound,
    id: 'round-history',
    status: 'done',
    progressDone: 4,
    progressTotal: 4,
    createdAt: '2026-07-22T11:00:00.000Z',
    finishedAt: '2026-07-22T11:04:00.000Z',
    phase: 'done',
  }];

  const progress = evaluationProgress(round, history, Date.parse('2026-07-22T12:03:00.000Z'));

  assert.equal(progress?.currentStep, '运行第 3/5 条评测用例');
  assert.equal(progress?.completedUnits, 6);
  assert.equal(progress?.totalUnits, 9);
  assert.equal(progress?.percent, 67);
  assert.equal(progress?.elapsedLabel, '3 分钟');
  assert.equal(progress?.estimatedSeconds, 120);
  assert.equal(progress?.estimatedLabel, '约 2 分钟');
  assert.deepEqual(progress?.nextSteps, ['完成当前用例', '运行剩余 2 条用例', '汇总评测结果']);
});

test('单条评测明显超出估算后不显示虚假的短倒计时', () => {
  const round = {
    ...runningRound,
    phase: 'cases',
    phaseStartedAt: '2026-07-22T12:00:00.000Z',
    currentItemStartedAt: '2026-07-22T12:00:00.000Z',
    progressTotal: 3,
  };
  const history = [{
    ...runningRound,
    id: 'round-history',
    status: 'done',
    progressDone: 4,
    progressTotal: 4,
    createdAt: '2026-07-22T11:00:00.000Z',
    finishedAt: '2026-07-22T11:04:00.000Z',
    phase: 'done',
  }];

  const progress = evaluationProgress(round, history, Date.parse('2026-07-22T12:03:00.000Z'));
  assert.equal(progress?.estimatedSeconds, 0);
  assert.equal(progress?.estimatedLabel, '当前用例响应较慢，暂无法准确估算');
});
