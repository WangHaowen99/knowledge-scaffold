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
const { Modal } = await vite.ssrLoadModule('/src/shared/ui/primitives.jsx');

after(async () => {
  await vite.close();
});

// Modal 依赖 document、window、HTMLElement 等浏览器全局，且内部 useEffect
// 会注册全局 keydown 监听。每个测试用独立 JSDOM 实例并在结束时还原全局，
// 避免监听器互相干扰。事件构造器也需从 dom.window 取，避免使用 Node 全局。
function withDom(testFn) {
  return async () => {
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
      Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
    }

    const { createRoot } = await import('react-dom/client');
    const root = createRoot(document.getElementById('root'));
    const closeSpy = { calls: 0 };
    const renderModal = async (props = {}) => {
      await act(async () => {
        root.render(React.createElement(Modal, {
          title: '测试弹窗',
          onClose: () => { closeSpy.calls += 1; },
          ...props,
        }, React.createElement('input', { 'aria-label': '输入' })));
      });
    };
    const backdrop = () => document.querySelector('.ui-modal-backdrop');
    const closeButton = () => document.querySelector('.ui-modal-backdrop header button[aria-label="关闭"]');
    const mouseDown = (target) => target.dispatchEvent(new dom.window.MouseEvent('mousedown', { bubbles: true }));
    const keyDown = (key, opts = {}) => document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key, ...opts }));

    try {
      await testFn({ renderModal, backdrop, closeButton, closeSpy, mouseDown, keyDown, dom });
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
      for (const [name, descriptor] of originalGlobals) {
        if (descriptor === undefined) delete globalThis[name];
        else Object.defineProperty(globalThis, name, descriptor);
      }
    }
  };
}

test('默认 props：点击遮罩调用 onClose', withDom(async ({ renderModal, backdrop, closeSpy, mouseDown }) => {
  await renderModal();
  assert.equal(closeSpy.calls, 0);
  mouseDown(backdrop());
  assert.equal(closeSpy.calls, 1);
}));

test('默认 props：按 Esc 调用 onClose', withDom(async ({ renderModal, closeSpy, keyDown }) => {
  await renderModal();
  assert.equal(closeSpy.calls, 0);
  keyDown('Escape');
  assert.equal(closeSpy.calls, 1);
}));

test('closeOnOverlayClick=false：点击遮罩不调用 onClose', withDom(async ({ renderModal, backdrop, closeSpy, mouseDown }) => {
  await renderModal({ closeOnOverlayClick: false });
  mouseDown(backdrop());
  assert.equal(closeSpy.calls, 0);
}));

test('closeOnEscape=false：按 Esc 不调用 onClose', withDom(async ({ renderModal, closeSpy, keyDown }) => {
  await renderModal({ closeOnEscape: false });
  keyDown('Escape');
  assert.equal(closeSpy.calls, 0);
}));

test('任何配置下点击 X 按钮都调用 onClose', withDom(async ({ renderModal, closeButton, closeSpy }) => {
  await renderModal({ closeOnOverlayClick: false, closeOnEscape: false });
  closeButton().click();
  assert.equal(closeSpy.calls, 1);
}));

test('closeOnOverlayClick=false：点击弹窗内部不调用 onClose', withDom(async ({ renderModal, backdrop, closeSpy, mouseDown }) => {
  await renderModal({ closeOnOverlayClick: false });
  // 在弹窗主体上派发 mousedown，target !== currentTarget，不应触发 onClose
  const dialog = backdrop().querySelector('.ui-modal');
  mouseDown(dialog);
  assert.equal(closeSpy.calls, 0);
}));

test('Tab 在弹窗内可在首个/末尾可聚焦元素之间循环', withDom(async ({ renderModal, backdrop, dom }) => {
  await renderModal();
  const focusable = () => Array.from(backdrop().querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
  ));
  const items = focusable();
  assert.ok(items.length >= 2, '应有可聚焦元素');
  // Modal 的 Tab 循环逻辑：在首个元素按 Shift+Tab 跳到末尾；在末尾按 Tab 跳回首。
  // 用 act 包裹 focus 与 keydown，避免 React 19 在 act 外调度更新导致卡死。
  const tab = (shiftKey = false) => document.dispatchEvent(
    new dom.window.KeyboardEvent('keydown', { key: 'Tab', shiftKey }),
  );
  await act(async () => { items[0].focus(); });
  assert.equal(document.activeElement, items[0]);
  await act(async () => { tab(true); });
  assert.equal(document.activeElement, items.at(-1));
  await act(async () => { tab(false); });
  assert.equal(document.activeElement, items[0]);
}));
