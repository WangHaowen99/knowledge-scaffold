import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { api } from '../src/lib/api.js';
import { buildQuestionSubmission } from '../src/lib/questionAnswers.js';

const originalFetch = globalThis.fetch;
const originalStorage = globalThis.localStorage;
afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.localStorage = originalStorage;
});

test('同一答复请求同时发送模型答案和恢复勾选所需的结构化内容', async () => {
  // 数据全部人工合成；fetch 替身只捕获请求，不访问外部环境。
  globalThis.localStorage = { getItem: () => null };
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  const { answers, answerDetails } = buildQuestionSubmission([{
    question: '合成问题', multiSelect: true, options: [{ label: '方案一' }, { label: '方案二' }],
  }], { 0: ['方案一', '方案二'] }, { 0: '人工合成补充' });
  await api.respondPermission('project_alpha', 'request_demo', 'allow', '', answers, answerDetails);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/projects/project_alpha/session/permissions/request_demo');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    behavior: 'allow', message: '', answers,
    answer_details: { 合成问题: { selected_options: ['方案一', '方案二'], custom_text: '人工合成补充' } },
  });
});
