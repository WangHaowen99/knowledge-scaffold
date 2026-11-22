import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { api } from '../src/lib/api.js';

const source = readFileSync(new URL('../src/components/ChatPane.jsx', import.meta.url), 'utf8');
const start = source.indexOf('  const respondPermission = async');
const handler = source.slice(start, source.indexOf('  const reaskQuestion =', start));

function responseHarness(status = 410) {
  const errors = [], resolved = [];
  const context = {
    project: { id: 'project_alpha' },
    setLocalResolved: (update) => resolved.push(...update(new Set())),
    message: { error: (text) => errors.push(text), info: () => {} },
    api: {
      respondPermission: async () => { throw Object.assign(new Error('合成提交失败'), { status }); },
      sendMessage: () => assert.fail('失效答案不得自动发送为普通消息'),
    },
    setInput: () => assert.fail('问题提交不得修改聊天草稿'),
    setAttachments: () => assert.fail('问题提交不得修改附件'),
    setPending: () => assert.fail('问题提交不得创建普通消息气泡'),
    setState: () => assert.fail('失效答案不得启动会话'),
    setSubmitting: () => assert.fail('问题提交不得改变普通消息的发送状态'),
  };
  runInNewContext(`${handler}\nthis.respond = respondPermission;`, context);
  return { context, errors, resolved, respond: context.respond };
}

for (const status of [410, 404]) {
  test(`${status} 失效答案交由原卡处理，不补发、不清空草稿附件、不标为答复`, async () => {
    const h = responseHarness(status);
    const answers = { 合成文字甲: '合成回答甲', 合成文字乙: '合成回答乙' };
    await assert.rejects(h.respond('request_demo', 'allow', answers), (error) => error.status === status);
    assert.deepEqual(answers, { 合成文字甲: '合成回答甲', 合成文字乙: '合成回答乙' });
    assert.deepEqual(h.resolved, []);
    assert.deepEqual(h.errors, []);
  });
}

test('普通提交错误仍展示具体原因并交由原卡保留答案供重试', async () => {
  const h = responseHarness(409);
  await assert.rejects(h.respond('request_demo', 'allow', { 合成问题: '合成答案' }), /合成提交失败/);
  assert.deepEqual(h.errors, ['合成提交失败']);
  assert.deepEqual(h.resolved, []);
});

test('正常问答只提交权限答案，不发送普通消息', async () => {
  const h = responseHarness();
  const answers = { 合成问题: '合成答案' };
  const details = { 合成问题: { selected_options: [], custom_text: '合成答案' } };
  h.context.api.respondPermission = async (...args) => {
    assert.deepEqual(args, ['project_alpha', 'request_demo', 'allow', '', answers, details]);
  };
  await h.respond('request_demo', 'allow', answers, details);
  assert.deepEqual(h.resolved, []);
});

test('普通消息契约不变，权限接口兼容旧调用并支持可选选择结构', async () => {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  const calls = [];
  globalThis.localStorage = { getItem: () => null };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options.method, body: JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  try {
    await api.sendMessage('project_alpha', '合成普通消息', 'default');
    await api.respondPermission('project_alpha', 'request_demo', 'allow', '', { 合成问题: '合成答案' });
    const details = { 合成问题: { selected_options: [], custom_text: '合成答案' } };
    await api.respondPermission('project_alpha', 'request_demo', 'allow', '', { 合成问题: '合成答案' }, details);
    assert.deepEqual(calls, [
      { path: '/api/projects/project_alpha/session/messages', method: 'POST',
        body: { text: '合成普通消息', conversation_id: 'default' } },
      { path: '/api/projects/project_alpha/session/permissions/request_demo', method: 'POST',
        body: { behavior: 'allow', message: '', answers: { 合成问题: '合成答案' }, answer_details: null } },
      { path: '/api/projects/project_alpha/session/permissions/request_demo', method: 'POST',
        body: { behavior: 'allow', message: '', answers: { 合成问题: '合成答案' }, answer_details: details } },
    ]);
  } finally {
    Object.assign(globalThis, originals);
  }
});
