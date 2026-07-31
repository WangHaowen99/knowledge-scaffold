import { test } from 'node:test';
import assert from 'node:assert/strict';

import { packageWorkspaceFocus } from '../frontend/src/packageWorkspaceFocus.js';

test('统一工作台步骤映射到知识包工作区内部视图', () => {
  assert.deepEqual(packageWorkspaceFocus('ai_construction'), { tab: 'edit' });
  assert.deepEqual(packageWorkspaceFocus('scenario'), { tab: 'scenario' });
  assert.deepEqual(packageWorkspaceFocus('data_source'), { tab: 'data' });
  assert.deepEqual(packageWorkspaceFocus('evaluate'), { tab: 'evaluate' });
  assert.deepEqual(packageWorkspaceFocus('diagnose'), { tab: 'improve', improveView: 'diagnose' });
  assert.deepEqual(packageWorkspaceFocus('optimize'), { tab: 'improve', improveView: 'optimize' });
  assert.deepEqual(packageWorkspaceFocus('release'), { tab: 'release' });
});
