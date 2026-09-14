// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const uiCss = readFileSync(
  fileURLToPath(new URL('../src/shared/ui/ui.css', import.meta.url)),
  'utf8',
);
const vite = await createServer({
  root: frontendRoot,
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true },
});
const { default: Markdown } = await vite.ssrLoadModule(
  '/src/shared/ui/Markdown.jsx',
);
const tableMarkdown =
  '| Column A | Column B |\n| --- | --- |\n| value a | value b |';

after(async () => {
  await vite.close();
});

test('GFM tables render without adding a focus stop before overflow is measured', () => {
  const markup = renderToStaticMarkup(
    React.createElement(Markdown, null, tableMarkdown),
  );

  assert.match(markup, /class="mdv-table-shell"/);
  assert.match(markup, /class="mdv-table-scroll"/);
  assert.match(markup, /<div class="mdv-table-scroll"><table>/);
  assert.doesNotMatch(markup, /role="region"/);
  assert.doesNotMatch(markup, /tabindex=/);
  assert.doesNotMatch(markup, /mdv-table-scroll-hint/);
});

test('table styles prefer readable wrapping before local horizontal scrolling', () => {
  const markdownRule =
    uiCss.match(/\.mwr \.mdv\s*\{(?<rule>[^}]*)\}/)?.groups.rule || '';
  const shellRule =
    uiCss.match(/\.mwr \.mdv-table-shell\s*\{(?<rule>[^}]*)\}/)?.groups.rule ||
    '';
  const scrollRule =
    uiCss.match(/\.mwr \.mdv-table-scroll\s*\{(?<rule>[^}]*)\}/)?.groups.rule ||
    '';
  const tableRule =
    uiCss.match(/\.mwr \.mdv-table-scroll table\s*\{(?<rule>[^}]*)\}/)?.groups
      .rule || '';
  const headerRule =
    uiCss.match(/\.mwr \.mdv th\s*\{(?<rule>[^}]*)\}/)?.groups.rule || '';

  assert.match(markdownRule, /min-width:\s*0/);
  assert.match(markdownRule, /max-width:\s*100%/);
  assert.match(shellRule, /max-width:\s*100%/);
  assert.match(scrollRule, /overflow-x:\s*auto/);
  assert.match(tableRule, /width:\s*100%/);
  assert.doesNotMatch(tableRule, /max-content/);
  assert.match(headerRule, /min-width:\s*7\.5rem/);
  assert.match(headerRule, /white-space:\s*normal/);
  assert.match(headerRule, /overflow-wrap:\s*anywhere/);
});

async function withRenderedMarkdown(run) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>');
  let notifyResize = () => {};
  class ResizeObserverStub {
    constructor(callback) {
      notifyResize = () => callback([]);
    }

    observe() {}

    disconnect() {}
  }
  dom.window.ResizeObserver = ResizeObserverStub;

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
  try {
    await act(async () => {
      root.render(React.createElement(Markdown, null, tableMarkdown));
    });
    await run({ dom, notifyResize });
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

test('only overflowing tables expose scroll guidance and keyboard access', async () => {
  await withRenderedMarkdown(async ({ dom, notifyResize }) => {
    const scrollRegion = document.querySelector('.mdv-table-scroll');
    let clientWidth = 640;
    let scrollWidth = 640;
    let scrollLeft = 0;
    Object.defineProperties(scrollRegion, {
      clientWidth: { configurable: true, get: () => clientWidth },
      scrollWidth: { configurable: true, get: () => scrollWidth },
      scrollLeft: {
        configurable: true,
        get: () => scrollLeft,
        set: (value) => {
          scrollLeft = value;
        },
      },
    });

    await act(async () => notifyResize());
    assert.equal(scrollRegion.getAttribute('role'), null);
    assert.equal(scrollRegion.getAttribute('tabindex'), null);
    assert.equal(document.querySelector('.mdv-table-scroll-hint'), null);

    scrollWidth = 920;
    await act(async () => notifyResize());
    assert.equal(scrollRegion.getAttribute('role'), 'region');
    assert.equal(scrollRegion.getAttribute('aria-label'), '可横向滚动的表格');
    assert.equal(scrollRegion.getAttribute('tabindex'), '0');
    assert.match(
      document.querySelector('.mdv-table-scroll-hint').textContent,
      /左右滚动查看更多列/,
    );
    assert.equal(
      document.querySelector('.mdv-table-shell').dataset.scrollRight,
      'true',
    );

    scrollLeft = 140;
    await act(async () => {
      scrollRegion.dispatchEvent(
        new dom.window.Event('scroll', { bubbles: false }),
      );
    });
    const shell = document.querySelector('.mdv-table-shell');
    assert.equal(shell.dataset.scrollLeft, 'true');
    assert.equal(shell.dataset.scrollRight, 'true');

    scrollLeft = scrollWidth - clientWidth;
    await act(async () => {
      scrollRegion.dispatchEvent(
        new dom.window.Event('scroll', { bubbles: false }),
      );
    });
    assert.equal(shell.dataset.scrollLeft, 'true');
    assert.equal(shell.dataset.scrollRight, undefined);

    clientWidth = scrollWidth;
    scrollLeft = 0;
    await act(async () => notifyResize());
    assert.equal(scrollRegion.getAttribute('role'), null);
    assert.equal(scrollRegion.getAttribute('tabindex'), null);
    assert.equal(document.querySelector('.mdv-table-scroll-hint'), null);
  });
});
