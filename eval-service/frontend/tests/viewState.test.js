import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readView } from '../src/viewState.js';
import { shortId } from '../src/domain.js';

test('history navigation restores tab filters and offset from the URL', () => {
  globalThis.location = { hash: '#/runs/run_demo?tab=history&history_type=solver&history_offset=20&search=synthetic' };
  assert.equal(readView('tab', 'cases'), 'history');
  assert.equal(readView('history_type', 'all'), 'solver');
  assert.equal(readView('history_offset', 0), 20);
  assert.equal(readView('search', ''), 'synthetic');
  location.hash = '#/runs/run_demo?history_offset=-2';
  assert.equal(readView('history_offset', 0), 0);
  assert.equal(readView('tab', 'cases'), 'cases');
  delete globalThis.location;
});

test('visible record IDs extend when short prefixes collide', () => {
  const first = 'aaaaaaaaaaaabbbb', second = 'aaaaaaaaaaaacccc';
  assert.notEqual(shortId(first, [first, second]), shortId(second, [first, second]));
  assert.equal(shortId(first), 'aaaaaaaaaaaa');
});
