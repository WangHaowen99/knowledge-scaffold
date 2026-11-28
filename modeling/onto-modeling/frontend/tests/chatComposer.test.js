// F7：发送与停止合成一个按钮；运行中插的话送到哪了；平台代存的下一条命令
import assert from 'node:assert/strict';
import test from 'node:test';
import { applyEvents, applySnapshot, initialConversation } from '../src/state/conversation.js';
import {
  STEER_WAIT_MS, deliveryNote, fmtElapsed, loadDraft, restoreText, saveDraft, sendButtonMode,
} from '../src/pages/workbench/composerModel.js';

let n = 0;
const ev = (type, data, ts = '2026-11-04T06:51:10.000Z') => ({ id: String(++n), type, ts, data });

test('按钮：有内容就发送；没内容时运行中是停止、空闲时灰掉；输入框禁用时一律灰掉', () => {
  assert.equal(sendButtonMode({ canStop: false, hasContent: false, disabled: false }), 'off');
  assert.equal(sendButtonMode({ canStop: false, hasContent: true, disabled: false }), 'send');
  assert.equal(sendButtonMode({ canStop: true, hasContent: false, disabled: false }), 'stop');
  assert.equal(sendButtonMode({ canStop: true, hasContent: true, disabled: false }), 'send');
  assert.equal(sendButtonMode({ canStop: true, hasContent: true, disabled: true }), 'off');
});

test('插话的送达说明：先等、等久了写明在等哪个子任务并给打断并发送，看到了写时间，撤回写已撤回', () => {
  const sent = Date.parse('2026-11-04T06:51:10.000Z');
  const item = { delivery: 'steer', ts: '2026-11-04T06:51:10.000Z' };
  assert.deepEqual(deliveryNote(item, sent + 5000), { text: '等助手看到' });
  const waiting = { ...item, delivery_state: { state: 'pending', waiting_on: { kind: 'subagent', title: '语义检查' } } };
  assert.deepEqual(deliveryNote(waiting, sent + 6 * 60000), { text: '子任务「语义检查」结束后助手才能看到 · 已等 6 分', stopAndSend: true });
  assert.equal(deliveryNote(item, sent + STEER_WAIT_MS).text, '这一步做完后助手才能看到 · 已等 30 秒');
  assert.match(deliveryNote({ ...item, delivery_state: { state: 'seen', at: '2026-11-04T07:00:18.000Z' } }).text, /^助手已看到 \d\d:\d\d$/);
  assert.deepEqual(deliveryNote({ ...item, delivery_state: { state: 'returned' } }), { text: '已撤回' });
  assert.equal(deliveryNote({ ...item, delivery_state: { state: 'new_turn' } }), null);
  assert.equal(deliveryNote({ delivery: 'immediate' }), null);
  assert.deepEqual(deliveryNote({ delivery: 'deferred' }), { text: '下一步开始前带给助手' });
});

test('状态机：送达事件改到对应的气泡上；本地气泡还没换成服务端那条时按 message_id 找', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('message.user', { message_id: 'm-651', text: '顺便看下规则', delivery: 'steer' }),
    ev('message.delivery', { message_id: 'm-651', state: 'seen', at: '2026-11-04T07:00:18.000Z', waiting_on: null }),
  ]);
  assert.deepEqual(s.items['msg:m-651'].delivery_state, { state: 'seen', at: '2026-11-04T07:00:18.000Z', waiting_on: null });
  s = { ...s, order: [...s.order, 'local:cm-1'], items: { ...s.items, 'local:cm-1': { kind: 'user', local: true, message_id: 'm-700', text: '再补一句', delivery: 'steer' } } };
  s = applyEvents(s, [ev('message.delivery', { message_id: 'm-700', state: 'returned', at: null, waiting_on: null })]);
  assert.equal(s.items['local:cm-1'].delivery_state.state, 'returned');
  // 没有对应气泡的送达事件不出错
  assert.equal(applyEvents(s, [ev('message.delivery', { message_id: 'm-x', state: 'seen' })]).items['msg:m-x'], undefined);
});

test('状态机：代存的下一条命令跟着事件和快照走', () => {
  n = 0;
  const next = { id: 'nx-1', label: '提取对象', command: '/ontology-extract-object', text: '提取对象', created_at: '2026-11-04T06:51:10.000Z', paused: false };
  let s = applyEvents(initialConversation(), [ev('next.updated', { next })]);
  assert.deepEqual(s.next, next);
  s = applySnapshot(s, { state: 'running' }); // 快照没带 next 字段：保持
  assert.deepEqual(s.next, next);
  s = applySnapshot(s, { state: 'idle', next: { ...next, paused: true } });
  assert.equal(s.next.paused, true);
  s = applyEvents(s, [ev('next.updated', { next: null })]);
  assert.equal(s.next, null);
});

test('计时与撤回的话放回输入框', () => {
  assert.equal(fmtElapsed(372000), '6:12');
  assert.equal(fmtElapsed(3723000), '1:02:03');
  assert.equal(fmtElapsed(-5), '0:00');
  assert.equal(restoreText('', ['顺便看下规则']), '顺便看下规则');
  assert.equal(restoreText('新敲的', ['a', 'b']), '新敲的\na\nb');
});

test('没发出去的字按对话各存一份：不串到别的对话，清空后删掉', () => {
  const box = new Map();
  const storage = { getItem: (k) => box.get(k) ?? null, setItem: (k, v) => box.set(k, v), removeItem: (k) => box.delete(k) };
  saveDraft(storage, 'p', 'chat1', '简单介绍一下对象');
  assert.equal(loadDraft(storage, 'p', 'chat1'), '简单介绍一下对象');
  assert.equal(loadDraft(storage, 'p', 'plug1'), '');
  assert.equal(loadDraft(storage, 'q', 'chat1'), '');
  saveDraft(storage, 'p', 'chat1', '');
  assert.equal(box.size, 0);
  // 存储不可用时不报错
  const broken = { getItem: () => { throw new Error('x'); }, setItem: () => { throw new Error('x'); }, removeItem: () => {} };
  assert.equal(loadDraft(broken, 'p', 'c'), '');
  saveDraft(broken, 'p', 'c', 'x');
  assert.equal(loadDraft(null, 'p', 'c'), '');
});
