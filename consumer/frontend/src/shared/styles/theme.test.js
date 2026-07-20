import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';

const values = new Map();
let systemPrefersDark = true;
let reduceMotion = false;
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
};
globalThis.document = { documentElement: { dataset: {} } };
globalThis.window = {
  matchMedia: (query) => ({
    matches: query === '(prefers-reduced-motion: reduce)' ? reduceMotion : systemPrefersDark,
  }),
};

const { applyAppearance, getAppearance } = await import('./theme.js');

beforeEach(() => {
  values.clear();
  document.documentElement.dataset = {};
  delete document.startViewTransition;
  systemPrefersDark = true;
  reduceMotion = false;
});

test('dark appearance persists and updates the document mode', () => {
  assert.equal(applyAppearance('dark'), 'dark');
  assert.equal(getAppearance(), 'dark');
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
});

test('system appearance resolves through the media query', () => {
  assert.equal(applyAppearance('system'), 'system');
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');

  systemPrefersDark = false;
  assert.equal(applyAppearance(getAppearance()), 'system');
  assert.equal(document.documentElement.dataset.mwrMode, 'light');
});

test('explicit dark appearance is not overridden by a light system preference', () => {
  applyAppearance('dark');
  systemPrefersDark = false;

  assert.equal(applyAppearance(getAppearance()), 'dark');
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
});

test('explicit light appearance is not overridden by a dark system preference', () => {
  applyAppearance('light');
  systemPrefersDark = true;

  assert.equal(applyAppearance(getAppearance()), 'light');
  assert.equal(document.documentElement.dataset.mwrMode, 'light');
});

test('animated appearance changes use a view transition when available', async () => {
  let transitionCalls = 0;
  document.documentElement.dataset.mwrMode = 'light';
  document.startViewTransition = (update) => {
    transitionCalls += 1;
    update();
    return { finished: Promise.resolve() };
  };

  assert.equal(applyAppearance('dark', { animate: true }), 'dark');
  assert.equal(transitionCalls, 1);
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
  assert.equal(document.documentElement.dataset.mwrAppearanceTransition, 'view');
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(document.documentElement.dataset.mwrAppearanceTransition, undefined);
});

test('animated appearance changes fall back to scoped color transitions', () => {
  document.documentElement.dataset.mwrMode = 'light';

  assert.equal(applyAppearance('dark', { animate: true }), 'dark');
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
  assert.equal(document.documentElement.dataset.mwrAppearanceTransition, 'fallback');
});

test('reduced motion applies the appearance without starting a transition', () => {
  let transitionCalls = 0;
  reduceMotion = true;
  document.documentElement.dataset.mwrMode = 'light';
  document.startViewTransition = () => {
    transitionCalls += 1;
    return { finished: Promise.resolve() };
  };

  assert.equal(applyAppearance('dark', { animate: true }), 'dark');
  assert.equal(transitionCalls, 0);
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
  assert.equal(document.documentElement.dataset.mwrAppearanceTransition, undefined);
});
