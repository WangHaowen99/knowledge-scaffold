import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { api } from '../src/lib/api.js';

const source = readFileSync(new URL('../src/components/ChatPane.jsx', import.meta.url), 'utf8');
const start = source.indexOf('  const respondPermission = async');
const handler = source.slice(start, source.indexOf('  const reaskQuestion =', start));
const sendStart = source.indexOf('  const send = async');
const sendHandler = source.slice(sendStart, source.indexOf('  const confirmGoal =', sendStart));

function responseHarness() {
  const errors = [], warnings = [], sends = [], states = [], resolved = [];
  const view = { draft: '合成聊天草稿', attachments: ['synthetic.md'], submitting: false };
  const context = {
    project: { id: 'project_alpha' }, conversationIdRef: { current: 'default' },
    sendingRef: { current: false }, uploading: false, goalPending: false,
    setSubmitting: (value) => { view.submitting = value; },
    setLocalResolved: (update) => resolved.push(...update(new Set())),
    setState: (state) => states.push(state), setConversationsEpoch: () => {},
    message: { error: (text) => errors.push(text), warning: (text) => warnings.push(text), info: () => {} },
    api: {
      respondPermission: async () => { throw Object.assign(new Error('合成失效'), { status: 410 }); },
      forwardQuestionAnswer: (...args) => { sends.push(args); return context.deliver(); },
      sendMessage: () => assert.fail('补发不得调用普通消息接口'),
    },
    deliver: async () => ({ state: 'running' }),
    setInput: () => assert.fail('补发不得修改草稿'),
    setAttachments: () => assert.fail('补发不得修改附件'),
    setPending: () => assert.fail('补发不得冒充普通输入消息'),
  };
  runInNewContext(`${sendHandler}\n${handler}\nthis.respond = respondPermission; this.send = send;`, context);
  return { context, view, errors, warnings, sends, states, resolved, respond: context.respond, send: context.send };
}

test('补发保留草稿附件，发送中共用锁并锁定点击前的会话，显示后端实际状态', async () => {
  const h = responseHarness();
  let finish, started;
  const sending = new Promise((resolve) => { started = resolve; });
  h.context.deliver = () => new Promise((resolve) => { finish = resolve; started(); });
  const answers = { 合成问题: '合成答案' };
  const pending = h.respond('request_demo', 'allow', answers);
  h.context.conversationIdRef.current = 'conversation_next';
  await sending;
  assert.equal(h.view.submitting, true);
  assert.equal(h.context.sendingRef.current, true);
  assert.deepEqual(h.sends[0], ['project_alpha', 'request_demo', answers, 'default']);
  await h.send('合成普通消息');
  await assert.rejects(h.respond('request_demo', 'allow', answers), /消息正在提交/);
  assert.equal(h.sends.length, 1);
  assert.equal(h.warnings.length, 2);
  finish({ state: 'queued' });
  assert.equal((await pending).behavior, 'forwarded');
  assert.equal(h.view.draft, '合成聊天草稿');
  assert.deepEqual(h.view.attachments, ['synthetic.md']);
  assert.deepEqual(h.resolved, ['request_demo']);
  assert.equal(h.view.submitting, false);
  assert.equal(h.context.sendingRef.current, false);
  assert.deepEqual(h.states, ['queued']);
});

test('真实补发处理器拦截未确认目标和重复发送，409 展示原因且不提前解决原卡', async () => {
  for (const [field, value, expected] of [
    ['goalPending', true, /确认建模目标/],
    ['uploading', true, /消息正在提交/],
    ['sendingRef', { current: true }, /消息正在提交/],
  ]) {
    const h = responseHarness();
    h.context[field] = value;
    await assert.rejects(h.respond('request_demo', 'allow', { 合成问题: '合成答案' }), expected);
    assert.equal(h.sends.length, 0);
    assert.equal(h.warnings.length, 1);
    assert.deepEqual(h.resolved, []);
  }
  const h = responseHarness();
  h.context.deliver = async () => { throw Object.assign(new Error('合成 409 冲突原因'), { status: 409 }); };
  await assert.rejects(h.respond('request_demo', 'allow', { 合成问题: '合成答案' }), /合成 409 冲突原因/);
  assert.deepEqual(h.errors, ['合成 409 冲突原因']);
  assert.deepEqual(h.resolved, []);
  assert.equal(h.context.sendingRef.current, false);
  h.context.deliver = async () => ({ state: 'running' });
  assert.equal((await h.respond('request_demo', 'allow', { 合成问题: '合成答案' })).behavior, 'forwarded');
  assert.equal(h.sends.length, 2);
});

test('正常问答只提交权限答案，不触发补发或消息发送', async () => {
  const h = responseHarness();
  const answers = { 合成问题: '合成答案' };
  h.context.api.respondPermission = async (...args) => {
    assert.deepEqual(args, ['project_alpha', 'request_demo', 'allow', '', answers]);
  };
  await h.respond('request_demo', 'allow', answers);
  assert.equal(h.sends.length, 0);
  assert.equal(h.view.submitting, false);
});

test('普通消息契约不变，补发使用独立接口且不传重复消息文本', async () => {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  const calls = [];
  globalThis.localStorage = { getItem: () => null };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options.method, body: JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => ({ state: 'queued' }) };
  };
  try {
    await api.sendMessage('project_alpha', '合成普通消息', 'default');
    await api.forwardQuestionAnswer('project_alpha', 'request_demo', { 合成问题: '合成答案' }, 'default');
    assert.deepEqual(calls, [
      { path: '/api/projects/project_alpha/session/messages', method: 'POST',
        body: { text: '合成普通消息', conversation_id: 'default' } },
      { path: '/api/projects/project_alpha/session/questions/request_demo/forward', method: 'POST',
        body: { answers: { 合成问题: '合成答案' }, conversation_id: 'default' } },
    ]);
  } finally {
    Object.assign(globalThis, originals);
  }
});
