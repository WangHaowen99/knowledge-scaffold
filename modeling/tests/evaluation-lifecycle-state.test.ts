import assert from 'node:assert/strict';
import test from 'node:test';

import { rememberEvaluationView } from '../frontend/src/evaluationLifecycleState.js';

test('评测页签保留已访问视图以避免重复挂载取数', () => {
  const initial = new Set(['cases']);
  const withDiagnosis = rememberEvaluationView(initial, 'diagnose');
  const repeated = rememberEvaluationView(withDiagnosis, 'cases');

  assert.deepEqual([...withDiagnosis], ['cases', 'diagnose']);
  assert.equal(repeated, withDiagnosis);
  assert.deepEqual([...initial], ['cases']);
});
