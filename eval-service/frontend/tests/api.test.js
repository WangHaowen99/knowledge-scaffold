import { test } from 'node:test';
import assert from 'node:assert/strict';
import { api, allPages, randomId, requestId, REFRESH, TOKEN } from '../src/api.js';
function storage() {
  const values = new Map();
  return {
    getItem: (k) => values.get(k) || null,
    setItem: (k, v) => values.set(k, v),
    removeItem: (k) => values.delete(k),
  };
}
test('request IDs survive retry and are separated by subject', () => {
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
  sessionStorage.setItem('evaluation-subject', 'synthetic-user-a');
  const first = requestId('run', 'synthetic-input');
  assert.equal(first.id, requestId('run', 'synthetic-input').id);
  sessionStorage.setItem('evaluation-subject', 'synthetic-user-b');
  assert.notEqual(first.id, requestId('run', 'synthetic-input').id);
});
test('IDs are generated on plain HTTP pages, where crypto.randomUUID is missing', (t) => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  Object.defineProperty(globalThis, 'crypto', {
    configurable: true,
    value: { getRandomValues: (bytes) => original.get.call(globalThis).getRandomValues(bytes) },
  });
  t.after(() => Object.defineProperty(globalThis, 'crypto', original));
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const first = randomId();
  assert.match(first, uuid);
  assert.notEqual(first, randomId());
  globalThis.sessionStorage = storage();
  assert.match(requestId('run', 'synthetic-input').id, uuid);
});
test('late result from previous login is rejected', async () => {
  globalThis.localStorage = storage();
  localStorage.setItem(TOKEN, 'synthetic-old');
  globalThis.fetch = async () => {
    localStorage.setItem(TOKEN, 'synthetic-new');
    return { ok: true, json: async () => ({ items: [] }) };
  };
  await assert.rejects(api('/packages'), /身份已变化/);
});
test('a rejected request is never replayed after an account switch', async () => {
  globalThis.localStorage = storage();
  localStorage.setItem(TOKEN, 'synthetic-old');
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    localStorage.setItem(TOKEN, 'synthetic-new');
    return { status: 401, ok: false, json: async () => ({}) };
  };
  await assert.rejects(api('/runs', { request_id: 'synthetic-request' }), /身份已变化/);
  assert.equal(calls, 1);
});
test('paginated task loading includes activity beyond the first page and rejects loops', async () => {
  globalThis.localStorage = storage();
  let calls = 0;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () =>
      ++calls === 1
        ? { items: [{ id: 'history_demo' }], next_offset: 20 }
        : { items: [{ id: 'active_demo', status: 'running' }], next_offset: null },
  });
  assert.deepEqual(
    (await allPages('/runs')).map((r) => r.id),
    ['history_demo', 'active_demo'],
  );
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ items: [], next_offset: 0 }) });
  await assert.rejects(allPages('/runs'), /分页响应重复/);
});
test('only work that outlives the request hands over the refresh token', async () => {
  globalThis.localStorage = storage();
  localStorage.setItem(TOKEN, 'synthetic-access');
  localStorage.setItem(REFRESH, 'synthetic-refresh');
  const sent = [];
  globalThis.fetch = async (url, init) => {
    sent.push([url.replace('/evaluation/api', ''), init.method, init.headers['X-Refresh-Token'] || '']);
    return { ok: true, json: async () => ({}) };
  };
  await api('/runs', { request_id: 'synthetic-request' });
  await api('/runs/run_demo/resume', {});
  await api('/runs/run_demo/cancel', {});
  // A changed verdict and a regeneration queue suggestions that read the package after the request.
  await api('/runs/run_demo/results/1/override', { verdict: 'pass' });
  await api('/runs/run_demo/artifacts/regenerate', {});
  await api('/runs/run_demo/notifications/retry', {});
  await api('/runs/run_demo');
  assert.deepEqual(sent, [
    ['/runs', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/resume', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/cancel', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/results/1/override', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/artifacts/regenerate', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/notifications/retry', 'POST', ''],
    ['/runs/run_demo', 'GET', ''],
  ]);
});
