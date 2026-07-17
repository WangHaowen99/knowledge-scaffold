import assert from 'node:assert/strict';
import test from 'node:test';

import {
  formatDurationMs,
  formatTime,
  groupSessions,
  truncate,
} from './chatPresentation.js';

test('groupSessions keeps pinned sessions separate and applies title search', () => {
  const now = Date.now();
  const sessions = [
    { id: 'pinned', title: '固定分析', updated_at: new Date(now).toISOString(), metadata: { pinned: true } },
    { id: 'today', title: '今日分析', updated_at: new Date(now).toISOString() },
    { id: 'old', title: '历史记录', updated_at: '2020-01-01T00:00:00.000Z' },
  ];

  assert.deepEqual(groupSessions(sessions, '').map(([label, items]) => [label, items.map(({ id }) => id)]), [
    ['已固定', ['pinned']],
    ['今天', ['today']],
    ['更早', ['old']],
  ]);
  assert.deepEqual(groupSessions(sessions, '分析').map(([label]) => label), ['已固定', '今天']);
});

test('chat presentation formatters normalize empty and compact values', () => {
  assert.equal(formatTime('not-a-date'), '');
  assert.match(formatTime('2020-01-01T00:00:07.000Z'), /\d{2}:\d{2}:\d{2}$/);
  assert.equal(truncate('  多行\n  文本  ', 20), '多行 文本');
  assert.equal(truncate('abcdef', 3), 'abc…');
  assert.equal(formatDurationMs(999), '999 ms');
  assert.equal(formatDurationMs(1500), '1.50 s');
  assert.equal(formatDurationMs(-1), '');
});
