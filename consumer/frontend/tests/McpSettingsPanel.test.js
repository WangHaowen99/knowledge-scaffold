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
const { default: McpSettingsPanel } = await vite.ssrLoadModule(
  '/src/features/settings/mcp/McpSettingsPanel.jsx',
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

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function ontology(id, name) {
  return {
    id,
    name,
    enabled: true,
    active_version: '1.0.0',
    versions: ['1.0.0'],
    business_data: { configured: true, ready: true, provider: 'neo4j' },
  };
}

function statusPayload(workspaceId, endpointId) {
  return {
    workspace_id: workspaceId,
    mcp_endpoint_id: endpointId,
    running: true,
    transport: 'streamable-http',
    public_base_url: 'http://localhost/mcp',
    services: [],
  };
}

async function withRenderedPanel(run) {
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
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originalGlobals = new Map(
    Object.keys(testGlobals).map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  );
  for (const [name, value] of Object.entries(testGlobals)) {
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }

  const { createRoot } = await import('react-dom/client');
  const root = createRoot(document.getElementById('root'));
  const renderPanel = async (props) => {
    await act(async () => {
      root.render(React.createElement(McpSettingsPanel, props));
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
    throw lastError || new Error('Timed out waiting for MCP panel state');
  };
  const click = async (element) => {
    await act(async () => {
      element.dispatchEvent(new dom.window.MouseEvent('click', {
        bubbles: true,
        cancelable: true,
      }));
    });
  };

  try {
    await run({ renderPanel, flush, waitFor, click });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [name, descriptor] of originalGlobals) {
      if (descriptor === undefined) delete globalThis[name];
      else Object.defineProperty(globalThis, name, descriptor);
    }
  }
}

test('workspace switch never projects the previous ontology list into a loading or failed scope', async () => {
  const workspaceBStatus = deferred();
  const workspaceBTokens = deferred();
  const calls = [];
  let requestWorkspace = 'ws-a';
  const request = (url) => {
    const workspaceId = requestWorkspace;
    calls.push({ workspaceId, url });
    if (workspaceId === 'ws-a' && url === '/agentic/mcp/runtime/status') {
      return Promise.resolve(jsonResponse(statusPayload('ws-a', '000000000000000a')));
    }
    if (workspaceId === 'ws-a' && url === '/agentic/mcp/tokens') {
      return Promise.resolve(jsonResponse({ items: [] }));
    }
    if (workspaceId === 'ws-b' && url === '/agentic/mcp/runtime/status') {
      return workspaceBStatus.promise;
    }
    if (workspaceId === 'ws-b' && url === '/agentic/mcp/tokens') {
      return workspaceBTokens.promise;
    }
    throw new Error(`Unexpected request for ${workspaceId}: ${url}`);
  };

  await withRenderedPanel(async ({ renderPanel, flush, waitFor, click }) => {
    const common = {
      authenticated: true,
      canManageTokens: true,
      request,
      onRetryOntologies() {},
    };
    await renderPanel({
      ...common,
      workspaceId: 'ws-a',
      workspaceName: 'Workspace A',
      ontologies: [ontology('onto-a', 'Ontology A')],
      ontologiesLoading: false,
      ontologiesError: '',
    });
    await waitFor(() => {
      assert.match(document.querySelector('.settings-mcp-create').textContent, /Ontology A/);
    });

    requestWorkspace = 'ws-b';
    await renderPanel({
      ...common,
      workspaceId: 'ws-b',
      workspaceName: 'Workspace B',
      ontologies: [],
      ontologiesLoading: true,
      ontologiesError: '',
    });
    await waitFor(() => {
      assert.equal(calls.filter((call) => call.workspaceId === 'ws-b').length, 2);
    });

    workspaceBStatus.resolve(jsonResponse(statusPayload('ws-b', '000000000000000b')));
    workspaceBTokens.resolve(jsonResponse({ items: [] }));
    await flush();

    const createSection = document.querySelector('.settings-mcp-create');
    const ontologySelect = createSection.querySelectorAll('select')[1];
    const createButton = createSection.querySelector('.btn.btn-p');
    assert.doesNotMatch(createSection.textContent, /Ontology A/);
    assert.equal(ontologySelect.disabled, true);
    assert.equal(createButton.disabled, true);

    await renderPanel({
      ...common,
      workspaceId: 'ws-b',
      workspaceName: 'Workspace B',
      ontologies: [],
      ontologiesLoading: false,
      ontologiesError: 'list failed',
    });
    assert.doesNotMatch(document.querySelector('.settings-mcp-create').textContent, /Ontology A/);
    assert.equal(document.querySelector('.settings-mcp-create .btn.btn-p').disabled, true);

    await renderPanel({
      ...common,
      workspaceId: 'ws-b',
      workspaceName: 'Workspace B',
      ontologies: [ontology('onto-b', 'Ontology B')],
      ontologiesLoading: false,
      ontologiesError: '',
    });
    await waitFor(() => {
      const section = document.querySelector('.settings-mcp-create');
      assert.match(section.textContent, /Ontology B/);
      assert.doesNotMatch(section.textContent, /Ontology A/);
      assert.equal(section.querySelector('.btn.btn-p').disabled, false);
    });

    const readySection = document.querySelector('.settings-mcp-create');
    await click(readySection.querySelector('fieldset input[type="checkbox"]'));
    await waitFor(() => {
      const selects = document.querySelector('.settings-mcp-create').querySelectorAll('select');
      assert.equal(selects[1].value, 'onto-b');
      assert.equal(document.querySelector('.settings-mcp-create .btn.btn-p').disabled, false);
    });
  });
});
