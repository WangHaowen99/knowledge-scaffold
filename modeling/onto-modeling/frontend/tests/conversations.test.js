// 会话下拉的展示规则（lib/conversations.js）。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  UNTITLED,
  conversationTime,
  conversationTitle,
  isRunning,
  sortConversations,
} from '../src/lib/conversations.js';

test('最近更新的会话排在前面，且不改动入参', () => {
  const input = [
    { id: 'default', updated_at: 100 },
    { id: 'aaaaaaaaaaaa', updated_at: 300 },
    { id: 'bbbbbbbbbbbb', updated_at: 200 },
  ];
  assert.deepEqual(
    sortConversations(input).map((c) => c.id),
    ['aaaaaaaaaaaa', 'bbbbbbbbbbbb', 'default'],
  );
  assert.equal(input[0].id, 'default');
  assert.deepEqual(sortConversations(undefined), []);
});

test('空标题显示为新会话', () => {
  assert.equal(conversationTitle({ title: '' }), UNTITLED);
  assert.equal(conversationTitle({ title: '   ' }), UNTITLED);
  assert.equal(conversationTitle(null), UNTITLED);
  assert.equal(conversationTitle({ title: '订单建模' }), '订单建模');
});

test('相对时间按分钟、小时、天分档', () => {
  const now = 1_000_000;
  assert.equal(conversationTime(now - 30, now), '刚刚');
  assert.equal(conversationTime(now - 5 * 60, now), '5 分钟前');
  assert.equal(conversationTime(now - 3 * 3600, now), '3 小时前');
  assert.equal(conversationTime(now - 2 * 86400, now), '2 天前');
  assert.equal(conversationTime(0, now), '');
  assert.notEqual(conversationTime(now - 30 * 86400, now), '');
});

test('只有引擎绑定且正忙的会话算运行中', () => {
  const live = { conversation_id: 'default', state: 'running', busy: true };
  assert.equal(isRunning(live, 'default'), true);
  assert.equal(isRunning(live, 'aaaaaaaaaaaa'), false);
  assert.equal(isRunning({ ...live, busy: false }, 'default'), false);
  assert.equal(isRunning(null, 'default'), false);
});
