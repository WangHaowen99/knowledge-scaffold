// Synthetic history fixtures; no real project data.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { createClient, createTokenStore } from '../src/api/client.js';
import { createApi } from '../src/api/endpoints.js';
import { EMPTY_HISTORY, appendHistoryPage, historyEntryKey, versionTitle } from '../src/lib/versionHistory.js';

const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
beforeEach(() => { globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }; });
afterEach(() => Object.assign(globalThis, originals));

const iteration = (version) => ({ kind: 'iteration', commit: `commit-${version}`, version, time: version * 100 });
const release = (version) => ({ kind: 'release', release_id: `release-${version}`, version, time: 1 });

test('entries are titled and keyed by kind', () => {
  assert.equal(versionTitle('iteration', 3), '迭代 3');
  assert.equal(versionTitle('release', '0.1.1'), '发布版本 0.1.1');
  assert.equal(historyEntryKey(iteration(3)), 'iteration:commit-3');
  assert.equal(historyEntryKey(release('0.1.1')), 'release:release-0.1.1');
});

test('pages accumulate in server order and keep the first release error', () => {
  const first = appendHistoryPage(EMPTY_HISTORY, {
    items: [iteration(3), release('0.1.1')], next_cursor: '1:1', release_error: '',
  });
  assert.deepEqual(first.items.map(historyEntryKey), ['iteration:commit-3', 'release:release-0.1.1']);
  assert.equal(first.nextCursor, '1:1');
  const failed = appendHistoryPage(first, { items: [iteration(2)], next_cursor: '2:off', release_error: 'Hub 不可用' });
  const last = appendHistoryPage(failed, { items: [iteration(1)], next_cursor: null, release_error: '' });
  assert.deepEqual(last.items.map((entry) => entry.version), [3, '0.1.1', 2, 1]);
  assert.equal(last.nextCursor, null);
  assert.equal(last.releaseError, 'Hub 不可用');
});

test('history requests pass the cursor back verbatim', async () => {
  const paths = [];
  // 本分支的接口客户端（/api/v1，空参数不进查询串）
  const api = createApi(createClient({
    tokens: createTokenStore(globalThis.localStorage),
    fetchImpl: async (path) => {
      paths.push(String(path));
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  }));
  await api.history('project-alpha');
  await api.history('project-alpha', { cursor: '5:off' });
  assert.deepEqual(paths, [
    '/api/v1/projects/project-alpha/history?limit=50',
    '/api/v1/projects/project-alpha/history?limit=50&cursor=5%3Aoff',
  ]);
});
