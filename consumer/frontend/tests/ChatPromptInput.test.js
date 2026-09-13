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

async function withRenderedInput(run) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>');
  const testGlobals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement,
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
  const inputRef = React.createRef();
  const renderInput = async ({
    value,
    isSubmitting = false,
    onChange = () => {},
    onSend = () => {},
  }) => {
    await act(async () => {
      root.render(
        React.createElement(ChatPromptInput, {
          inputRef,
          value,
          isSubmitting,
          onChange,
          onSend,
        }),
      );
    });
  };

  try {
    await run({ dom, inputRef, renderInput });
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

test('renders a multiline prompt control without changing explicit line breaks', async () => {
  await withRenderedInput(async ({ inputRef, renderInput }) => {
    const value = '第一行\n\n第 1 批\n第 2 批';
    await renderInput({ value });

    assert.equal(inputRef.current.tagName, 'TEXTAREA');
    assert.equal(inputRef.current.rows, 1);
    assert.equal(inputRef.current.value, value);
  });
});

test('Enter sends and Shift+Enter keeps editing', async () => {
  await withRenderedInput(async ({ dom, inputRef, renderInput }) => {
    let sendCount = 0;
    await renderInput({
      value: '问题',
      onSend: () => {
        sendCount += 1;
      },
    });

    const shiftedEnter = new dom.window.KeyboardEvent('keydown', {
      key: 'Enter',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    await act(async () => inputRef.current.dispatchEvent(shiftedEnter));
    assert.equal(sendCount, 0);
    assert.equal(shiftedEnter.defaultPrevented, false);

    const enter = new dom.window.KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
    });
    await act(async () => inputRef.current.dispatchEvent(enter));
    assert.equal(sendCount, 1);
    assert.equal(enter.defaultPrevented, true);
  });
});

test('grows with content and caps the prompt height before enabling scrolling', async () => {
  await withRenderedInput(async ({ inputRef, renderInput }) => {
    await renderInput({ value: '第一行' });
    Object.defineProperty(inputRef.current, 'scrollHeight', {
      configurable: true,
      get: () => 180,
    });
    await renderInput({ value: '第一行\n第二行' });
    assert.equal(inputRef.current.style.height, '180px');
    assert.equal(inputRef.current.style.overflowY, 'hidden');

    Object.defineProperty(inputRef.current, 'scrollHeight', {
      configurable: true,
      get: () => 320,
    });
    await renderInput({ value: '第一行\n第二行\n第三行' });
    assert.equal(inputRef.current.style.height, '240px');
    assert.equal(inputRef.current.style.overflowY, 'auto');
  });
});

test('first-send pending and success preserve prompt input focus', async () => {
  await withRenderedInput(async ({ inputRef, renderInput }) => {
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
  });
});
