import assert from 'node:assert/strict';
import { test } from 'node:test';
import { missingPublishStages } from '../src/lib/packagePublish.js';

const stages = ['research', 'flowchart', 'object', 'operation', 'rulemetric', 'validate', 'mock', 'trace', 'eval'].map((key) => ({ key }));
test('publication requires modeling through validation, not later validation exercises', () => {
  assert.deepEqual(missingPublishStages(stages, ['finish', 'wait', 'finish', 'finish', 'wait', 'finish', 'wait', 'wait', 'wait']), []);
});
test('unfinished required stages identify the reason publication is blocked', () => {
  assert.deepEqual(missingPublishStages(stages, ['finish', 'wait', 'finish', 'wait', 'wait', 'process']).map((s) => s.key), ['operation', 'validate']);
});
