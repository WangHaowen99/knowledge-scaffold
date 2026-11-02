// 启动评测请求体（lib/api.js）：带上 refresh token，供 modeling 为长评测轮续期凭据。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { api, evalRunPayload } from '../src/lib/api.js';

const originalFetch = globalThis.fetch;
const originalStorage = globalThis.localStorage;

function stubStorage(values) {
  globalThis.localStorage = {
    getItem: (key) => (key in values ? values[key] : null),
    setItem: () => {},
    removeItem: () => {},
  };
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.localStorage = originalStorage;
});

test('有 refresh token 时随评测启动请求一起提交', async () => {
  stubStorage({ 'onto-access-token': 'synthetic-access', 'onto-refresh-token': 'synthetic-refresh' });
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    return { ok: true, status: 200, json: async () => ({ started: true }) };
  };

  await api.evalRun('p1', { cases: [{ question: 'Q1', groundTruth: 'G1' }], evalSetId: 's1' });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/projects/p1/eval/run');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-access');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    cases: [{ question: 'Q1', groundTruth: 'G1' }],
    data_source: { mode: 'none' },
    data_source_id: '',
    eval_set_id: 's1',
    allow_unbound: false,
    refresh_token: 'synthetic-refresh',
  });
});

test('无鉴权部署没有 refresh token：请求体不带该字段', () => {
  const payload = evalRunPayload({ cases: [], allowUnbound: true }, '');
  assert.equal('refresh_token' in payload, false);
  assert.equal(payload.allow_unbound, true);
});

test('评测请求携带选定模型和受限并发数', () => {
  const payload = evalRunPayload({
    cases: [],
    modelProfileId: 'global:synthetic-fast',
    caseConcurrency: 12,
  }, '');
  assert.equal(payload.model_profile_id, 'global:synthetic-fast');
  assert.equal(payload.case_concurrency, 12);
});
