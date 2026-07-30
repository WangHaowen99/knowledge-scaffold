import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  makeRestoreRequest,
  normalizeDiff,
  normalizeVersions,
  safeUiError,
  summarizeDiff,
  versionActor,
  versionSummary,
  versionTitle,
} from '../frontend/src/revision-state.js';

test('版本列表兼容两种字段格式并按序号倒序排列', () => {
  const versions = normalizeVersions({ revisions: [
    {
      id: 'old',
      sequence: 2,
      package_version: '1.0.0',
      actor: { kind: 'agent', display_name: '助手' },
      changed_paths: ['package/index.md'],
      created_at: '2026-07-20T10:00:00Z',
    },
    {
      id: 'new',
      sequence: 3,
      packageVersion: '1.0.1',
      actor: { kind: 'user', displayName: '王老师' },
      changedPaths: ['package/rules/a.md'],
      createdAt: '2026-07-21T10:00:00Z',
    },
  ] });

  assert.deepEqual(versions.map((item) => item.id), ['new', 'old']);
  assert.equal(versions[0].actor.displayName, '王老师');
  assert.deepEqual(versions[1].changedPaths, ['package/index.md']);
  assert.equal(versionTitle(versions[0]), '版本 3');
});

test('版本说明和操作人使用通俗的默认文字', () => {
  assert.equal(versionSummary({ id: 'a', cause: 'validation_fix' }), '修复问题');
  assert.equal(versionSummary({ id: 'b', cause: 'unknown' }), '内容更新');
  assert.equal(versionActor({ id: 'a', actor: { kind: 'agent' } }), '智能助手');
  assert.equal(versionTitle({ id: 'c', packageVersion: '2.1.0' }), '版本 2.1.0');
});

test('文件修改兼容下划线字段并计算汇总', () => {
  const diff = normalizeDiff({
    from_revision: 'before',
    to_revision: 'after',
    files: [
      {
        path: 'package/index.md',
        status: 'modified',
        hunks: [{
          old_start: 4,
          new_start: 4,
          lines: [
            { type: 'context', content: '标题' },
            { type: 'remove', content: '旧内容' },
            { type: 'add', content: '新内容' },
          ],
        }],
      },
      { path: 'package/rules/new.md', status: 'added', additions: 3, deletions: 0 },
    ],
  });

  assert.equal(diff.fromRevision, 'before');
  assert.equal(diff.files[0].hunks[0].oldStart, 4);
  assert.deepEqual(summarizeDiff(diff), {
    files: 2,
    added: 1,
    modified: 1,
    deleted: 0,
    additions: 4,
    deletions: 1,
  });
});

test('恢复请求同时携带目标版本和当前版本', () => {
  assert.deepEqual(makeRestoreRequest('older', 'current'), {
    targetRevision: 'older',
    expectedRevision: 'current',
  });
  assert.throws(() => makeRestoreRequest('current', 'current'), /当前已是这个版本/);
  assert.throws(() => makeRestoreRequest('', 'current'), /请选择/);
});

test('页面不会直接显示底层报错术语', () => {
  assert.equal(safeUiError('revision_conflict', '内容已更新，请重试'), '内容已更新，请重试');
  assert.equal(safeUiError('网络暂时不可用'), '网络暂时不可用');
});
