import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createProjectEventParser,
  mergeProjectEvents,
} from '../frontend/src/project-events.js';

test('SSE 消息可跨任意分块读取', () => {
  const received: unknown[] = [];
  const parser = createProjectEventParser({ onEvent: (event) => received.push(event) });

  assert.deepEqual(parser.push('id: 7\nevent: run.updated\nda'), []);
  assert.deepEqual(parser.push('ta: {"seq":7,"project_id":"p1",'), []);
  const events = parser.push('"payload":{"status":"running"}}\n\n');

  assert.equal(events.length, 1);
  assert.deepEqual(events[0], received[0]);
  assert.equal(events[0].seq, 7);
  assert.equal(events[0].type, 'run.updated');
  assert.equal(events[0].projectId, 'p1');
  assert.deepEqual(events[0].payload, { status: 'running' });
  assert.equal(parser.getLastSeq(), 7);
});

test('SSE 解析支持多行数据、CRLF 和 UTF-8 字节分块', () => {
  const encoder = new TextEncoder();
  const bytes = encoder.encode('id: 8\r\ndata: 第一行\r\ndata: 第二行\r\n\r\n');
  const parser = createProjectEventParser();
  const splitAt = encoder.encode('id: 8\r\ndata: ').length + 1;

  const first = parser.push(bytes.slice(0, splitAt));
  const second = parser.push(bytes.slice(splitAt));

  assert.equal(first.length, 0);
  assert.equal(second.length, 1);
  assert.equal(second[0].payload, '第一行\n第二行');
  assert.equal(second[0].seq, 8);
});

test('SSE 的回车换行可以落在两个分块中', () => {
  const parser = createProjectEventParser();
  assert.deepEqual(parser.push('data: {"seq":9}\r'), []);
  assert.deepEqual(parser.push('\n\r'), []);
  const events = parser.push('\n');
  assert.equal(events.length, 1);
  assert.equal(events[0].seq, 9);
});

test('重连后按 seq 忽略已收到的消息', () => {
  const parser = createProjectEventParser({ lastSeq: 10 });
  const events = parser.push([
    'id: 10',
    'data: {"seq":10,"type":"old"}',
    '',
    'id: 11',
    'data: {"seq":11,"type":"new"}',
    '',
    '',
  ].join('\n'));

  assert.deepEqual(events.map((event) => event.type), ['new']);
  assert.equal(parser.getLastSeq(), 11);
});

test('连接结束时仍会交付最后一条完整消息', () => {
  const parser = createProjectEventParser();
  parser.push('data: {"seq":2,"type":"done"}');
  const events = parser.flush();

  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'done');
});

test('事件列表按 seq 合并并去重', () => {
  const merged = mergeProjectEvents(
    [{ id: '2', seq: 2 }, { id: '1', seq: 1 }],
    [{ id: '2-copy', seq: 2 }, { id: '3', seq: 3 }],
  );
  assert.deepEqual(merged.map((event) => event.seq), [1, 2, 3]);
});
