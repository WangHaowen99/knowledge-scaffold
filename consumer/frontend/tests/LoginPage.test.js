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
const { default: LoginPage } = await vite.ssrLoadModule('/src/features/auth/LoginPage.jsx');
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

async function withRenderedLoginPage(run) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const testGlobals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    HTMLFormElement: dom.window.HTMLFormElement,
    HTMLInputElement: dom.window.HTMLInputElement,
    Event: dom.window.Event,
    MouseEvent: dom.window.MouseEvent,
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

  const renderPage = async (authValue = { login: async () => ({ success: true }) }) => {
    await act(async () => {
      root.render(
        React.createElement(
          AuthCtx.Provider,
          { value: authValue },
          React.createElement(LoginPage),
        ),
      );
    });
  };

  const flush = async () => {
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  };

  try {
    await run({ dom, renderPage, flush });
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

async function openRegisterView(dom) {
  const switchButton = document.querySelector('.lswitch .llink');
  assert.ok(switchButton);
  await act(async () => {
    switchButton.dispatchEvent(new dom.window.MouseEvent('click', {
      bubbles: true,
      cancelable: true,
    }));
  });
}

async function changeInputValue(dom, input, value) {
  const valueSetter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    'value',
  )?.set;
  valueSetter.call(input, value);
  await act(async () => {
    input.dispatchEvent(new dom.window.Event('input', {
      bubbles: true,
      cancelable: true,
    }));
    input.dispatchEvent(new dom.window.Event('change', {
      bubbles: true,
      cancelable: true,
    }));
  });
}

test('register form marks required fields and shows feedback on empty submit', async () => {
  let registerCalls = 0;
  globalThis.fetch = async (url) => {
    if (url === '/auth/capabilities') return jsonResponse({ registration_enabled: true });
    if (url === '/auth/register') {
      registerCalls += 1;
      return jsonResponse({ success: true });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  await withRenderedLoginPage(async ({ dom, renderPage, flush }) => {
    await renderPage();
    await flush();
    await openRegisterView(dom);

    const form = document.querySelector('form.lcard');
    const usernameInput = document.getElementById('register-username');
    const emailInput = document.getElementById('register-email');
    const passwordInput = document.getElementById('register-password');
    assert.ok(form);
    assert.equal(usernameInput.required, true);
    assert.equal(emailInput.required, true);
    assert.equal(passwordInput.required, true);

    let reportValidityCalls = 0;
    form.checkValidity = () => false;
    form.reportValidity = () => {
      reportValidityCalls += 1;
      return false;
    };

    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {
        bubbles: true,
        cancelable: true,
      }));
    });

    const errorBox = document.querySelector('.lerr[role="alert"]');
    assert.equal(registerCalls, 0);
    assert.equal(reportValidityCalls, 1);
    assert.ok(errorBox);
    assert.match(errorBox.textContent, /请先完成表单必填项和格式校验后再注册/);
  });
});

test('register form still submits valid input to /auth/register', async () => {
  let registerPayload = null;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/capabilities') return jsonResponse({ registration_enabled: true });
    if (url === '/auth/register') {
      registerPayload = JSON.parse(options.body);
      return jsonResponse({ success: true });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  await withRenderedLoginPage(async ({ dom, renderPage, flush }) => {
    await renderPage();
    await flush();
    await openRegisterView(dom);

    const form = document.querySelector('form.lcard');
    const usernameInput = document.getElementById('register-username');
    const emailInput = document.getElementById('register-email');
    const passwordInput = document.getElementById('register-password');
    form.checkValidity = () => true;
    form.reportValidity = () => true;

    await changeInputValue(dom, usernameInput, 'alice');
    await changeInputValue(dom, emailInput, 'alice@example.com');
    await changeInputValue(dom, passwordInput, 'P@ssw0rd!');

    await act(async () => {
      form.dispatchEvent(new dom.window.Event('submit', {
        bubbles: true,
        cancelable: true,
      }));
    });
    await flush();

    assert.deepEqual(registerPayload, {
      username: 'alice',
      email: 'alice@example.com',
      password: 'P@ssw0rd!',
    });
    assert.equal(document.getElementById('login-username').value, 'alice');
    assert.ok(document.querySelector('.lok'));
  });
});
