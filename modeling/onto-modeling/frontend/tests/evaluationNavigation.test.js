import { test } from 'node:test';
import assert from 'node:assert/strict';
import { evaluationUrl, openEvaluation } from '../src/lib/evaluation.js';
const release = { hub_package_id: 'package_demo', release_id: 'release_demo', package_digest: 'a'.repeat(64), project_id: 'project_alpha', token: 'synthetic-invalid' };
// All references and login material in these tests are synthetic.
test('evaluation navigation carries only Hub references', () => {
  const url = new URL(evaluationUrl(release), 'https://example.com');
  assert.deepEqual([...url.searchParams.keys()], ['hub_package_id', 'release_id', 'package_digest']);
  assert.equal(url.pathname, '/evaluation/');
});
test('unconfigured evaluation cannot fall back to modeling', async () => {
  let navigated = false;
  await assert.rejects(openEvaluation(release, { fetchImpl: async () => ({ ok: true, json: async () => ({}) }), navigate: () => { navigated = true; } }), /未配置/);
  assert.equal(navigated, false);
});

test('healthy evaluation navigates using the same-origin proxy', async () => {
  let destination;
  await openEvaluation(release, {
    fetchImpl: async (url, options) => {
      assert.equal(url, '/evaluation/api/health');
      assert.equal(options.cache, 'no-store');
      return { ok: true, json: async () => ({ service: 'evaluation', status: 'ok' }) };
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
      fetchImpl, navigate: () => { navigated = true; },
    }), expected);
    assert.equal(navigated, false);
  });
}
