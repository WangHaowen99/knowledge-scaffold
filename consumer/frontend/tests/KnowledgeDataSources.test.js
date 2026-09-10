import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const vite = await createServer({
  root: frontendRoot,
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true },
});
const { default: KnowledgeDataSources } = await vite.ssrLoadModule(
  '/src/features/knowledge/KnowledgeDataSources.jsx',
);
const { AuthCtx } = await vite.ssrLoadModule('/src/features/auth/useAuthContext.js');

after(async () => {
  await vite.close();
});

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function withRenderedDataSource(fetchImpl, run) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/',
  });
  const testGlobals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    HTMLButtonElement: dom.window.HTMLButtonElement,
    HTMLInputElement: dom.window.HTMLInputElement,
    HTMLSelectElement: dom.window.HTMLSelectElement,
    Event: dom.window.Event,
    MouseEvent: dom.window.MouseEvent,
    KeyboardEvent: dom.window.KeyboardEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originalGlobals = new Map(
    Object.keys(testGlobals).map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  const originalFetch = globalThis.fetch;
  for (const [name, value] of Object.entries(testGlobals)) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value,
    });
  }
  globalThis.fetch = fetchImpl;

  const { createRoot } = await import('react-dom/client');
  const root = createRoot(document.getElementById('root'));

  const renderPanel = async ({
    ontologyId = 'onto-1',
    onDataSourceChanged = () => {},
    authValue = {
      can: (permission) => ['ws.data_source.write', 'ws.data_source.test'].includes(permission),
      workspaceId: 'ws-1',
    },
  } = {}) => {
    await act(async () => {
      root.render(
        React.createElement(
          AuthCtx.Provider,
          { value: authValue },
          React.createElement(KnowledgeDataSources, {
            ontologyId,
            onDataSourceChanged,
          }),
        ),
      );
    });
  };

  const removePanel = async () => {
    await act(async () => {
      root.render(null);
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
    let lastError = null;
    for (let index = 0; index < attempts; index += 1) {
      try {
        await flush();
        return assertion();
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error('Timed out waiting for assertion');
  };

  try {
    await run({ dom, renderPanel, removePanel, flush, waitFor });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    globalThis.fetch = originalFetch;
    for (const [name, descriptor] of originalGlobals) {
      if (descriptor === undefined) delete globalThis[name];
      else Object.defineProperty(globalThis, name, descriptor);
    }
  }
}

async function click(dom, element) {
  await act(async () => {
    element.dispatchEvent(new dom.window.MouseEvent('click', {
      bubbles: true,
      cancelable: true,
    }));
  });
}

test('cancel keeps the current data source and confirm sends the delete request', async () => {
  let configured = true;
  let deleteCalls = 0;
  let refreshCalls = 0;
  const fetchImpl = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/agentic/business-data/provider-schemas') {
      return jsonResponse({
        providers: [{
          name: 'postgres',
          label: 'Postgres',
          fields: [{ name: 'dsn', label: 'DSN', type: 'text', required: true }],
        }],
      });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'GET') {
      return jsonResponse(configured
        ? {
          configured: true,
          source: { provider: 'postgres', config: { dsn: 'postgres://demo' } },
        }
        : {
          configured: false,
          source: null,
        });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'DELETE') {
      deleteCalls += 1;
      configured = false;
      return jsonResponse({ configured: false, source: null });
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  };

  await withRenderedDataSource(fetchImpl, async ({ dom, renderPanel, flush, waitFor }) => {
    await renderPanel({
      onDataSourceChanged: () => {
        refreshCalls += 1;
      },
    });

    await waitFor(() => {
      const clearButton = document.querySelector('.btn.btn-danger');
      assert.ok(clearButton);
    });

    await click(dom, document.querySelector('.btn.btn-danger'));
    const firstDialog = document.querySelector('.ui-dialog');
    assert.ok(firstDialog);
    assert.match(firstDialog.textContent, /当前知识包将变为“未配置”/);
    assert.match(firstDialog.textContent, /相关查询将不可用/);
    assert.equal(deleteCalls, 0);

    await click(dom, document.querySelector('.ui-dialog-actions .btn'));
    await flush();
    assert.equal(deleteCalls, 0);
    assert.equal(document.querySelector('.ui-dialog'), null);
    assert.ok(document.querySelector('.btn.btn-danger'));
    assert.match(document.querySelector('.tag').textContent, /已配置/);

    await click(dom, document.querySelector('.btn.btn-danger'));
    assert.ok(document.querySelector('.ui-dialog'));

    await click(dom, document.querySelector('.ui-dialog-actions .btn-danger-solid'));
    await waitFor(() => {
      assert.equal(deleteCalls, 1);
      assert.equal(document.querySelector('.ui-dialog'), null);
      assert.equal(document.querySelector('.btn.btn-danger'), null);
      assert.match(document.querySelector('.tag').textContent, /未配置/);
      assert.equal(refreshCalls, 1);
    });
  });
});

test('a successful save notifies its original scope after the user switches packages', async () => {
  const saveResponse = deferred();
  const getCalls = new Map();
  let saveCalls = 0;
  const fetchImpl = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/agentic/business-data/provider-schemas') {
      return jsonResponse({
        providers: [{
          name: 'postgres',
          label: 'Postgres',
          fields: [{ name: 'dsn', label: 'DSN', type: 'text', required: true }],
        }],
      });
    }
    const match = /^\/agentic\/ontologies\/(onto-[12])\/data-source$/.exec(url);
    const [, ontologyKey] = match || [];
    if (ontologyKey && method === 'GET') {
      getCalls.set(ontologyKey, (getCalls.get(ontologyKey) || 0) + 1);
      return jsonResponse({
        configured: true,
        source: { provider: 'postgres', config: { dsn: `postgres://${ontologyKey}` } },
      });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'PUT') {
      saveCalls += 1;
      return saveResponse.promise;
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  };

  await withRenderedDataSource(fetchImpl, async ({ dom, renderPanel, flush, waitFor }) => {
    const originalNotifications = [];
    const nextNotifications = [];
    await renderPanel({
      ontologyId: 'onto-1',
      onDataSourceChanged: (scope) => originalNotifications.push(scope),
    });
    await waitFor(() => {
      const saveButton = document.querySelector('.foot .btn.btn-p');
      assert.ok(saveButton);
      assert.equal(saveButton.disabled, false);
    });

    await click(dom, document.querySelector('.foot .btn.btn-p'));
    await waitFor(() => assert.equal(saveCalls, 1));

    await renderPanel({
      ontologyId: 'onto-2',
      onDataSourceChanged: (scope) => nextNotifications.push(scope),
    });
    await waitFor(() => assert.equal(getCalls.get('onto-2'), 1));

    saveResponse.resolve(jsonResponse({ configured: true }));
    await waitFor(() => assert.equal(originalNotifications.length, 1));

    assert.deepEqual(originalNotifications, [{ ontologyId: 'onto-1', workspaceId: 'ws-1' }]);
    assert.deepEqual(nextNotifications, []);
    assert.equal(getCalls.get('onto-1'), 1);
    await flush();
  });
});

test('a successful save invalidates its scope before the response body finishes parsing', async () => {
  const responseBody = deferred();
  const notifications = [];
  let saveCalls = 0;
  const fetchImpl = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/agentic/business-data/provider-schemas') {
      return jsonResponse({
        providers: [{
          name: 'postgres',
          label: 'Postgres',
          fields: [{ name: 'dsn', label: 'DSN', type: 'text', required: true }],
        }],
      });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'GET') {
      return jsonResponse({
        configured: true,
        source: { provider: 'postgres', config: { dsn: 'postgres://onto-1' } },
      });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'PUT') {
      saveCalls += 1;
      return { ok: true, status: 200, json: () => responseBody.promise };
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  };

  await withRenderedDataSource(fetchImpl, async ({ dom, renderPanel, flush, waitFor }) => {
    await renderPanel({
      onDataSourceChanged: (scope) => notifications.push(scope),
    });
    await waitFor(() => assert.equal(document.querySelector('.foot .btn.btn-p')?.disabled, false));

    await click(dom, document.querySelector('.foot .btn.btn-p'));
    await waitFor(() => assert.equal(saveCalls, 1));
    await flush();
    const notificationsBeforeBody = notifications.length;

    responseBody.resolve({ configured: true });
    await flush();
    assert.equal(notificationsBeforeBody, 1);
    assert.deepEqual(notifications[0], { ontologyId: 'onto-1', workspaceId: 'ws-1' });
  });
});

test('a successful delete notifies its original scope after the editor unmounts', async () => {
  const deleteResponse = deferred();
  const notifications = [];
  let getCalls = 0;
  let deleteCalls = 0;
  const fetchImpl = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (url === '/agentic/business-data/provider-schemas') {
      return jsonResponse({
        providers: [{
          name: 'postgres',
          label: 'Postgres',
          fields: [{ name: 'dsn', label: 'DSN', type: 'text', required: true }],
        }],
      });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'GET') {
      getCalls += 1;
      return jsonResponse({
        configured: true,
        source: { provider: 'postgres', config: { dsn: 'postgres://onto-1' } },
      });
    }
    if (url === '/agentic/ontologies/onto-1/data-source' && method === 'DELETE') {
      deleteCalls += 1;
      return deleteResponse.promise;
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  };

  await withRenderedDataSource(fetchImpl, async ({ dom, renderPanel, removePanel, flush, waitFor }) => {
    await renderPanel({
      onDataSourceChanged: (scope) => notifications.push(scope),
    });
    await waitFor(() => assert.ok(document.querySelector('.btn.btn-danger')));

    await click(dom, document.querySelector('.btn.btn-danger'));
    await click(dom, document.querySelector('.ui-dialog-actions .btn-danger-solid'));
    await waitFor(() => assert.equal(deleteCalls, 1));

    await removePanel();
    deleteResponse.resolve(jsonResponse({ configured: false, source: null }));
    await waitFor(() => assert.equal(notifications.length, 1));

    assert.deepEqual(notifications, [{ ontologyId: 'onto-1', workspaceId: 'ws-1' }]);
    assert.equal(getCalls, 1);
    await flush();
  });
});
