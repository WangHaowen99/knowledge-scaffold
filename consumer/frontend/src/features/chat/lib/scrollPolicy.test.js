import assert from 'node:assert/strict';
import test from 'node:test';

import { distanceFromScrollBottom, isNearScrollBottom } from './scrollPolicy.js';

test('scroll distance is clamped and near-bottom uses a stable threshold', () => {
  assert.equal(distanceFromScrollBottom({ scrollTop: 730, scrollHeight: 1000, clientHeight: 200 }), 70);
  assert.equal(isNearScrollBottom({ scrollTop: 730, scrollHeight: 1000, clientHeight: 200 }), true);
  assert.equal(isNearScrollBottom({ scrollTop: 600, scrollHeight: 1000, clientHeight: 200 }), false);
  assert.equal(distanceFromScrollBottom({ scrollTop: 900, scrollHeight: 1000, clientHeight: 200 }), 0);
});

test('invalid scroll measurements never force follow mode', () => {
  assert.equal(isNearScrollBottom({ scrollTop: 'invalid', scrollHeight: 1000, clientHeight: 200 }), false);
});
