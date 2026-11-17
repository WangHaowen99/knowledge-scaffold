import { test } from 'node:test';
import assert from 'node:assert/strict';
import { api, allPages, clearLogin, randomId, requestId, REFRESH, SESSION, saveLogin, TOKEN, tokenSubject } from '../src/api.js';
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
  saveLogin({ access_token: 'synthetic-old', refresh_token: 'synthetic-old-refresh' });
  globalThis.fetch = async () => {
    saveLogin({ access_token: 'synthetic-new', refresh_token: 'synthetic-new-refresh' });
    return { ok: true, json: async () => ({ items: [] }) };
  };
  await assert.rejects(api('/packages'), /身份已变化/);
});

test('an aborted API request never starts a network operation', async () => {
  const controller = new AbortController();
  controller.abort();
  globalThis.fetch = async () => assert.fail('Cancelled request must not use the network');
  await assert.rejects(api('/data-source-tests', undefined, 'GET', true, 'json', undefined,
    { signal: controller.signal }), { name: 'AbortError' });
});

test('API forwards an abort signal to fetch and rejects a cancelled response body', async () => {
  globalThis.localStorage = storage();
  const controller = new AbortController();
  globalThis.fetch = async (url, init) => {
    assert.equal(init.signal, controller.signal);
    return { ok: true, json: async () => {
      controller.abort();
      throw controller.signal.reason;
    } };
  };
  await assert.rejects(api('/data-source-tests', undefined, 'GET', true, 'json', undefined,
    { signal: controller.signal }), { name: 'AbortError' });
});

test('API keeps the abort signal after refreshing authentication', async () => {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-access-old', refresh_token: 'synthetic-refresh-old' });
  const controller = new AbortController();
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} });
  globalThis.fetch = async (url, init) => {
    if (url.endsWith('/auth/refresh')) return { ok: true, json: async () => ({
      access_token: 'synthetic-access-new', refresh_token: 'synthetic-refresh-new',
    }) };
    assert.equal(init.signal, controller.signal);
    return init.headers.Authorization === 'Bearer synthetic-access-old'
      ? { status: 401, ok: false, json: async () => ({}) }
      : { status: 200, ok: true, json: async () => ({ id: 'job_demo', status: 'running' }) };
  };
  assert.equal((await api('/data-source-tests', undefined, 'GET', true, 'json', undefined,
    { signal: controller.signal })).id, 'job_demo');
});
test('a rejected request is never replayed after an account switch', async () => {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-old', refresh_token: 'synthetic-old-refresh' });
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    saveLogin({ access_token: 'synthetic-new', refresh_token: 'synthetic-new-refresh' });
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
  // Delivering feedback waits for its suggestions; listing feedback drafts the suggestions of feedback waiting for an owner.
  await api('/runs/run_demo/notifications/retry', {});
  await api('/package-tasks?origin=consumer_feedback');
  await api('/package-tasks');
  await api('/runs/run_demo');
  assert.deepEqual(sent, [
    ['/runs', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/resume', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/cancel', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/results/1/override', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/artifacts/regenerate', 'POST', 'synthetic-refresh'],
    ['/runs/run_demo/notifications/retry', 'POST', 'synthetic-refresh'],
    ['/package-tasks?origin=consumer_feedback', 'GET', 'synthetic-refresh'],
    ['/package-tasks', 'GET', ''],
    ['/runs/run_demo', 'GET', ''],
  ]);
});

const reply = (status, body = {}) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

async function withRefreshScenario(locked, refreshReply = () => reply(200, {
  access_token: 'synthetic-access-new', refresh_token: 'synthetic-refresh-new',
})) {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-access-old', refresh_token: 'synthetic-refresh-old' });
  let queue = Promise.resolve();
  const locks = locked ? { request: (_key, action) => {
    const next = queue.then(action);
    queue = next.catch(() => {});
    return next;
  } } : undefined;
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { locks } });
  const events = [];
  globalThis.CustomEvent = class { constructor(type, options) { this.type = type; this.detail = options.detail; } };
  globalThis.window = { dispatchEvent: (event) => events.push(event) };
  let refreshes = 0, originalRequests = 0, retries = 0;
  let releaseOriginal;
  const originalBarrier = new Promise((resolve) => { releaseOriginal = resolve; });
  globalThis.fetch = async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshes += 1;
      return refreshReply();
    }
    if (init.headers.Authorization === 'Bearer synthetic-access-old') {
      originalRequests += 1;
      if (originalRequests === 2) releaseOriginal();
      await originalBarrier;
      return reply(401);
    }
    retries += 1;
    return reply(200, { ok: true });
  };
  const results = await Promise.allSettled([api('/runs/synthetic-run'), api('/runs/synthetic-run/results')]);
  return { results, events, refreshes, retries };
}

for (const locked of [false, true]) {
  test(`simultaneous 401 responses share a successful refresh (Web Locks ${locked})`, async () => {
    const { results, events, refreshes, retries } = await withRefreshScenario(locked);
    assert.deepEqual(results.map((result) => result.status), ['fulfilled', 'fulfilled']);
    assert.equal(refreshes, 1);
    assert.equal(retries, 2);
    assert.equal(events.length, 0);
    assert.equal(localStorage.getItem(TOKEN), 'synthetic-access-new');
  });
}

test('rejected refresh requests require login only when credentials are invalid', async () => {
  const invalid = await withRefreshScenario(false, () => reply(400, { code: 'refresh_invalid' }));
  assert.deepEqual(invalid.results.map((result) => result.status), ['rejected', 'rejected']);
  assert.equal(invalid.events.length, 2);
  assert.equal(invalid.events[0].detail.session, localStorage.getItem(SESSION));
  const temporary = await withRefreshScenario(false, () => reply(503, { code: 'dependency_unavailable' }));
  assert.deepEqual(temporary.results.map((result) => result.status), ['rejected', 'rejected']);
  assert.equal(temporary.events.length, 0);
  assert.equal(localStorage.getItem(TOKEN), 'synthetic-access-old');
});

test('logout during refresh cannot restore credentials or demand login again', async () => {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-access-old', refresh_token: 'synthetic-refresh-old' });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} });
  let finishRefresh;
  const refreshBarrier = new Promise((resolve) => { finishRefresh = resolve; });
  let eventCount = 0;
  globalThis.window = { dispatchEvent: () => { eventCount += 1; } };
  globalThis.fetch = async (url) => url.endsWith('/auth/refresh')
    ? refreshBarrier
    : reply(401);
  const request = api('/runs/synthetic-run');
  await new Promise((resolve) => setImmediate(resolve));
  clearLogin();
  finishRefresh(reply(200, { access_token: 'synthetic-access-new', refresh_token: 'synthetic-refresh-new' }));
  await assert.rejects(request, /身份已变化/);
  assert.equal(localStorage.getItem(TOKEN), null);
  assert.equal(eventCount, 0);
});

test('account switch during refresh preserves the new account and rejects the old request', async () => {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-access-old', refresh_token: 'synthetic-refresh-old' });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} });
  let finishRefresh;
  const refreshBarrier = new Promise((resolve) => { finishRefresh = resolve; });
  globalThis.window = { dispatchEvent: () => assert.fail('old request demanded login') };
  globalThis.fetch = async (url) => url.endsWith('/auth/refresh') ? refreshBarrier : reply(401);
  const request = api('/runs/synthetic-run');
  await new Promise((resolve) => setImmediate(resolve));
  saveLogin({ access_token: 'synthetic-other-access', refresh_token: 'synthetic-other-refresh' });
  finishRefresh(reply(200, { access_token: 'synthetic-access-new', refresh_token: 'synthetic-refresh-new' }));
  await assert.rejects(request, /身份已变化/);
  assert.equal(localStorage.getItem(TOKEN), 'synthetic-other-access');
  assert.equal(localStorage.getItem(REFRESH), 'synthetic-other-refresh');
});

test('a same-origin account change without the session marker still rejects the old response', async () => {
  const jwt = (subject) => `header.${Buffer.from(JSON.stringify({ sub: subject })).toString('base64url')}.signature`;
  assert.equal(tokenSubject(jwt('synthetic-user-a')), 'synthetic-user-a');
  globalThis.localStorage = storage();
  localStorage.setItem(TOKEN, jwt('synthetic-user-a'));
  localStorage.setItem(REFRESH, 'synthetic-refresh-a');
  globalThis.fetch = async () => {
    localStorage.setItem(TOKEN, jwt('synthetic-user-b'));
    localStorage.setItem(REFRESH, 'synthetic-refresh-b');
    return reply(200, { ok: true });
  };
  await assert.rejects(api('/runs/synthetic-run'), /身份已变化/);
  assert.equal(localStorage.getItem(TOKEN), jwt('synthetic-user-b'));
});

test('another tab renewing the same login lets a waiting request retry with its token', async () => {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-access-old', refresh_token: 'synthetic-refresh-old' });
  const session = localStorage.getItem(SESSION);
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
    locks: { request: async (_key, action) => {
      localStorage.setItem(TOKEN, 'synthetic-access-new');
      localStorage.setItem(REFRESH, 'synthetic-refresh-new');
      return action();
    } },
  } });
  globalThis.window = { dispatchEvent: () => assert.fail('renewal demanded login') };
  let refreshes = 0;
  globalThis.fetch = async (url, init) => {
    if (url.endsWith('/auth/refresh')) refreshes += 1;
    return init.headers.Authorization === 'Bearer synthetic-access-old'
      ? reply(401)
      : reply(200, { ok: true });
  };
  assert.deepEqual(await api('/runs/synthetic-run'), { ok: true });
  assert.equal(refreshes, 0);
  assert.equal(localStorage.getItem(SESSION), session);
});

test('the same detail requests recover across two access token expirations', async () => {
  globalThis.localStorage = storage();
  saveLogin({ access_token: 'synthetic-access-0', refresh_token: 'synthetic-refresh-0' });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} });
  globalThis.window = { dispatchEvent: () => assert.fail('renewal demanded login') };
  let generation = 0, refreshes = 0;
  globalThis.fetch = async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshes += 1;
      return reply(200, {
        access_token: `synthetic-access-${generation + 1}`,
        refresh_token: `synthetic-refresh-${generation + 1}`,
      });
    }
    return init.headers.Authorization === `Bearer synthetic-access-${generation}`
      ? reply(401)
      : reply(200, { ok: true });
  };
  for (generation = 0; generation < 2; generation += 1) {
    const responses = await Promise.all([api('/runs/synthetic-run'), api('/runs/synthetic-run/results')]);
    assert.deepEqual(responses, [{ ok: true }, { ok: true }]);
  }
  assert.equal(refreshes, 2);
  assert.equal(localStorage.getItem(TOKEN), 'synthetic-access-2');
});
