import test from 'node:test';
import assert from 'node:assert/strict';

const values = new Map();
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
};
globalThis.document = { documentElement: { dataset: {} } };
globalThis.window = { matchMedia: () => ({ matches: true }) };

const { applyAppearance, getAppearance } = await import('./theme.js');

test('dark appearance persists and updates the document mode', () => {
  assert.equal(applyAppearance('dark'), 'dark');
  assert.equal(getAppearance(), 'dark');
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
});

test('system appearance resolves through the media query', () => {
  assert.equal(applyAppearance('system'), 'system');
  assert.equal(document.documentElement.dataset.mwrMode, 'dark');
});
