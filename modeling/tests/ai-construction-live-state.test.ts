import assert from 'node:assert/strict';
import test from 'node:test';

import {
  liveAiConstructionMessage,
  mergeAiConstructionMessages,
  mergeAiConstructionSession,
  prependAiConstructionMessagePage,
  upsertAiConstructionMessage,
} from '../frontend/src/aiConstructionLiveState.js';

test('资料分析进度事件可直接替换当前消息而无需重新加载整个页面', () => {
  const current = [{
    id: 'progress-1', seq: 4, role: 'assistant',
    blocks: [{ type: 'status', title: '正在读取资料', progress: 30 }],
  }];
  const event = {
    type: 'ai_construction.message.written',
    payload: { message: {
      id: 'progress-1', seq: 4, role: 'assistant',
      blocks: [{ type: 'status', title: '正在检查资料质量和关键缺口', progress: 86 }],
    } },
  };

  const message = liveAiConstructionMessage(event);
  assert.equal(message?.blocks[0].progress, 86);
  assert.deepEqual(upsertAiConstructionMessage(current, message), [message]);
});

test('局部消息按序插入，非法或轻量事件交给完整刷新处理', () => {
  const current = [{ id: 'message-2', seq: 2, role: 'assistant', blocks: [] }];
  const inserted = { id: 'message-1', seq: 1, role: 'assistant', blocks: [] };

  assert.deepEqual(upsertAiConstructionMessage(current, inserted).map((item) => item.id), [
    'message-1', 'message-2',
  ]);
  assert.equal(liveAiConstructionMessage({
    type: 'ai_construction.message.written', payload: { messageId: 'message-3' },
  }), null);
  assert.equal(liveAiConstructionMessage({ type: 'other.event', payload: { message: inserted } }), null);
});

test('刷新会话时保留服务端窗口之外的实时消息和已加载旧消息', () => {
  const loaded = [
    { id: 'message-1', seq: 1, role: 'assistant', blocks: [] },
    { id: 'message-2', seq: 2, role: 'assistant', blocks: [{ type: 'text', text: '旧内容' }] },
    { id: 'message-201', seq: 201, role: 'assistant', blocks: [] },
  ];
  const refreshed = [
    { id: 'message-2', seq: 2, role: 'assistant', blocks: [{ type: 'text', text: '服务端新内容' }] },
    { id: 'message-200', seq: 200, role: 'assistant', blocks: [] },
  ];

  const merged = mergeAiConstructionMessages(loaded, refreshed);
  assert.deepEqual(merged.map((message) => message.seq), [1, 2, 200, 201]);
  assert.equal(merged.find((message) => message.id === 'message-2')?.blocks[0].text, '服务端新内容');
});

test('会话刷新保留实时消息和已加载历史页的翻页状态', () => {
  const current = {
    project: { id: 'project-1' },
    aiConstructionMessages: [
      { id: 'message-1', seq: 1, role: 'assistant', blocks: [] },
      { id: 'message-201', seq: 201, role: 'assistant', blocks: [] },
    ],
    aiConstructionMessagesPage: { hasMore: false, oldestSeq: 1, newestSeq: 201 },
  };
  const refreshed = {
    project: { id: 'project-1' },
    aiConstructionMessages: [
      { id: 'message-2', seq: 2, role: 'assistant', blocks: [] },
      { id: 'message-200', seq: 200, role: 'assistant', blocks: [] },
    ],
    aiConstructionMessagesPage: { hasMore: true, oldestSeq: 2, newestSeq: 200 },
  };

  const merged = mergeAiConstructionSession(current, refreshed);
  assert.deepEqual(merged.aiConstructionMessages.map((message) => message.seq), [1, 2, 200, 201]);
  assert.deepEqual(merged.aiConstructionMessagesPage, {
    hasMore: false, oldestSeq: 1, newestSeq: 201,
  });
});

test('加载更早消息时前插历史页并更新是否还有历史', () => {
  const session = {
    project: { id: 'project-1' },
    aiConstructionMessages: [
      { id: 'message-6', seq: 6, role: 'assistant', blocks: [] },
      { id: 'message-7', seq: 7, role: 'assistant', blocks: [] },
    ],
    aiConstructionMessagesPage: { hasMore: true, oldestSeq: 6, newestSeq: 7 },
  };
  const next = prependAiConstructionMessagePage(session, {
    messages: [
      { id: 'message-4', seq: 4, role: 'assistant', blocks: [] },
      { id: 'message-5', seq: 5, role: 'assistant', blocks: [] },
    ],
    hasMore: false, oldestSeq: 4, newestSeq: 5,
  });

  assert.deepEqual(next.aiConstructionMessages.map((message) => message.seq), [4, 5, 6, 7]);
  assert.deepEqual(next.aiConstructionMessagesPage, {
    hasMore: false, oldestSeq: 4, newestSeq: 7,
  });
});
