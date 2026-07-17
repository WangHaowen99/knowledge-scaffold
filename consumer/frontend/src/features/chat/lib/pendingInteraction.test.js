import assert from 'node:assert/strict';
import test from 'node:test';

import { findPendingInteraction } from './pendingInteraction.js';

const interaction = { id: 'interaction-1', type: 'single_choice' };

test('returns the latest unresolved interaction', () => {
  assert.equal(
    findPendingInteraction([
      { role: 'user', content: '请选择' },
      { role: 'assistant', status: 'awaiting', interaction },
    ]),
    interaction,
  );
});

test('does not reuse an interaction after the user has responded', () => {
  assert.equal(
    findPendingInteraction([
      { role: 'assistant', status: 'awaiting', interaction },
      { role: 'user', content: 'F001、F002、F003' },
    ]),
    null,
  );
});

test('does not expose a completed historical interaction', () => {
  assert.equal(
    findPendingInteraction([
      { role: 'assistant', status: 'awaiting', interaction },
      { role: 'user', content: 'F001、F002、F003' },
      { role: 'assistant', status: 'finished', content: '累计成本为 900' },
    ]),
    null,
  );
});
