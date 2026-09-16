// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
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

function concurrencyPayload(value = 5) {
  return {
    stream_user_workspace_concurrency: value,
    default: 5,
    min: 1,
    max: 32,
  };
}

function setInputValue(input, value) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value',
  ).set;
  setter.call(input, value);
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
}

async function withRenderedPanel(run) {
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
    HTMLButtonElement: dom.window.HTMLButtonElement,
    HTMLInputElement: dom.window.HTMLInputElement,
    HTMLSelectElement: dom.window.HTMLSelectElement,
    Event: dom.window.Event,
    MouseEvent: dom.window.MouseEvent,
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
      element.dispatchEvent(
        new dom.window.MouseEvent('click', {
          bubbles: true,
          cancelable: true,
        }),
      );
    });
  };

  try {
    await run({ renderPanel, flush, waitFor, click });
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

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
test('workspace switch never projects the previous ontology list into a loading or failed scope', async () => {
  const workspaceBStatus = deferred();
  const workspaceBTokens = deferred();
  const workspaceBConcurrency = deferred();
  const calls = [];
  let requestWorkspace = 'ws-a';
  const request = (url) => {
    const workspaceId = requestWorkspace;
    calls.push({ workspaceId, url });
    if (workspaceId === 'ws-a' && url === '/agentic/mcp/runtime/status') {
      return Promise.resolve(
        jsonResponse(statusPayload('ws-a', '000000000000000a')),
      );
    }
    if (workspaceId === 'ws-a' && url === '/agentic/mcp/tokens') {
      return Promise.resolve(jsonResponse({ items: [] }));
    }
    if (workspaceId === 'ws-a' && url === '/agentic/mcp/runtime/concurrency') {
      return Promise.resolve(jsonResponse(concurrencyPayload()));
    }
    if (workspaceId === 'ws-b' && url === '/agentic/mcp/runtime/status') {
      return workspaceBStatus.promise;
    }
    if (workspaceId === 'ws-b' && url === '/agentic/mcp/tokens') {
      return workspaceBTokens.promise;
    }
    if (workspaceId === 'ws-b' && url === '/agentic/mcp/runtime/concurrency') {
      return workspaceBConcurrency.promise;
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
      assert.match(
        document.querySelector('.settings-mcp-create').textContent,
        /Ontology A/,
      );
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
      assert.equal(
        calls.filter((call) => call.workspaceId === 'ws-b').length,
        3,
      );
    });

    workspaceBStatus.resolve(
      jsonResponse(statusPayload('ws-b', '000000000000000b')),
    );
    workspaceBTokens.resolve(jsonResponse({ items: [] }));
    workspaceBConcurrency.resolve(jsonResponse(concurrencyPayload()));
    await flush();

    const createSection = document.querySelector('.settings-mcp-create');
    const [, ontologySelect] = createSection.querySelectorAll('select');
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
    assert.doesNotMatch(
      document.querySelector('.settings-mcp-create').textContent,
      /Ontology A/,
    );
    assert.equal(
      document.querySelector('.settings-mcp-create .btn.btn-p').disabled,
      true,
    );

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
      const selects = document
        .querySelector('.settings-mcp-create')
        .querySelectorAll('select');
      assert.equal(selects[1].value, 'onto-b');
      assert.equal(
        document.querySelector('.settings-mcp-create .btn.btn-p').disabled,
        false,
      );
    });
  });
});

test('platform admin can save the workspace MCP streaming concurrency', async () => {
  const calls = [];
  let concurrency = 5;
  const request = async (url, options = {}) => {
    calls.push({ url, options });
    if (url === '/agentic/mcp/runtime/status') {
      return jsonResponse(statusPayload('ws-a', '000000000000000a'));
    }
    if (url === '/agentic/mcp/tokens') {
      return jsonResponse({ items: [] });
    }
    if (url === '/agentic/mcp/runtime/concurrency' && !options.method) {
      return jsonResponse(concurrencyPayload(concurrency));
    }
    if (url === '/agentic/mcp/runtime/concurrency' && options.method === 'PATCH') {
      concurrency = JSON.parse(options.body).stream_user_workspace_concurrency;
      return jsonResponse(concurrencyPayload(concurrency));
    }
    throw new Error(`Unexpected request: ${options.method || 'GET'} ${url}`);
  };

  await withRenderedPanel(async ({ renderPanel, waitFor, click }) => {
    await renderPanel({
      authenticated: true,
      canManageTokens: false,
      canManageConcurrency: true,
      request,
      workspaceId: 'ws-a',
      workspaceName: 'Workspace A',
      ontologies: [],
      ontologiesLoading: false,
      ontologiesError: '',
      onRetryOntologies() {},
    });
    await waitFor(() => {
      const input = document.querySelector('.settings-mcp-concurrency input');
      assert.equal(input.value, '5');
      assert.equal(input.disabled, false);
    });

    const input = document.querySelector('.settings-mcp-concurrency input');
    await act(async () => setInputValue(input, '12'));
    await click(document.querySelector('.settings-mcp-concurrency .btn'));
    await waitFor(() => {
      const patchCall = calls.find((call) => call.options.method === 'PATCH');
      assert.deepEqual(JSON.parse(patchCall.options.body), {
        stream_user_workspace_concurrency: 12,
      });
      assert.equal(document.querySelector('.settings-mcp-concurrency input').value, '12');
    });
  });
});

test('missing concurrency endpoint does not hide the rest of the MCP panel', async () => {
  const request = async (url) => {
    if (url === '/agentic/mcp/runtime/status') {
      return jsonResponse(statusPayload('ws-a', '000000000000000a'));
    }
    if (url === '/agentic/mcp/tokens') {
      return jsonResponse({ items: [] });
    }
    if (url === '/agentic/mcp/runtime/concurrency') {
      return jsonResponse({ detail: 'Not Found' }, 404);
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  await withRenderedPanel(async ({ renderPanel, waitFor }) => {
    await renderPanel({
      authenticated: true,
      canManageTokens: true,
      canManageConcurrency: true,
      request,
      workspaceId: 'ws-a',
      workspaceName: 'Workspace A',
      ontologies: [],
      ontologiesLoading: false,
      ontologiesError: '',
      onRetryOntologies() {},
    });
    await waitFor(() => {
      assert.equal(document.querySelector('.settings-mcp-metrics strong').textContent, '在线');
      const input = document.querySelector('.settings-mcp-concurrency input');
      assert.equal(input.value, '5');
      assert.equal(input.disabled, true);
    });
  });
});

test('workspace switch blocks a stale concurrency draft until the new scope loads', async () => {
  const workspaceBStatus = deferred();
  const workspaceBTokens = deferred();
  const workspaceBConcurrency = deferred();
  const calls = [];
  let requestWorkspace = 'ws-a';
  let patchCalls = 0;
  const request = (url, options = {}) => {
    calls.push({ workspaceId: requestWorkspace, url, options });
    if (options.method === 'PATCH') {
      patchCalls += 1;
      return Promise.resolve(jsonResponse(concurrencyPayload(99)));
    }
    if (requestWorkspace === 'ws-a' && url === '/agentic/mcp/runtime/status') {
      return Promise.resolve(jsonResponse(statusPayload('ws-a', '000000000000000a')));
    }
    if (requestWorkspace === 'ws-a' && url === '/agentic/mcp/tokens') {
      return Promise.resolve(jsonResponse({ items: [] }));
    }
    if (requestWorkspace === 'ws-a' && url === '/agentic/mcp/runtime/concurrency') {
      return Promise.resolve(jsonResponse(concurrencyPayload(11)));
    }
    if (requestWorkspace === 'ws-b' && url === '/agentic/mcp/runtime/status') {
      return workspaceBStatus.promise;
    }
    if (requestWorkspace === 'ws-b' && url === '/agentic/mcp/tokens') {
      return workspaceBTokens.promise;
    }
    if (
      requestWorkspace === 'ws-b' &&
      url === '/agentic/mcp/runtime/concurrency'
    ) {
      return workspaceBConcurrency.promise;
    }
    throw new Error(`Unexpected request for ${requestWorkspace}: ${url}`);
  };

  await withRenderedPanel(async ({ renderPanel, waitFor, click }) => {
    const common = {
      authenticated: true,
      canManageTokens: false,
      canManageConcurrency: true,
      request,
      ontologies: [],
      ontologiesLoading: false,
      ontologiesError: '',
      onRetryOntologies() {},
    };
    await renderPanel({ ...common, workspaceId: 'ws-a', workspaceName: 'Workspace A' });
    await waitFor(() => assert.equal(
      document.querySelector('.settings-mcp-concurrency input').value,
      '11',
    ));

    requestWorkspace = 'ws-b';
    await renderPanel({ ...common, workspaceId: 'ws-b', workspaceName: 'Workspace B' });
    await waitFor(() => assert.equal(
      calls.filter((call) => call.workspaceId === 'ws-b').length,
      3,
    ));

    const section = document.querySelector('.settings-mcp-concurrency');
    const input = section.querySelector('input');
    const saveButton = section.querySelector('.btn');
    assert.equal(input.disabled, true);
    assert.equal(saveButton.disabled, true);
    await click(saveButton);
    assert.equal(patchCalls, 0);

    workspaceBStatus.resolve(jsonResponse(statusPayload('ws-b', '000000000000000b')));
    workspaceBTokens.resolve(jsonResponse({ items: [] }));
    workspaceBConcurrency.resolve(jsonResponse(concurrencyPayload(7)));
    await waitFor(() => {
      assert.equal(input.disabled, false);
      assert.equal(input.value, '7');
    });
  });
});

test('non-admin can inspect but cannot change MCP streaming concurrency', async () => {
  const request = async (url) => {
    if (url === '/agentic/mcp/runtime/status') {
      return jsonResponse(statusPayload('ws-a', '000000000000000a'));
    }
    if (url === '/agentic/mcp/tokens') {
      return jsonResponse({ items: [] });
    }
    if (url === '/agentic/mcp/runtime/concurrency') {
      return jsonResponse(concurrencyPayload(9));
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  await withRenderedPanel(async ({ renderPanel, waitFor }) => {
    await renderPanel({
      authenticated: true,
      canManageTokens: true,
      canManageConcurrency: false,
      request,
      workspaceId: 'ws-a',
      workspaceName: 'Workspace A',
      ontologies: [],
      ontologiesLoading: false,
      ontologiesError: '',
      onRetryOntologies() {},
    });
    await waitFor(() => {
      const section = document.querySelector('.settings-mcp-concurrency');
      assert.equal(section.querySelector('input').value, '9');
      assert.equal(section.querySelector('input').disabled, true);
      assert.equal(section.querySelector('.btn'), null);
    });
  });
});
