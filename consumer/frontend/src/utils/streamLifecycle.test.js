import assert from 'node:assert/strict';
import test from 'node:test';

import {
  streamStatusForAdmission,
  streamStatusForConnection,
  streamStatusForError,
  streamStatusForTerminal,
} from './streamLifecycle.js';

test('stream lifecycle distinguishes queued, running and reconnecting transport', () => {
  assert.equal(streamStatusForAdmission('queued'), 'queued');
  assert.equal(streamStatusForAdmission('running'), 'reconnecting');
  assert.equal(streamStatusForConnection(0), 'streaming');
  assert.equal(streamStatusForConnection(2), 'reconnecting');
});

test('stream lifecycle converges all terminal run outcomes', () => {
  assert.equal(streamStatusForTerminal('completed'), 'finished');
  assert.equal(streamStatusForTerminal('success'), 'finished');
  assert.equal(streamStatusForTerminal('awaiting_input'), 'awaiting');
  assert.equal(streamStatusForTerminal('cancelled'), 'cancelled');
  assert.equal(streamStatusForTerminal('error'), 'error');
  assert.equal(streamStatusForError({ name: 'AbortError' }), 'cancelled');
  assert.equal(streamStatusForError(new Error('network')), 'error');
});
