// Synthetic API fixtures; no real project data or credentials.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, describe, test } from 'node:test';
import { api } from '../src/lib/api.js';

// Suite-scoped hooks keep these stubs local when all test files share one process.
describe('package release API', () => {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage, document: globalThis.document };
  let values;
  beforeEach(() => {
    values = new Map();
    globalThis.localStorage = {
      getItem: (k) => values.get(k) || null,
      setItem: (k, v) => values.set(k, v),
      removeItem: (k) => values.delete(k),
    };
    globalThis.document = { createElement: () => ({ click() {}, remove() {} }), body: { appendChild() {} } };
  });
  afterEach(() => Object.assign(globalThis, originals));
  const response = (body, status = 200) => ({
    ok: status < 400, status, json: async () => body,
    headers: new Headers(), blob: async () => new Blob(['synthetic package']),
  });

  test('publication saves a release without downloading', async () => {
    const calls = [];
    globalThis.fetch = async (path, options) => {
      calls.push({ path, options });
      return response({ release_id: 'release-a', version: '0.1.1' });
    };
    await api.publishPackage('project-alpha');
    assert.equal(calls[0].options.method, 'POST');
    assert.ok(JSON.parse(calls[0].options.body).request_id);
    assert.equal(calls.length, 1);
    assert.equal(values.size, 0);
  });

  test('a lost publish response retains the same request across retries', async () => {
    const ids = [];
    globalThis.fetch = async (_, options) => {
      ids.push(JSON.parse(options.body).request_id);
      throw new Error('synthetic network failure');
    };
    await assert.rejects(api.publishPackage('project-alpha'));
    await assert.rejects(api.publishPackage('project-alpha'));
    assert.equal(ids[0], ids[1]);
  });

  test('history download never calls the publication endpoint', async () => {
    const calls = [];
    globalThis.fetch = async (path) => { calls.push(path); return response({}); };
    await api.downloadRelease('project-alpha', 'release-a');
    assert.deepEqual(calls, ['/api/projects/project-alpha/package/releases/release-a/download']);
  });
});
