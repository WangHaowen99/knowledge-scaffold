// All identities and package references in these tests are synthetic.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { api } from '../src/lib/api.js';

const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
let values;
beforeEach(() => {
  values = new Map();
  globalThis.localStorage = { getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
});
afterEach(() => Object.assign(globalThis, originals));
const response = (body) => ({ ok: true, status: 200, json: async () => body });

test('Hub import fixes release and digest and recovers original request after lost response', async () => {
  const calls = [];
  const body = { name: 'project_alpha', ontology_id: 'package_demo', release_id: 'release_demo', package_digest: 'a'.repeat(64) };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, body: JSON.parse(options.body) });
    if (calls.length === 1) throw new Error('Synthetic timeout');
    return response({ id: 'project-alpha' });
  };
  await assert.rejects(api.importHubPackage(body));
  await api.importHubPackage(body);
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[1].path, '/api/hub/imports');
  assert.equal(calls[1].body.release_id, body.release_id);
  assert.equal(values.size, 0);
});

test('Hub package browsing does not require a project or workspace identity', async () => {
  const calls = [];
  globalThis.fetch = async (path) => { calls.push(path); return response({ items: [], total: 0 }); };
  await api.hubPackages(20);
  await api.hubPackageVersions('package_demo', 40);
  assert.deepEqual(calls, ['/api/hub/packages?offset=20&limit=20', '/api/hub/packages/package_demo/releases?offset=40&limit=20']);
});
