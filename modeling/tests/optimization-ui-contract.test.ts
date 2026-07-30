import assert from 'node:assert/strict';
import test from 'node:test';

import {
  candidateStatusLabel,
  linkedOptimizationCandidates,
  optimizationCandidateNotice,
} from '../frontend/src/optimization-candidates.js';
import {
  canProposeFromDiagnoses,
  diagnosesForRound,
  requireOverrideReason,
} from '../frontend/src/optimization-flow.js';
import * as api from '../frontend/src/api.js';

test('刷新后由建议关联恢复优化候选，且只接受单一候选响应契约', () => {
  const suggestions = [{ id: 'sg-1', candidateRevisionId: 'cr-1', status: 'candidate' }];
  const candidates = [
    { id: 'cr-1', status: 'pending' },
    { id: 'cr-other', status: 'pending' },
  ];
  assert.deepEqual(linkedOptimizationCandidates(suggestions, candidates), [candidates[0]]);
  assert.equal(candidateStatusLabel({ status: 'superseded' }), '已过期');
  assert.deepEqual(optimizationCandidateNotice({
    candidateId: 'cr-1', status: 'pending', selected: 1, basedRevisionOid: 'rev-1',
  }), {
    candidateId: 'cr-1', status: 'pending', selected: 1, basedRevisionOid: 'rev-1',
  });
  assert.throws(() => optimizationCandidateNotice({ version: '1.1.0', applied: 1 }), /响应不完整/);
});

test('诊断完成且存在非阻断包修复项后才允许生成建议', () => {
  const diagnoses = [
    { id: 'dg-old', roundId: 'round-old', packageOptimizable: true, blocking: false },
    { id: 'dg-data', roundId: 'round-1', packageOptimizable: false, blocking: false },
    { id: 'dg-package', roundId: 'round-1', packageOptimizable: true, blocking: false },
  ];
  const current = diagnosesForRound(diagnoses, 'round-1');
  assert.deepEqual(current.map((item) => item.id), ['dg-data', 'dg-package']);
  assert.equal(canProposeFromDiagnoses(current), true);
  assert.equal(canProposeFromDiagnoses([{ packageOptimizable: true, blocking: true }]), false);
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
