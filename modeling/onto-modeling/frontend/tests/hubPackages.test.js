// 知识包 Hub 的接口（ADR-0098，从 develop 的 hubPackages 测试移植）：发布与导入的 request_id 在拿到结果前重试不变，
// 不依赖 randomUUID；浏览 Hub 包不带项目身份。包身份、名称全部为合成数据。
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, test } from 'node:test';
import { createApi } from '../src/api/endpoints.js';
import { ApiError } from '../src/api/errors.js';

describe('知识包 Hub 接口', () => {
  const originals = { localStorage: globalThis.localStorage };
  let values;
  beforeEach(() => {
    values = new Map();
    globalThis.localStorage = {
      getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key),
    };
  });
  afterEach(() => Object.assign(globalThis, originals));

  const clientOf = (post, get = async () => ({})) => createApi({ post, get });

  for (const operation of ['import', 'publish']) {
    test(`${operation}：响应丢了重试用同一个 request_id，只生成一次，成功后清掉`, async () => {
      const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
      let generations = 0;
      Object.defineProperty(globalThis, 'crypto', {
        configurable: true,
        value: { getRandomValues(bytes) { generations += 1; return webcrypto.getRandomValues(bytes); } },
      });
      try {
        const ids = [];
        const api = clientOf(async (_path, body) => {
          ids.push(body.request_id);
          if (ids.length === 1) throw new Error('Synthetic timeout');
          return { id: 'project_alpha', release_id: 'release_demo' };
        });
        const send = operation === 'publish'
          ? () => api.publishRelease('project_alpha', { commit: 'c'.repeat(40) })
          : () => api.importHubPackage({ name: 'project_alpha', ontology_id: 'package_demo', release_id: 'release_demo', package_digest: 'a'.repeat(64) });
        await assert.rejects(send(), /Synthetic timeout/);
        assert.equal(values.size, 1);
        await send();
        assert.match(ids[0], /^[a-f0-9]{32}$/);
        assert.equal(ids[0], ids[1]);
        assert.equal(generations, 1);
        assert.equal(values.size, 0);
      } finally {
        if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor);
        else delete globalThis.crypto;
      }
    });
  }

  test('服务端给了明确结论（4xx）换新键；不同选项是不同的意图', async () => {
    const ids = [];
    const api = clientOf(async (_path, body) => {
      ids.push(body.request_id);
      throw new ApiError({ status: 409, code: 'solver_validation_acknowledgement_required' });
    });
    await assert.rejects(api.publishRelease('project_alpha', { commit: 'c'.repeat(40) }));
    assert.equal(values.size, 0);
    await assert.rejects(api.publishRelease('project_alpha', { commit: 'c'.repeat(40), solver_unverified_acknowledged: true }));
    assert.notEqual(ids[0], ids[1]);
  });

  test('从 Hub 导入固定版本与摘要；丢响应后重发同一个请求', async () => {
    const calls = [];
    const body = { name: 'project_alpha', ontology_id: 'package_demo', release_id: 'release_demo', package_digest: 'a'.repeat(64) };
    const api = clientOf(async (path, sent) => {
      calls.push({ path, body: sent });
      if (calls.length === 1) throw new Error('Synthetic timeout');
      return { id: 'project-alpha' };
    });
    await assert.rejects(api.importHubPackage(body));
    await api.importHubPackage(body);
    assert.deepEqual(calls[0], calls[1]);
    assert.equal(calls[1].path, '/hub/imports');
    assert.equal(calls[1].body.release_id, body.release_id);
    assert.equal(values.size, 0);
  });

  test('浏览 Hub 包不带项目或工作区身份', async () => {
    const calls = [];
    const api = clientOf(async () => ({}), async (path, options) => { calls.push([path, options.query]); return { items: [], total: 0 }; });
    await api.hubPackages(20);
    await api.hubPackageReleases('package_demo', 40);
    assert.deepEqual(calls, [['/hub/packages', { offset: 20, limit: 20 }], ['/hub/packages/package_demo/releases', { offset: 40, limit: 20 }]]);
  });
});

// develop !659：包列表的版本列叫「最新版本」，版本列表用「创建时间」代替「摘要」
test('导入已发布知识包的列表列名与 develop 一致', () => {
  const source = readFileSync(new URL('../src/pages/import/HubImportModal.jsx', import.meta.url), 'utf8');
  assert.match(source, /title: '最新版本', key: 'version'/);
  assert.match(source, /title: '创建时间', key: 'created_at', render: formatCreatedAt/);
  assert.doesNotMatch(source, /title: '摘要'/);
});
