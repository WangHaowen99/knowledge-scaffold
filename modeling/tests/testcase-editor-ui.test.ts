import assert from 'node:assert/strict';
import test from 'node:test';

import { canSaveTestcasePatch, normalizeTestcasePatch } from '../frontend/src/M3Panels.jsx';

test('评测用例编辑保存前会 trim，并阻止空白问题提交', () => {
  assert.equal(canSaveTestcasePatch('   '), false);
  assert.equal(canSaveTestcasePatch('  真实问题  '), true);
  assert.deepEqual(normalizeTestcasePatch('  真实问题  ', '  标准答案  '), {
    question: '真实问题',
    groundTruth: '标准答案',
  });
});
