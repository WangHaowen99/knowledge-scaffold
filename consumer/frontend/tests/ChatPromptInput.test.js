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
const { default: ChatPromptInput } = await vite.ssrLoadModule(
  '/src/features/chat/components/ChatPromptInput.jsx',
);

after(async () => {
  await vite.close();
});

test('first-send pending and success preserve prompt input focus', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>');
  const testGlobals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originalGlobals = new Map(
    Object.keys(testGlobals).map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
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
  const inputRef = React.createRef();
  const renderInput = async ({ value, isSubmitting }) => {
    await act(async () => {
      root.render(React.createElement(ChatPromptInput, {
        inputRef,
        value,
        isSubmitting,
        onChange() {},
        onSend() {},
      }));
    });
  };

  try {
    await renderInput({ value: 'first question', isSubmitting: false });
    inputRef.current.focus();
    assert.equal(document.activeElement, inputRef.current);

    await renderInput({ value: 'first question', isSubmitting: true });
    assert.equal(inputRef.current.readOnly, true);
    assert.equal(inputRef.current.disabled, false);
    assert.equal(document.activeElement, inputRef.current);

    await renderInput({ value: '', isSubmitting: false });
    assert.equal(inputRef.current.readOnly, false);
    assert.equal(document.activeElement, inputRef.current);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [name, descriptor] of originalGlobals) {
      if (descriptor === undefined) delete globalThis[name];
      else Object.defineProperty(globalThis, name, descriptor);
    }
  }
});
