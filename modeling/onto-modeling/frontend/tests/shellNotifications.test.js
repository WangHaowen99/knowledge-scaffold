// 用户流通知合进列表（I1 审查修复第 5 条）
import assert from 'node:assert/strict';
import test from 'node:test';
import { applyNotification } from '../src/shell/notifications.js';

test('同组未读被替换：列表只留新的那条，未读数不越加越多', () => {
  let s = { items: [], unread: 0 };
  s = applyNotification(s, { id: 'n1', read: false, replaces: [] });
  s = applyNotification(s, { id: 'n2', read: false, replaces: ['n1'] });
  s = applyNotification(s, { id: 'n3', read: false, replaces: ['n2'] });
  assert.deepEqual(s.items.map((n) => n.id), ['n3']);
  assert.equal(s.unread, 1);
  assert.equal('replaces' in s.items[0], false);
});

test('替换的旧通知已读、或不在本地列表里：未读数照常只加新的', () => {
  let s = { items: [{ id: 'old', read: true }], unread: 3 };
  s = applyNotification(s, { id: 'n4', read: false, replaces: ['old', 'not-loaded'] });
  assert.deepEqual(s.items.map((n) => n.id), ['n4']);
  assert.equal(s.unread, 4);
  // 后端没带 replaces（旧版本）：照旧加一条
  s = applyNotification(s, { id: 'n5', read: false });
  assert.deepEqual(s.items.map((n) => n.id), ['n5', 'n4']);
  assert.equal(s.unread, 5);
});
