// 发送时先显示的本地气泡：服务端回传同一条后原地替换，不重复；沿用本地气泡的界面键，发送动画不被打断
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ackLocalUser, addLocalUser, applyEvent, dropLocalUser, initialConversation, timelineBlocks,
} from '../src/state/conversation.js';

const echo = (id, text, seq = 1) => ({ id: seq, type: 'message.user', ts: '2026-11-03T10:00:00Z', data: { message_id: id, text } });

test('本地气泡：先回 202 再到对话流，按 message_id 换成服务端那条，位置不变', () => {
  let s = addLocalUser(initialConversation(), 'cm-1', '提取对象');
  s = ackLocalUser(s, 'cm-1', 'u-9');
  s = applyEvent(s, echo('u-9', '提取对象'));
  assert.deepEqual(s.order, ['msg:u-9']);
  assert.equal(s.items['msg:u-9'].local_key, 'local:cm-1');
  assert.equal(timelineBlocks(s).length, 1);
});

test('本地气泡：对话流比 202 先到，按正文对上；之后的 ack 不再加回来', () => {
  let s = addLocalUser(initialConversation(), 'cm-1', '提取对象');
  s = applyEvent(s, echo('u-9', '提取对象'));
  s = ackLocalUser(s, 'cm-1', 'u-9');
  assert.deepEqual(s.order, ['msg:u-9']);
});

test('本地气泡：发送失败时去掉；别人发的消息照常追加', () => {
  let s = addLocalUser(initialConversation(), 'cm-1', '提取对象');
  s = dropLocalUser(s, 'cm-1');
  assert.deepEqual(s.order, []);
  s = applyEvent(s, echo('u-3', '另一条'));
  assert.deepEqual(s.order, ['msg:u-3']);
  assert.equal(s.items['msg:u-3'].local_key, undefined);
});
