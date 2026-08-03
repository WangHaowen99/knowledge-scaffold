import { test } from 'node:test';
import assert from 'node:assert/strict';

import { optimizationOperationProgress } from '../frontend/src/optimizationOperationProgress.js';

test('失败诊断期间展示独立证据分析进度并估计剩余时长', () => {
  const progress = optimizationOperationProgress({
    operation: 'diagnose',
    startedAt: '2026-07-22T12:00:00.000Z',
  }, Date.parse('2026-07-22T12:03:00.000Z'));

  assert.equal(progress?.currentStep, '读取失败结果、执行轨迹和独立证据');
  assert.equal(progress?.elapsedLabel, '3 分钟');
  assert.equal(progress?.estimatedSeconds, 420);
  assert.equal(progress?.estimatedLabel, '约 7 分钟');
  assert.deepEqual(progress?.nextSteps, ['区分数据、运行、用例与知识包问题', '保存失败诊断']);
});

test('生成优化建议期间展示已耗时和预计剩余时长', () => {
  const progress = optimizationOperationProgress({
    operation: 'propose',
    startedAt: '2026-07-22T12:00:00.000Z',
  }, Date.parse('2026-07-22T12:01:00.000Z'));

  assert.equal(progress?.currentStep, '分析诊断证据并生成优化建议');
  assert.equal(progress?.elapsedLabel, '1 分钟');
  assert.equal(progress?.estimatedSeconds, 120);
  assert.equal(progress?.estimatedLabel, '约 2 分钟');
  assert.equal(progress?.indeterminate, true);
  assert.deepEqual(progress?.nextSteps, ['保存并展示优化建议']);
});

test('优化请求超过经验时长后停止倒计时', () => {
  const progress = optimizationOperationProgress({
    operation: 'propose',
    startedAt: '2026-07-22T12:00:00.000Z',
  }, Date.parse('2026-07-22T12:03:01.000Z'));

  assert.equal(progress?.estimatedSeconds, 0);
  assert.equal(progress?.estimatedLabel, '模型响应较慢，暂无法准确估算');
});

test('应用优化建议时明确生成的是待确认的修改', () => {
  const progress = optimizationOperationProgress({
    operation: 'apply',
    startedAt: '2026-07-22T12:00:00.000Z',
  }, Date.parse('2026-07-22T12:00:10.000Z'));

  assert.equal(progress?.currentStep, '正在把所选优化建议整理成待确认的修改');
  assert.deepEqual(progress?.nextSteps, ['模型修改知识包', '校验修改结果', '保存待确认的修改']);
});
