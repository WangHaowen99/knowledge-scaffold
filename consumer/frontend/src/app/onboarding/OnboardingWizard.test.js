import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
const vite = await createServer({
  root: frontendRoot,
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true },
});
const {
  activateWizardFocus,
  default: OnboardingWizard,
  dismissWizardAsSeen,
  resolveHasNoWorkspace,
  trapWizardTab,
} = await vite.ssrLoadModule('/src/app/onboarding/OnboardingWizard.jsx');
const { AuthCtx } = await vite.ssrLoadModule('/src/features/auth/useAuthContext.js');

after(async () => {
  await vite.close();
});

function createKeyboardEvent(key, shiftKey = false) {
  return {
    key,
    shiftKey,
    defaultPrevented: false,
    propagationStopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
  };
}

function createFocusable(documentRef, name, style = {}) {
  return {
    name,
    isConnected: true,
    style: { display: 'block', visibility: 'visible', ...style },
    focusCount: 0,
    focus() {
      this.focusCount += 1;
      documentRef.activeElement = this;
    },
  };
}

test('workspace-empty onboarding requires a completed successful empty list', () => {
  const state = { workspacesLoaded: false, workspacesLoading: false, workspacesError: '', workspaces: [] };
  assert.equal(resolveHasNoWorkspace(undefined, state), false);
  assert.equal(resolveHasNoWorkspace(undefined, { ...state, workspacesLoaded: true, workspacesLoading: true }), false);
  assert.equal(resolveHasNoWorkspace(undefined, { ...state, workspacesLoaded: true, workspacesError: '加载失败' }), false);
  assert.equal(resolveHasNoWorkspace(undefined, { ...state, workspacesLoaded: true, workspaces: [{ id: 'ws-a' }] }), false);
  assert.equal(resolveHasNoWorkspace(undefined, { ...state, workspacesLoaded: true }), true);
  assert.equal(resolveHasNoWorkspace(false, { ...state, workspacesLoaded: true }), false);
  assert.equal(resolveHasNoWorkspace(true, state), true);
});

test('wizard dialog exposes modal and labelled semantics', () => {
  const markup = renderToStaticMarkup(
    React.createElement(
      AuthCtx.Provider,
      {
        value: {
          isAdmin: false,
          workspaces: [],
          workspacesLoaded: false,
          workspacesError: '',
        },
      },
      React.createElement(OnboardingWizard, {
        onNavigate() {},
        onOpenNavigation() {},
        onClose() {},
      }),
    ),
  );

  assert.match(markup, /role="dialog"/);
  assert.match(markup, /aria-modal="true"/);
  assert.match(markup, /tabindex="-1"/);
  const [, labelledBy] = markup.match(/aria-labelledby="([^"]+)"/) || [];
  assert.ok(labelledBy);
  assert.ok(markup.includes(`id="${labelledBy}"`));
});

test('wizard focus activates inside the dialog, Escape dismisses, and cleanup restores focus', () => {
  let keydownHandler = null;
  const documentRef = {
    activeElement: null,
    body: {},
    addEventListener(type, handler) {
      if (type === 'keydown') keydownHandler = handler;
    },
    removeEventListener(type, handler) {
      if (type === 'keydown' && keydownHandler === handler) keydownHandler = null;
    },
  };
  const previous = createFocusable(documentRef, 'previous');
  const first = createFocusable(documentRef, 'first');
  const hidden = createFocusable(documentRef, 'hidden', { display: 'none' });
  const last = createFocusable(documentRef, 'last');
  documentRef.activeElement = previous;
  const items = [hidden, first, last];
  const card = createFocusable(documentRef, 'card');
  card.querySelectorAll = () => items;
  card.contains = (element) => items.includes(element);
  let dismissed = 0;

  const cleanup = activateWizardFocus(card, () => { dismissed += 1; }, {
    documentRef,
    resolveStyle: (element) => element.style,
  });

  assert.equal(documentRef.activeElement, first);
  assert.equal(first.focusCount, 1);
  const escape = createKeyboardEvent('Escape');
  keydownHandler(escape);
  assert.equal(dismissed, 1);
  assert.equal(escape.defaultPrevented, true);
  assert.equal(escape.propagationStopped, true);

  cleanup();
  assert.equal(keydownHandler, null);
  assert.equal(documentRef.activeElement, previous);
  assert.equal(previous.focusCount, 1);
});

test('Tab and Shift+Tab stay inside the wizard dialog', () => {
  const documentRef = { activeElement: null };
  const first = createFocusable(documentRef, 'first');
  const middle = createFocusable(documentRef, 'middle');
  const last = createFocusable(documentRef, 'last');
  const items = [first, middle, last];
  const card = createFocusable(documentRef, 'card');
  card.querySelectorAll = () => items;
  card.contains = (element) => items.includes(element);
  const resolveStyle = (element) => element.style;

  documentRef.activeElement = last;
  const forward = createKeyboardEvent('Tab');
  assert.equal(trapWizardTab(forward, card, documentRef.activeElement, resolveStyle), true);
  assert.equal(documentRef.activeElement, first);
  assert.equal(forward.defaultPrevented, true);
  assert.equal(forward.propagationStopped, true);

  const backward = createKeyboardEvent('Tab', true);
  assert.equal(trapWizardTab(backward, card, documentRef.activeElement, resolveStyle), true);
  assert.equal(documentRef.activeElement, last);
  assert.equal(backward.defaultPrevented, true);

  documentRef.activeElement = middle;
  const internal = createKeyboardEvent('Tab');
  assert.equal(trapWizardTab(internal, card, documentRef.activeElement, resolveStyle), false);
  assert.equal(internal.defaultPrevented, false);
  assert.equal(internal.propagationStopped, true);

  const outside = createFocusable(documentRef, 'outside');
  documentRef.activeElement = outside;
  const reenter = createKeyboardEvent('Tab', true);
  assert.equal(trapWizardTab(reenter, card, documentRef.activeElement, resolveStyle), true);
  assert.equal(documentRef.activeElement, last);
});

test('dismissing the wizard records the seen marker before closing', () => {
  const values = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
  let markerAtClose = null;

  dismissWizardAsSeen(() => {
    markerAtClose = values.get('mw_wizard_seen');
  });

  assert.equal(markerAtClose, '1');
  delete globalThis.sessionStorage;
});
