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

async function withRenderedDataSource(run) {
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
    await run({ dom, renderPanel, flush, waitFor });
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
  globalThis.fetch = async (url, options = {}) => {
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

  await withRenderedDataSource(async ({ dom, renderPanel, flush, waitFor }) => {
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

    await click(dom, document.querySelector('.btn.btn-danger'));
    assert.ok(document.querySelector('.ui-dialog'));

    await click(dom, document.querySelector('.ui-dialog-actions .btn-danger-solid'));
    await waitFor(() => {
      assert.equal(deleteCalls, 1);
      assert.equal(document.querySelector('.ui-dialog'), null);
      assert.equal(document.querySelector('.btn.btn-danger'), null);
      assert.equal(refreshCalls, 1);
    });
  });
});
