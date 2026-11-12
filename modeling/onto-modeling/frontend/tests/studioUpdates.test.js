// Studio 表视图「最后更新时间」列的显示与排序（lib/studioUpdates.js）。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { compareUpdatedAt, formatUpdatedAt, withUpdatedAt } from '../src/lib/studioUpdates.js';

test('时间按浏览器时区精确到秒，取不到显示破折号', () => {
  const seconds = new Date(2026, 0, 2, 3, 4, 5).getTime() / 1000;
  assert.equal(formatUpdatedAt(seconds), '2026-01-02 03:04:05');
  assert.equal(formatUpdatedAt(undefined), '—');
  assert.equal(formatUpdatedAt(0), '—');
});

test('按路径并入时间，没有时间的节点保持 undefined', () => {
  const rows = withUpdatedAt(
    [{ id: 'a', path: 'ontology/object/a.md' }, { id: 'b', path: 'ontology/rule/b.md' }],
    { 'ontology/object/a.md': 100 },
  );
  assert.deepEqual(rows.map((row) => row.updatedAt), [100, undefined]);
});

// 模拟 antd：降序时把比较结果取反
const sortRows = (rows, order) =>
  [...rows]
    .sort((a, b) => (order === 'ascend' ? 1 : -1) * compareUpdatedAt(a, b, order))
    .map((row) => row.id);

test('升序降序都按时间排，取不到时间的行始终在最后，同一时间保持原顺序', () => {
  const rows = [
    { id: 'missing', updatedAt: undefined },
    { id: 'old', updatedAt: 100 },
    { id: 'same-1', updatedAt: 200 },
    { id: 'same-2', updatedAt: 200 },
    { id: 'new', updatedAt: 300 },
  ];
  assert.deepEqual(sortRows(rows, 'descend'), ['new', 'same-1', 'same-2', 'old', 'missing']);
  assert.deepEqual(sortRows(rows, 'ascend'), ['old', 'same-1', 'same-2', 'new', 'missing']);
});
