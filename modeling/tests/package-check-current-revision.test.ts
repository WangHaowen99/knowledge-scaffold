import assert from 'node:assert/strict';
import { test } from 'node:test';

import { currentPackageCheckView } from '../src/workflow/package-check.js';

test('旧修订体检不会作为当前体检返回可修复发现', () => {
  const view = currentPackageCheckView({
    version: '1.1.0',
    revision: 'revision-old',
    findings: [{
      id: 'old-finding', severity: '错误', category: '一致性', target: 'index.md',
      message: '这是旧修订的问题', suggestion: '不应继续显示', source: 'rule',
    }],
    createdAt: '2026-07-24T00:00:00.000Z',
  }, 'revision-current');

  assert.deepEqual(view, {
    currentRevision: 'revision-current',
    stale: true,
    check: null,
  });
  assert.doesNotMatch(JSON.stringify(view), /old-finding|这是旧修订的问题|不应继续显示/);
});

test('当前知识包不存在时也不会展示数据库残留体检', () => {
  const view = currentPackageCheckView({
    version: '1.0.0', revision: 'orphan-revision', findings: [],
    createdAt: '2026-07-24T00:00:00.000Z',
  }, null);

  assert.deepEqual(view, { currentRevision: null, stale: true, check: null });
});
