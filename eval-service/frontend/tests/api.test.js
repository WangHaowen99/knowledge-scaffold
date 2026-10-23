import { test } from 'node:test';
import assert from 'node:assert/strict';
import { api, requestId, TOKEN } from '../src/api.js';
function storage() { const values = new Map(); return { getItem: (k) => values.get(k) || null, setItem: (k, v) => values.set(k, v), removeItem: (k) => values.delete(k) }; }
test('request IDs survive retry and are separated by subject', () => {
  globalThis.localStorage = storage(); globalThis.sessionStorage = storage();
  sessionStorage.setItem('evaluation-subject', 'synthetic-user-a');
  const first = requestId('run', 'synthetic-input');
  assert.equal(first.id, requestId('run', 'synthetic-input').id);
  sessionStorage.setItem('evaluation-subject', 'synthetic-user-b');
  assert.notEqual(first.id, requestId('run', 'synthetic-input').id);
});
test('late result from previous login is rejected', async () => {
  globalThis.localStorage = storage(); localStorage.setItem(TOKEN, 'synthetic-old');
  globalThis.fetch = async () => { localStorage.setItem(TOKEN, 'synthetic-new'); return { ok: true, json: async () => ({ items: [] }) }; };
  await assert.rejects(api('/packages'), /身份已变化/);
});
test('a rejected request is never replayed after an account switch', async () => {
  globalThis.localStorage = storage(); localStorage.setItem(TOKEN, 'synthetic-old');
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; localStorage.setItem(TOKEN, 'synthetic-new'); return { status: 401, ok: false, json: async () => ({}) }; };
  await assert.rejects(api('/runs', { request_id: 'synthetic-request' }), /身份已变化/);
  assert.equal(calls, 1);
});
