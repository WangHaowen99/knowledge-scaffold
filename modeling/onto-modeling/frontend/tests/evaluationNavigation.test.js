import { test } from 'node:test';
import assert from 'node:assert/strict';
import { HANDOFF, READY, evaluationUrl, openEvaluation } from '../src/lib/evaluation.js';
const release = { hub_package_id: 'package_demo', release_id: 'release_demo', package_digest: 'a'.repeat(64), project_id: 'project_alpha', token: 'synthetic-invalid' };
// All references, origins and login material in these tests are synthetic.
const here = new URL('http://modeling.example.com:5100/');
const sameOrigin = { entry: { url: '/evaluation/' }, here, messages: {} };
const healthy = async () => ({ ok: true, json: async () => ({ service: 'evaluation', status: 'ok' }) });

test('evaluation navigation carries only Hub references', () => {
  const url = new URL(evaluationUrl(release), 'https://example.com');
  assert.deepEqual([...url.searchParams.keys()], ['hub_package_id', 'release_id', 'package_digest']);
  assert.equal(url.pathname, '/evaluation/');
});
test('unconfigured evaluation cannot fall back to modeling', async () => {
  let navigated = false;
  await assert.rejects(openEvaluation(release, { ...sameOrigin, fetchImpl: async () => ({ ok: true, json: async () => ({}) }), navigate: () => { navigated = true; } }), /未配置/);
  assert.equal(navigated, false);
});

test('healthy evaluation navigates using the same-origin proxy', async () => {
  let destination;
  await openEvaluation(release, {
    ...sameOrigin,
    fetchImpl: async (url, options) => {
      assert.equal(url, '/evaluation/api/health');
      assert.equal(options.cache, 'no-store');
      return healthy();
    },
    navigate: (url) => { destination = url; },
  });
  assert.equal(destination, evaluationUrl(release));
});

for (const [name, fetchImpl, expected] of [
  ['missing route', async () => ({ ok: false, status: 404 }), /路由未配置/],
  ['unavailable upstream', async () => ({ ok: false, status: 502 }), /HTTP 502/],
  ['network failure', async () => { throw new Error('synthetic network failure'); }, /无法连接/],
  ['SPA fallback', async () => ({ ok: true, json: async () => { throw new SyntaxError('synthetic HTML response'); } }), /响应不匹配/],
]) {
  test(`${name} prevents navigation and explains the failure`, async () => {
    let navigated = false;
    await assert.rejects(openEvaluation(release, {
      ...sameOrigin, fetchImpl, navigate: () => { navigated = true; },
    }), expected);
    assert.equal(navigated, false);
  });
}

function crossOriginHarness() {
  const listeners = new Set();
  const messages = { addEventListener: (type, fn) => listeners.add(fn), removeEventListener: (type, fn) => listeners.delete(fn) };
  const popup = { location: { href: 'about:blank' }, closed: false, sent: [], close() { this.closed = true; }, postMessage(message, origin) { this.sent.push({ message, origin }); } };
  return { listeners, messages, popup, dispatch: (event) => [...listeners].forEach((fn) => fn(event)) };
}
const evaluationOrigin = 'http://eval.example.com:8090';
const crossEntry = { url: `${evaluationOrigin}/evaluation/` };
const tokens = () => ({ access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' });

test('cross-origin evaluation opens a window and hands over the login only to its origin', async () => {
  const { messages, popup, dispatch, listeners } = crossOriginHarness();
  let checked;
  await openEvaluation(release, {
    entry: crossEntry, here, messages, tokens, openWindow: () => popup,
    fetchImpl: async (url) => { checked = url; return healthy(); },
    navigate: () => assert.fail('the modeling page must stay open'),
  });
  assert.equal(checked, `${evaluationOrigin}/evaluation/api/health`);
  const destination = new URL(popup.location.href);
  assert.equal(destination.origin, evaluationOrigin);
  assert.deepEqual([...destination.searchParams.keys()], ['hub_package_id', 'release_id', 'package_digest']);
  assert.equal(popup.location.href.includes('synthetic-access'), false);

  dispatch({ source: {}, origin: evaluationOrigin, data: { type: READY } });
  dispatch({ source: popup, origin: 'https://attacker.example.net', data: { type: READY } });
  dispatch({ source: popup, origin: evaluationOrigin, data: { type: 'other' } });
  assert.deepEqual(popup.sent, []);
  dispatch({ source: popup, origin: evaluationOrigin, data: { type: READY } });
  assert.deepEqual(popup.sent, [{ message: { type: HANDOFF, access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' }, origin: evaluationOrigin }]);
  assert.equal(listeners.size, 0);
});

test('a failed cross-origin health check closes the window without handing over the login', async () => {
  const { messages, popup, listeners } = crossOriginHarness();
  await assert.rejects(openEvaluation(release, {
    entry: crossEntry, here, messages, tokens, openWindow: () => popup,
    fetchImpl: async () => { throw new Error('synthetic network failure'); },
  }), /无法连接评测服务/);
  assert.equal(popup.closed, true);
  assert.equal(listeners.size, 0);
});

test('a blocked popup is reported instead of navigating', async () => {
  const { messages } = crossOriginHarness();
  await assert.rejects(openEvaluation(release, {
    entry: crossEntry, here, messages, tokens, openWindow: () => null, fetchImpl: healthy,
  }), /拦截/);
});

test('an absolute entry on the modeling origin keeps same-origin navigation', async () => {
  let destination;
  await openEvaluation(release, {
    entry: { url: 'http://modeling.example.com:5100/evaluation/' }, here, messages: {}, fetchImpl: healthy,
    openWindow: () => assert.fail('no window for same-origin evaluation'),
    navigate: (url) => { destination = url; },
  });
  assert.equal(new URL(destination).pathname, '/evaluation/');
});

test('without a deployed entry evaluation opens on the modeling host at the default port', async () => {
  const { messages, popup } = crossOriginHarness();
  let checked;
  await openEvaluation(release, {
    entry: { url: '' }, here, messages, tokens, openWindow: () => popup,
    fetchImpl: async (url) => { checked = url; return healthy(); },
  });
  assert.equal(checked, 'http://modeling.example.com:8090/evaluation/api/health');
  assert.equal(new URL(popup.location.href).origin, 'http://modeling.example.com:8090');
});
