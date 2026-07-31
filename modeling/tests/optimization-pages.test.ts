import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  FailureDiagnosisPanel,
  OptimizationPanel,
  optimizationApplyFailureMessage,
  optimizationApplyTaskForSuggestions,
} from '../frontend/src/M4Panel.jsx';

test('失败诊断和优化更新使用两个独立页面组件', () => {
  assert.equal(typeof FailureDiagnosisPanel, 'function');
  assert.equal(typeof OptimizationPanel, 'function');
  assert.notEqual(FailureDiagnosisPanel, OptimizationPanel);
});

test('优化候选生成失败时优先回显服务端失败原因', () => {
  assert.equal(optimizationApplyFailureMessage({
    node: {
      status: 'failed',
      reason: '修订与原规格完全相同（建议未被落实）',
      output: { error: 'fallback' },
    },
  }), '修订与原规格完全相同（建议未被落实）');
  assert.equal(optimizationApplyFailureMessage({ node: { status: 'done' } }), '');
});

test('旧轮次候选失败不得继续污染新一轮优化页面', () => {
  const current = [{ id: 'sg_new', status: 'pending' }];
  assert.equal(optimizationApplyTaskForSuggestions({
    node: { status: 'failed', output: { suggestionIds: ['sg_old'] } },
  }, current), null);
  assert.equal(optimizationApplyTaskForSuggestions({
    node: { status: 'failed', output: { suggestionIds: ['sg_new'] } },
  }, current)?.node.status, 'failed');
  assert.equal(optimizationApplyTaskForSuggestions({
    node: { status: 'failed', output: {} },
  }, current), null);
});
