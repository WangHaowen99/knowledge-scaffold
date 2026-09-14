// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { createOntologyResource } from './ontologyResource.js';

const currentDir = dirname(fileURLToPath(import.meta.url));
const appShellSource = readFileSync(
  join(currentDir, '../AppShell.jsx'),
  'utf8',
);
const kbBrowserSource = readFileSync(
  join(currentDir, '../../features/knowledge/hooks/useKbBrowser.js'),
  'utf8',
);
const configCenterSource = readFileSync(
  join(currentDir, '../../features/settings/ConfigCenter.jsx'),
  'utf8',
);
const mcpSettingsSource = readFileSync(
  join(currentDir, '../../features/settings/mcp/McpSettingsPanel.jsx'),
  'utf8',
);

const deferredResponse = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

const response = (ontologies) => ({
  ok: true,
  status: 200,
  json: async () => ({ ontologies }),
});

test('same principal, workspace and revision share one in-flight ontology request', async () => {
  const pending = deferredResponse();
  let calls = 0;
  const resource = createOntologyResource({
    fetcher: (_url, { signal }) => {
      calls += 1;
      assert.equal(signal.aborted, false);
      return pending.promise;
    },
  });
  const scope = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };

  const first = resource.load(scope);
  const second = resource.load(scope);

  assert.equal(first, second);
  await Promise.resolve();
  assert.equal(calls, 1);
  pending.resolve(response([{ id: 'onto-a' }]));
  assert.deepEqual(await first, [{ id: 'onto-a' }]);
  assert.equal(await resource.load(scope), await first);
  assert.equal(calls, 1);
});

test('a new revision performs one new request without reusing stale data', async () => {
  let calls = 0;
  const resource = createOntologyResource({
    fetcher: async () => response([{ id: `onto-${++calls}` }]),
  });

  const first = await resource.load({
    principalId: 'user-a',
    workspaceId: 'ws-a',
    revision: 3,
  });
  const second = await resource.load({
    principalId: 'user-a',
    workspaceId: 'ws-a',
    revision: 4,
  });

  assert.deepEqual(first, [{ id: 'onto-1' }]);
  assert.deepEqual(second, [{ id: 'onto-2' }]);
  assert.equal(calls, 2);
});

test('invalid ontology JSON is rejected and evicted so the same scope can refetch', async () => {
  let calls = 0;
  const resource = createOntologyResource({
    fetcher: async () => {
      calls += 1;
      if (calls === 1) {
        return {
          ok: true,
          status: 200,
          json: async () => {
            throw new SyntaxError('truncated JSON');
          },
        };
      }
      return response([{ id: 'fresh-after-json-error' }]);
    },
  });
  const scope = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };

  await assert.rejects(resource.load(scope), /valid JSON/i);
  assert.deepEqual(await resource.load(scope), [
    { id: 'fresh-after-json-error' },
  ]);
  assert.equal(calls, 2);
});

test('invalid ontology payload shape is rejected and evicted so the same scope can refetch', async () => {
  let calls = 0;
  const resource = createOntologyResource({
    fetcher: async () => {
      calls += 1;
      if (calls === 1) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ data: [] }),
        };
      }
      return response([{ id: 'fresh-after-shape-error' }]);
    },
  });
  const scope = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };

  await assert.rejects(resource.load(scope), /ontologies array/i);
  assert.deepEqual(await resource.load(scope), [
    { id: 'fresh-after-shape-error' },
  ]);
  assert.equal(calls, 2);
});

test('invalid ontology items are rejected and evicted so the same scope can refetch', async (context) => {
  const invalidItems = [null, 'ontology-a', [], {}, { id: '' }, { id: '   ' }];

  for (const [index, invalidItem] of invalidItems.entries()) {
    await context.test(`invalid item ${index + 1}`, async () => {
      let calls = 0;
      const resource = createOntologyResource({
        fetcher: async () => {
          calls += 1;
          return calls === 1 ?
            response([invalidItem]) :
            response([{ id: `fresh-after-invalid-item-${index + 1}` }]);
        },
      });
      const scope = {
        principalId: 'user-a',
        workspaceId: 'ws-a',
        revision: index,
      };

      await assert.rejects(
        resource.load(scope),
        /valid ontology object with a non-empty id/i,
      );
      assert.deepEqual(await resource.load(scope), [
        { id: `fresh-after-invalid-item-${index + 1}` },
      ]);
      assert.equal(calls, 2);
    });
  }
});

test('scope invalidation aborts the old request and permits a clean retry', async () => {
  const requests = [];
  const resource = createOntologyResource({
    fetcher: (_url, { signal }) => {
      const pending = deferredResponse();
      requests.push({ signal, pending });
      signal.addEventListener(
        'abort',
        () => {
          pending.reject(new DOMException('aborted', 'AbortError'));
        },
        { once: true },
      );
      return pending.promise;
    },
  });
  const scope = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };

  const stale = resource.load(scope);
  await Promise.resolve();
  resource.invalidateScope(scope);

  assert.equal(requests[0].signal.aborted, true);
  await assert.rejects(stale, { name: 'AbortError' });
  const retry = resource.load(scope);
  await Promise.resolve();
  assert.equal(requests.length, 2);
  requests[1].pending.resolve(response([{ id: 'fresh' }]));
  assert.deepEqual(await retry, [{ id: 'fresh' }]);
});

test('releasing the last pending subscriber aborts the shared request and permits a refetch', async () => {
  const requests = [];
  const resource = createOntologyResource({
    fetcher: (_url, { signal }) => {
      const pending = deferredResponse();
      requests.push({ signal, pending });
      signal.addEventListener(
        'abort',
        () => {
          pending.reject(new DOMException('aborted', 'AbortError'));
        },
        { once: true },
      );
      return pending.promise;
    },
  });
  const scope = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };

  const first = resource.subscribe(scope);
  const second = resource.subscribe(scope);
  assert.equal(first.promise, second.promise);
  await Promise.resolve();
  assert.equal(requests.length, 1);

  first.release();
  assert.equal(requests[0].signal.aborted, false);
  second.release();
  assert.equal(requests[0].signal.aborted, true);
  await assert.rejects(first.promise, { name: 'AbortError' });

  const retry = resource.subscribe(scope);
  await Promise.resolve();
  assert.equal(requests.length, 2);
  requests[1].pending.resolve(response([{ id: 'fresh-after-release' }]));
  assert.deepEqual(await retry.promise, [{ id: 'fresh-after-release' }]);
  retry.release();
});

test('releasing subscribers after resolution retains the parsed cache entry', async () => {
  let calls = 0;
  let requestSignal;
  const resource = createOntologyResource({
    fetcher: async (_url, { signal }) => {
      calls += 1;
      requestSignal = signal;
      return response([{ id: 'cached-ontology' }]);
    },
  });
  const scope = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };

  const first = resource.subscribe(scope);
  assert.deepEqual(await first.promise, [{ id: 'cached-ontology' }]);
  first.release();
  const cached = resource.subscribe(scope);

  assert.equal(cached.promise, first.promise);
  assert.deepEqual(await cached.promise, [{ id: 'cached-ontology' }]);
  cached.release();
  assert.equal(calls, 1);
  assert.equal(requestSignal.aborted, false);
});

test('logout clears only the matching principal cache entries', async () => {
  let calls = 0;
  const resource = createOntologyResource({
    fetcher: async () => {
      calls += 1;
      return response([{ id: `ontology-${calls}`, call: calls }]);
    },
  });
  const userA = { principalId: 'user-a', workspaceId: 'ws-a', revision: 0 };
  const userB = { principalId: 'user-b', workspaceId: 'ws-a', revision: 0 };

  await resource.load(userA);
  await resource.load(userB);
  resource.clearPrincipal('user-a');
  await resource.load(userA);
  await resource.load(userB);

  assert.equal(calls, 3);
});

test('AppShell gates list and tree subscribers and clears the principal on logout', () => {
  assert.equal(appShellSource.includes('ontologyRequestPolicy({'), true);
  assert.equal(
    appShellSource.includes('listEnabled: ontologyPolicy.listEnabled'),
    true,
  );
  assert.equal(
    appShellSource.includes('treeEnabled: ontologyPolicy.treeEnabled'),
    true,
  );
  assert.equal(appShellSource.includes('ontologyResource,'), true);
  assert.equal(
    appShellSource.includes('clearOntologyResourcePrincipal(principalId)'),
    true,
  );
  assert.equal(appShellSource.includes('ontologies={kb.packages}'), true);
  assert.equal(
    kbBrowserSource.includes(
      'if (!treeEnabled || !workspaceDataReady || !listStateCurrent || !pkgId)',
    ),
    true,
  );
  assert.equal(configCenterSource.includes('ontologies={ontologies}'), true);
  assert.equal(
    mcpSettingsSource.includes("request('/agentic/ontologies'"),
    false,
  );
});

test('AppShell propagates authoritative ontology loading, error and retry state to chat and MCP', () => {
  assert.equal(
    (appShellSource.match(/ontologiesLoading=\{kb\.pkgsLoading\}/g) || [])
      .length,
    2,
  );
  assert.equal(
    (appShellSource.match(/ontologiesError=\{kb\.packagesError\}/g) || [])
      .length,
    2,
  );
  assert.equal(
    (appShellSource.match(/onRetryOntologies=\{kb\.retryPackages\}/g) || [])
      .length,
    2,
  );
  assert.equal(
    configCenterSource.includes('ontologiesLoading={ontologiesLoading}'),
    true,
  );
  assert.equal(
    configCenterSource.includes('ontologiesError={ontologiesError}'),
    true,
  );
  assert.equal(
    configCenterSource.includes('onRetryOntologies={onRetryOntologies}'),
    true,
  );
  assert.equal(
    mcpSettingsSource.includes(
      'const ontologyListReady = !ontologiesLoading && !ontologiesError;',
    ),
    true,
  );
  assert.equal(
    mcpSettingsSource.includes('const refreshAll = useCallback'),
    true,
  );
  assert.equal(mcpSettingsSource.includes('onRetryOntologies?.()'), true);
  assert.equal(mcpSettingsSource.includes('ontologiesRef'), false);
  assert.equal(
    mcpSettingsSource.includes(
      '() => (ontologyListReady ? normalizedOntologies : [])',
    ),
    true,
  );
  assert.match(
    mcpSettingsSource,
    /if \(!ontologyListReady\)\s*\{\s*return;\s*\}/,
  );
  assert.equal(
    /\{\s*ontologyListReady\s*&&\s*!ontologyOptions\.some\(\(item\) => item\.eligibility\.eligible\)\s*&&\s*\(/.test(
      mcpSettingsSource,
    ),
    true,
  );
});

test('ontology list view is non-authoritative until the active scope subscription is initialized', () => {
  assert.equal(
    kbBrowserSource.includes(
      "const listStateKey = `${scopeKey}:${listEnabled ? 'enabled' : 'disabled'}`;",
    ),
    true,
  );
  assert.equal(
    kbBrowserSource.includes(
      'const listStateCurrent = resolvedListStateKey === listStateKey;',
    ),
    true,
  );
  assert.equal(
    kbBrowserSource.includes('packages: listStateCurrent ? packages : [],'),
    true,
  );
  assert.match(
    kbBrowserSource,
    /pkgsLoading:\s*listStateCurrent\s*\?\s*pkgsLoading\s*:\s*Boolean\(listEnabled\s*&&\s*workspaceDataReady\),/,
  );
  assert.equal(
    kbBrowserSource.includes(
      "packagesError: listStateCurrent ? packagesError : '',",
    ),
    true,
  );
});

test('useKbBrowser releases its active ontology subscription when the list surface leaves', () => {
  assert.equal(
    kbBrowserSource.includes('const packageSubscriptionRef = useRef(null);'),
    true,
  );
  assert.match(
    kbBrowserSource,
    /ontologyResource\.subscribe\(\{\s*principalId,\s*workspaceId,\s*revision,?\s*\}\)/,
  );
  assert.equal(kbBrowserSource.includes('releasePackageSubscription();'), true);
});
