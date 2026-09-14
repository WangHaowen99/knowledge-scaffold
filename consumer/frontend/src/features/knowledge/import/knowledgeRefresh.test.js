// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  nextKnowledgeRevision,
  reconcileTreeSelection,
  resolveKnowledgeVersionOverride,
} from './knowledgeRefresh.js';

const tree = [{
  name: 'objects',
  path: 'objects',
  type: 'dir',
  children: [{ name: 'Part.md', path: 'objects/Part.md', type: 'file' }],
}, {
  name: 'operations',
  path: 'operations',
  type: 'dir',
  children: [],
}];

test('knowledge revision always advances to retrigger unchanged raw-file paths', () => {
  assert.equal(nextKnowledgeRevision(0), 1);
  assert.equal(nextKnowledgeRevision(41), 42);
  assert.equal(nextKnowledgeRevision(Number.NaN), 1);
});

test('active-changing mutations leave history only when explicitly requested', () => {
  assert.equal(resolveKnowledgeVersionOverride('1.0.0', true), null);
  assert.equal(resolveKnowledgeVersionOverride('1.0.0', false), '1.0.0');
});

test('tree refresh retains an existing file and safely falls back when it disappeared', () => {
  const selected = { dir: 'objects', file: 'objects/Part.md' };
  assert.equal(reconcileTreeSelection(tree, selected, 'objects'), selected);
  assert.deepEqual(reconcileTreeSelection(tree, { dir: 'missing', file: 'missing.md' }, 'objects'), {
    dir: 'objects',
    file: null,
  });
});
