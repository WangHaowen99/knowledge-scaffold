// 往前翻页（S-06 before_seq）：旧的一页接到前面，与一次性归并整段的结果一致
import assert from 'node:assert/strict';
import test from 'node:test';
import { applyEvents, initialConversation, prependHistory } from '../src/state/conversation.js';

const user = (seq, id, text) => ({ id: seq, type: 'message.user', ts: '2026-11-03T10:00:00Z', data: { message_id: id, text } });
const card = (seq, id, title, type = 'card') => ({ id: seq, type, ts: '2026-11-03T10:00:00Z', data: { card_id: id, card_type: 'round', title } });

test('翻页：旧页接在前面，顺序与整段归并相同', () => {
  const all = [user(1, 'u1', '一'), card(2, 'c1', '旧标题'), user(3, 'u2', '二'), card(4, 'c1', '新标题', 'card.updated'), user(5, 'u3', '三')];
  const whole = applyEvents(initialConversation(), all);
  const paged = prependHistory(applyEvents(initialConversation(), all.slice(3)), all.slice(0, 3));
  assert.deepEqual(paged.order, whole.order);
  const view = (st) => st.order.map((k) => st.items[k].card?.title || st.items[k].text);
  assert.deepEqual(view(paged), view(whole));
  assert.ok(view(paged).includes('新标题'));
});

test('翻页：记下翻出来的键（不播入场），已有的键不算', () => {
  const s = prependHistory(applyEvents(initialConversation(), [user(3, 'u2', '二')]), [user(1, 'u1', '一'), user(2, 'u2', '二')]);
  assert.deepEqual(s.prependedKeys, ['msg:u1']);
  assert.equal(s.historyGen, 1);
  assert.deepEqual(s.order, ['msg:u1', 'msg:u2']);
});
