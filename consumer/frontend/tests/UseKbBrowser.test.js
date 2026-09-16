// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import React, { act, useEffect } from 'react';
import { createServer } from 'vite';

import { createOntologyResource } from '../src/app/providers/ontologyResource.js';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const vite = await createServer({
  root: frontendRoot,
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true },
});
const { default: useKbBrowser } = await vite.ssrLoadModule(
  '/src/features/knowledge/hooks/useKbBrowser.js',
);
const { AuthCtx } = await vite.ssrLoadModule(
  '/src/features/auth/useAuthContext.js',
);

after(async () => {
  await vite.close();
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function ontologyResponse(id) {
  return {
    ok: true,
    status: 200,
    json: async () => ({ ontologies: [{ id, name: id }] }),
  };
}

function createInstrumentedResource() {
  const requests = [];
  const subscriptions = [];
  const invalidations = [];
  const base = createOntologyResource({
    fetcher: (_url, { signal }) => {
      const pending = deferred();
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
  return {
    requests,
    subscriptions,
    invalidations,
    resource: {
      ...base,
      subscribe(scope) {
        subscriptions.push({ ...scope });
        return base.subscribe(scope);
      },
      invalidateScope(scope) {
        invalidations.push({ ...scope });
        base.invalidateScope(scope);
      },
    },
  };
}

function HookHarness({ listEnabled, resource, onSnapshot }) {
  const state = useKbBrowser({
    listEnabled,
    treeEnabled: false,
    ontologyResource: resource,
  });
  useEffect(() => {
    onSnapshot(state);
  }, [onSnapshot, state]);
  return React.createElement('div', {
    'data-loading': String(state.pkgsLoading),
    'data-packages': state.packages.map((item) => item.id).join(','),
  });
}

async function withRenderedHook(run) {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="root"></div></body></html>',
    {
      url: 'http://localhost/',
    },
  );
  const testGlobals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    Event: dom.window.Event,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originalGlobals = new Map(
    Object.keys(testGlobals).map((name) => [
      name,
      Object.getOwnPropertyDescriptor(globalThis, name),
    ]),
  );
  for (const [name, value] of Object.entries(testGlobals)) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value,
    });
  }

  const { createRoot } = await import('react-dom/client');
  const root = createRoot(document.getElementById('root'));
  let latestState = null;
  const onSnapshot = (state) => {
    latestState = state;
  };
  const renderHook = async ({ workspaceId, listEnabled, resource }) => {
    const authValue = {
      can: () => true,
      user: { username: 'operator-a' },
      workspaceId,
      status: 'authenticated',
      workspaceDataReady: true,
    };
    await act(async () => {
      root.render(
        React.createElement(
          AuthCtx.Provider,
          { value: authValue },
          React.createElement(HookHarness, {
            listEnabled,
            resource,
            onSnapshot,
          }),
        ),
      );
    });
  };
  const flush = async (cycles = 4) => {
    for (let index = 0; index < cycles; index += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  };
  const waitFor = async (assertion, attempts = 40) => {
    let lastError;
    for (let index = 0; index < attempts; index += 1) {
      try {
        await flush();
        return assertion();
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error('Timed out waiting for knowledge hook state');
  };

  try {
    await run({
      renderHook,
      flush,
      waitFor,
      getState: () => latestState,
    });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [name, descriptor] of originalGlobals) {
      if (descriptor === undefined) {
        delete globalThis[name];
      } else {
        Object.defineProperty(globalThis, name, descriptor);
      }
    }
  }
}

test('late workspace A invalidation does not abort or replace the active workspace B request', async () => {
  const instrumented = createInstrumentedResource();
  await withRenderedHook(async ({ renderHook, flush, waitFor, getState }) => {
    await renderHook({
      workspaceId: 'ws-a',
      listEnabled: true,
      resource: instrumented.resource,
    });
    await waitFor(() => assert.equal(instrumented.requests.length, 1));
    instrumented.requests[0].pending.resolve(ontologyResponse('onto-a'));
    await waitFor(() =>
      assert.deepEqual(
        getState().packages.map((item) => item.id),
        ['onto-a'],
      ),
    );
    const lateWorkspaceARefresh = getState().retryPackages;

    await renderHook({
      workspaceId: 'ws-b',
      listEnabled: true,
      resource: instrumented.resource,
    });
    await waitFor(() => assert.equal(instrumented.requests.length, 2));
    assert.equal(instrumented.requests[1].signal.aborted, false);

    await act(async () => {
      void lateWorkspaceARefresh({ ontologyId: 'onto-a', workspaceId: 'ws-a' });
      await Promise.resolve();
    });
    await flush();

    assert.equal(instrumented.requests.length, 2);
    assert.equal(instrumented.requests[1].signal.aborted, false);
    instrumented.requests[1].pending.resolve(ontologyResponse('onto-b'));
    await waitFor(() => {
      assert.equal(getState().pkgsLoading, false);
      assert.deepEqual(
        getState().packages.map((item) => item.id),
        ['onto-b'],
      );
    });
  });
});

test('late invalidation after the final list surface leaves does not create a new subscription', async () => {
  const instrumented = createInstrumentedResource();
  await withRenderedHook(async ({ renderHook, flush, waitFor, getState }) => {
    await renderHook({
      workspaceId: 'ws-a',
      listEnabled: true,
      resource: instrumented.resource,
    });
    await waitFor(() => assert.equal(instrumented.requests.length, 1));
    instrumented.requests[0].pending.resolve(ontologyResponse('onto-a'));
    await waitFor(() => assert.equal(getState().pkgsLoading, false));
    const lateRefresh = getState().retryPackages;

    await renderHook({
      workspaceId: 'ws-a',
      listEnabled: false,
      resource: instrumented.resource,
    });
    await waitFor(() => assert.equal(getState().pkgsLoading, false));
    const subscriptionsBeforeLateCallback = instrumented.subscriptions.length;

    await act(async () => {
      void lateRefresh({ ontologyId: 'onto-a', workspaceId: 'ws-a' });
      await Promise.resolve();
    });
    await flush();

    assert.equal(
      instrumented.subscriptions.length,
      subscriptionsBeforeLateCallback,
    );
    assert.equal(instrumented.requests.length, 1);
    assert.equal(getState().pkgsLoading, false);
  });
});
