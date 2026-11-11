import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import {
  clearWorkspaceDirty,
  hasWorkspaceDirty,
  setWorkspaceDirty,
  workspaceDirtySources,
} from '../src/lib/workspaceDirty.js';

afterEach(clearWorkspaceDirty);

test('多个编辑器独立登记，全部清理后才允许回退', () => {
  setWorkspaceDirty('plain-editor', true);
  setWorkspaceDirty('flowchart-editor', true);
  assert.equal(hasWorkspaceDirty(), true);
  assert.deepEqual(workspaceDirtySources(), ['flowchart-editor', 'plain-editor']);

  setWorkspaceDirty('plain-editor', false);
  assert.equal(hasWorkspaceDirty(), true);
  setWorkspaceDirty('flowchart-editor', false);
  assert.equal(hasWorkspaceDirty(), false);
});

test('重复登记同一编辑器不会产生幽灵脏状态', () => {
  setWorkspaceDirty('studio-editor', true);
  setWorkspaceDirty('studio-editor', true);
  assert.deepEqual(workspaceDirtySources(), ['studio-editor']);
  setWorkspaceDirty('studio-editor', false);
  assert.equal(hasWorkspaceDirty(), false);
});
