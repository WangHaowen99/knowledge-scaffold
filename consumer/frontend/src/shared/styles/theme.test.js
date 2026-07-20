import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';

const values = new Map();
let systemPrefersDark = true;
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
};
globalThis.document = { documentElement: { dataset: {} } };
globalThis.window = { matchMedia: () => ({ matches: systemPrefersDark }) };

const { applyAppearance, getAppearance } = await import('./theme.js');

beforeEach(() => {
  values.clear();
  document.documentElement.dataset = {};
  systemPrefersDark = true;
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
