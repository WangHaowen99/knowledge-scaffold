import assert from 'node:assert/strict';
import test from 'node:test';

import { can } from './permissions.js';

test('global observability remains available to administrators', () => {
  assert.equal(can({ role: 'admin' }, 'observability.global'), true);
  assert.equal(can({ role: 'user' }, 'observability.global'), false);
});

test('removed workspace connection permissions stay unavailable', () => {
  assert.equal(can({ role: 'admin' }, 'ws.connections.write'), false);
  assert.equal(can({ role: 'admin' }, 'ws.connections.test'), false);
});
