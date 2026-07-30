import assert from 'node:assert/strict';
import test from 'node:test';

import {
  linkedOptimizationCandidates,
  optimizationCandidateNotice,
  pendingLinkedOptimizationCandidates,
} from '../frontend/src/optimization-candidates.js';
import {
  canProposeFromDiagnoses,
  diagnoseAndRefresh,
  diagnosesForRound,
  optimizationExpertReviewForRound,
  optimizationExpertReviewState,
  pendingSuggestionIdsForRound,
  requireOverrideReason,
  suggestionsForRound,
} from '../frontend/src/optimization-flow.js';
import * as api from '../frontend/src/api.js';

test('刷新后由建议关联恢复优化候选，且只接受单一候选响应契约', () => {
  const suggestions = [{ id: 'sg-1', candidateRevisionId: 'cr-1', status: 'candidate' }];
  const candidates = [
    { id: 'cr-1', status: 'pending' },
    { id: 'cr-other', status: 'pending' },
  ];
  assert.deepEqual(linkedOptimizationCandidates(suggestions, candidates), [candidates[0]]);
  assert.deepEqual(pendingLinkedOptimizationCandidates(suggestions, candidates), [candidates[0]]);
  assert.deepEqual(pendingLinkedOptimizationCandidates([
    { ...suggestions[0], status: 'rejected' },
  ], candidates), []);
  assert.deepEqual(optimizationCandidateNotice({
    candidateId: 'cr-1', status: 'pending', selected: 1, basedRevisionOid: 'rev-1',
  }), {
    candidateId: 'cr-1', status: 'pending', selected: 1, basedRevisionOid: 'rev-1',
  });
  assert.throws(() => optimizationCandidateNotice({ version: '1.1.0', applied: 1 }), /响应不完整/);
});

test('重诊断完成后立即刷新建议和候选状态', async () => {
  const calls = [];
  await diagnoseAndRefresh(
    async () => { calls.push('diagnose'); },
    async () => { calls.push('refresh'); },
  );
  assert.deepEqual(calls, ['diagnose', 'refresh']);
});

test('建议展示和应用只使用目标轮次，且专家复核可在刷新后恢复', () => {
  const suggestions = [
    { id: 'sg-old', roundId: 'round-old', status: 'pending' },
    { id: 'sg-current', roundId: 'round-1', status: 'pending' },
    { id: 'sg-settled', roundId: 'round-1', status: 'applied' },
  ];
  assert.deepEqual(suggestionsForRound(suggestions, 'round-1').map((item) => item.id), [
    'sg-current', 'sg-settled',
  ]);
  assert.deepEqual(pendingSuggestionIdsForRound(suggestions, 'round-1'), ['sg-current']);

  const immediate = {
    status: 'waiting_expert', taskId: 'task-current', suggestionIds: ['sg-current'],
    validationErrors: ['修订未覆盖目标'], error: '自动修订两次失败',
  };
  assert.deepEqual(optimizationExpertReviewState(immediate), {
    taskId: 'task-current', suggestionIds: ['sg-current'],
    validationErrors: ['修订未覆盖目标'], error: '自动修订两次失败',
  });
  const persisted = {
    task: { node: { id: 'task-current', status: 'waiting_user', output: {
      phase: 'expert_review', suggestionIds: ['sg-current'],
      validationErrors: ['修订未覆盖目标'], error: '自动修订两次失败',
    } } },
  };
  assert.deepEqual(
    optimizationExpertReviewForRound(persisted, suggestions, 'round-1'),
    optimizationExpertReviewState(immediate),
  );
  assert.equal(optimizationExpertReviewForRound({
    task: { node: { id: 'task-old', status: 'waiting_user', output: {
      phase: 'expert_review', suggestionIds: ['sg-old'], validationErrors: [],
    } } },
  }, suggestions, 'round-1'), null);
});

test('诊断完成且存在非阻断包修复项后才允许生成建议', () => {
  const diagnoses = [
    { id: 'dg-old', roundId: 'round-old', packageOptimizable: true, blocking: false, resolutionStatus: 'confirmed' },
    { id: 'dg-data', roundId: 'round-1', packageOptimizable: false, blocking: false, resolutionStatus: 'resolved' },
    { id: 'dg-package', roundId: 'round-1', packageOptimizable: true, blocking: false, resolutionStatus: 'confirmed' },
  ];
  const current = diagnosesForRound(diagnoses, 'round-1');
  assert.deepEqual(current.map((item) => item.id), ['dg-data', 'dg-package']);
  assert.equal(canProposeFromDiagnoses(current), true);
  assert.equal(canProposeFromDiagnoses([{
    packageOptimizable: true, blocking: false, resolutionStatus: 'pending',
  }]), false);
  assert.equal(canProposeFromDiagnoses([{
    packageOptimizable: true, blocking: true, resolutionStatus: 'confirmed',
  }]), false);
});

test('人工改判 API 必须提交非空 reason', async () => {
  assert.equal(requireOverrideReason('  同义表达可接受  '), '同义表达可接受');
  assert.throws(() => requireOverrideReason('  '), /请填写人工改判理由/);
  const originalFetch = globalThis.fetch;
  let body: any;
  globalThis.fetch = (async (_url, options) => {
    body = JSON.parse(String(options?.body));
    return new Response(JSON.stringify({ result: { id: 'result-1' } }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;
  try {
    await api.overrideVerdict('project-1', 'result-1', 'pass', '同义表达可接受');
    assert.deepEqual(body, { verdict: 'pass', reason: '同义表达可接受' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('优化应用专家复核 API 使用可恢复状态和单一决策请求', async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; options: RequestInit }> = [];
  globalThis.fetch = (async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ status: 'retry_ready', suggestionIds: ['sg-1'] }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;
  try {
    await api.getOptimizeApplyStatus('项目/甲');
    await api.reviewOptimizeApply('项目/甲', 'task-1', 'retry', '补充边界约束');
    assert.equal(calls[0].url, '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E7%94%B2/optimize/apply-status');
    assert.equal(calls[1].url, '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E7%94%B2/optimize/apply-review');
    assert.equal(calls[1].options.body, JSON.stringify({
      taskId: 'task-1', action: 'retry', note: '补充边界约束',
    }));
  } finally {
    globalThis.fetch = originalFetch;
  }
});
