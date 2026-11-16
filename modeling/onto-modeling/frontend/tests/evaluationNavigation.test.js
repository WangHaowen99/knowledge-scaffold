// 独立评测入口（ADR-0092，从 develop 的 lib/evaluation.js 移植）：地址只带 Hub 引用，登录只交给评测页面的来源
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { HANDOFF, READY, evaluationUrl, openEvaluation } from '../src/pages/evaluation/evaluationEntry.js';
const release = { ontology_id: 'package_demo', release_id: 'release_demo', package_digest: 'a'.repeat(64), project_id: 'project_alpha', token: 'synthetic-invalid' };
// All references, origins and login material in these tests are synthetic.
const here = new URL('http://modeling.example.com:5100/');
const healthy = async () => ({ ok: true, json: async () => ({ service: 'evaluation', status: 'ok' }) });

test('evaluation navigation carries only Hub references', () => {
  const url = new URL(evaluationUrl(release, 'https://eval.example.com/evaluation/'));
  assert.deepEqual([...url.searchParams.keys()], ['ontology_id', 'release_id', 'package_digest']);
  assert.equal(url.pathname, '/evaluation/');
});

test('solver validation navigation preselects the run type and nothing else', () => {
  const url = new URL(evaluationUrl(release, 'https://eval.example.com/evaluation/', 'solver'));
  assert.deepEqual([...url.searchParams.keys()], ['ontology_id', 'release_id', 'package_digest', 'run_type']);
  assert.equal(url.searchParams.get('run_type'), 'solver');
  assert.equal(new URL(evaluationUrl(release, 'https://eval.example.com/evaluation/', 'other')).searchParams.has('run_type'), false);
});
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
  assert.deepEqual([...destination.searchParams.keys()], ['ontology_id', 'release_id', 'package_digest']);
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

for (const url of ['/evaluation/', 'http://modeling.example.com:5100/evaluation/', 'javascript:alert(1)']) {
  test(`reject unsupported entry ${url} before opening a window`, async () => {
    await assert.rejects(openEvaluation(release, {
      entry: { url }, here, messages: {}, openWindow: () => assert.fail('must not open'),
    }), /独立|http/);
  });
}

test('HTTPS modeling rejects HTTP evaluation', async () => {
  await assert.rejects(openEvaluation(release, {
    entry: crossEntry, here: new URL('https://modeling.example.com'), messages: {},
    openWindow: () => assert.fail('must not open'),
  }), /HTTPS/);
});

for (const [name, fetchImpl, expected] of [
  ['missing route', async () => ({ ok: false, status: 404 }), /HTTP 404/],
  ['unavailable upstream', async () => ({ ok: false, status: 502 }), /HTTP 502/],
  ['SPA fallback', async () => ({ ok: true, json: async () => { throw new SyntaxError('synthetic HTML'); } }), /响应不匹配/],
]) {
  test(`${name} closes evaluation window`, async () => {
    const { messages, popup, listeners } = crossOriginHarness();
    await assert.rejects(openEvaluation(release, {
      entry: crossEntry, here, messages, openWindow: () => popup, fetchImpl,
    }), expected);
    assert.equal(popup.closed, true);
    assert.equal(listeners.size, 0);
  });
}

test('without a deployed entry evaluation opens on the modeling host at the default port', async () => {
  const { messages, popup, dispatch } = crossOriginHarness();
  let checked;
  await openEvaluation(release, {
    entry: { url: '' }, here, messages, tokens, openWindow: () => popup,
    fetchImpl: async (url) => { checked = url; return healthy(); },
  });
  assert.equal(checked, 'http://modeling.example.com:5180/evaluation/api/health');
  assert.equal(new URL(popup.location.href).origin, 'http://modeling.example.com:5180');
  dispatch({ source: popup, origin: 'http://modeling.example.com:5180', data: { type: READY } });
});

test('without a prefetched entry the click loads it once and reuses it', async () => {
  const { messages, popup } = crossOriginHarness();
  let loads = 0;
  const { prefetchEvaluationEntry } = await import('../src/pages/evaluation/evaluationEntry.js');
  await prefetchEvaluationEntry(async () => { loads += 1; return crossEntry; });
  await openEvaluation(release, { here, messages, tokens, openWindow: () => popup, fetchImpl: healthy, handoffTimeoutMs: 10 });
  assert.equal(loads, 1);
  assert.equal(new URL(popup.location.href).origin, evaluationOrigin);
});
