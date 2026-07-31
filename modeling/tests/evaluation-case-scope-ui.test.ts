import assert from 'node:assert/strict';
import test from 'node:test';

import { summarizeEvaluationCases } from '../frontend/src/evaluationCaseScope.js';

test('评测页面区分真实用例和知识包建议问题', () => {
  const summary = summarizeEvaluationCases([
    { id: 'manual-ready', source: 'manual', question: '真实场景一', groundTruth: '答案一' },
    { id: 'manual-empty', source: 'manual', question: '真实场景二', groundTruth: '' },
    { id: 'package', source: 'package', question: '建议问题', groundTruth: '建议答案' },
  ]);

  assert.equal(summary.officialTotal, 2);
  assert.equal(summary.officialReady, 1);
  assert.equal(summary.withoutReferenceTotal, 1);
  assert.equal(summary.suggestionTotal, 1);
  assert.equal(summary.canRun, true);
});

test('只要存在真实用例即可运行，缺少参考答案的结果由人工判定', () => {
  const summary = summarizeEvaluationCases([
    { id: 'manual-empty', source: 'manual', question: '真实场景', groundTruth: '' },
  ]);
  assert.equal(summary.canRun, true);
  assert.equal(summary.withoutReferenceTotal, 1);
});
