import assert from 'node:assert/strict';
import test from 'node:test';
import { EventStream, backoffDelay, createSSEParser, resumeQuery } from '../src/api/sse.js';

test('解析器：retry、id、多行 data、注释，跨分片拼接', () => {
  const p = createSSEParser();
  let frames = p.push('retry: 3000\n\nid: 15');
  assert.equal(p.retry, 3000);
  assert.deepEqual(frames, []);
  frames = p.push('32\ndata: {"a":\ndata: 1}\n\n: 注释\r\ndata: {"type":"ping"}\r\n\r\n');
  assert.deepEqual(frames, [{ id: '1532', data: '{"a":\n1}' }, { id: null, data: '{"type":"ping"}' }]);
});

test('resumeQuery：对话流用 after，项目流用 last_event_id', () => {
  assert.deepEqual(resumeQuery('conversation', 12), { after: 12 });
  assert.deepEqual(resumeQuery('project', 'e1.40'), { last_event_id: 'e1.40' });
  assert.deepEqual(resumeQuery('user', null), {});
});

test('退避：3s 起步翻倍，最长 30s', () => {
  assert.equal(backoffDelay(0), 0);
  assert.equal(backoffDelay(1), 3000);
  assert.equal(backoffDelay(2), 6000);
  assert.equal(backoffDelay(9), 30000);
});

// 一个可控的 SSE 响应：push 写帧，end 关流
function sseResponse() {
  let ctrl;
  const enc = new TextEncoder();
  const body = new ReadableStream({ start(c) { ctrl = c; } });
  return {
    res: new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    push: (s) => ctrl.enqueue(enc.encode(s)),
    end: () => ctrl.close(),
  };
}
const frame = (id, type, data = {}) => `${id != null ? `id: ${id}\n` : ''}data: ${JSON.stringify({ id: id != null ? String(id) : undefined, type, data })}\n\n`;
const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms));

test('对话流：断线后带 after=<最后 seq> 重连，回放的重复事件被丢弃，ping 不上抛', async () => {
  const urls = [];
  const conns = [];
  const got = [];
  const stream = new EventStream({
    kind: 'conversation',
    url: (q) => { urls.push(q); return `/x?after=${q.after ?? ''}`; },
    fetchImpl: async () => { const c = sseResponse(); conns.push(c); return c.res; },
    getToken: () => 't',
    onEvent: (e) => got.push(`${e.type}:${e.id ?? ''}`),
    heartbeatMs: 1000,
  });
  stream.retryMs = 10;
  stream.start();
  await tick();
  conns[0].push('retry: 10\n\n');
  conns[0].push(frame(1, 'message.user') + frame(null, 'ping') + frame(2, 'notice'));
  await tick();
  conns[0].end();
  await tick(40);
  assert.deepEqual(urls[1], { after: 2 });
  conns[1].push(frame(2, 'notice') + frame(3, 'card'));
  await tick();
  stream.stop();
  assert.deepEqual(got, ['message.user:1', 'notice:2', 'card:3']);
});

test('项目流：stream.reset 交给 onReset；带 Last-Event-ID 头重连', async () => {
  const conns = [];
  const headers = [];
  const resets = [];
  const stream = new EventStream({
    kind: 'project',
    url: (q) => `/p?last=${q.last_event_id ?? ''}`,
    fetchImpl: async (u, init) => { headers.push(init.headers['Last-Event-ID']); const c = sseResponse(); conns.push(c); return c.res; },
    onEvent: () => {},
    onReset: (d) => resets.push(d.resync),
    heartbeatMs: 1000,
  });
  stream.retryMs = 5;
  stream.start();
  await tick();
  conns[0].push(frame('e1.7', 'flow.updated') + frame(null, 'stream.reset', { reason: 'gap', resync: ['FL-01', 'IN-01'] }));
  await tick();
  conns[0].end();
  await tick(30);
  stream.stop();
  assert.deepEqual(resets, [['FL-01', 'IN-01']]);
  assert.equal(headers[0], undefined);
  assert.equal(headers[1], 'e1.7');
});

test('心跳超时：没有任何帧就重连', async () => {
  let opens = 0;
  const stream = new EventStream({
    kind: 'user',
    url: () => '/u',
    fetchImpl: async () => { opens += 1; return sseResponse().res; },
    onEvent: () => {},
    heartbeatMs: 20,
  });
  stream.retryMs = 1;
  stream.start();
  await tick(70);
  stream.stop();
  assert.ok(opens >= 2, `opens=${opens}`);
});

test('auth.expired：刷新令牌后立即重连；401 打开失败且刷新失败时停止', async () => {
  const conns = [];
  let refreshed = 0;
  const stream = new EventStream({
    kind: 'project',
    url: () => '/p',
    fetchImpl: async () => { const c = sseResponse(); conns.push(c); return c.res; },
    refresh: async () => { refreshed += 1; return true; },
    onEvent: () => {},
    heartbeatMs: 1000,
  });
  stream.start();
  await tick();
  conns[0].push(frame(null, 'auth.expired'));
  await tick(20);
  stream.stop();
  assert.equal(refreshed, 1);
  assert.equal(conns.length, 2);

  const statuses = [];
  let authRequired = 0;
  const s2 = new EventStream({
    kind: 'project',
    url: () => '/p',
    fetchImpl: async () => new Response('{}', { status: 401 }),
    refresh: async () => false,
    onEvent: () => {},
    onStatus: (s) => statuses.push(s),
    onAuthRequired: () => { authRequired += 1; },
  });
  s2.start();
  await tick(20);
  assert.equal(authRequired, 1);
  assert.equal(statuses.at(-1), 'failed');
});
