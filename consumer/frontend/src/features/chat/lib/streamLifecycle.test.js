import assert from 'node:assert/strict';
import test from 'node:test';

import {
  abortStreamController,
  abortStreamControllers,
  isSilentStreamAbort,
  isTerminalRunStatus,
  messageStatusForTerminalRun,
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

test('terminal run outcomes keep their message semantics after a 410 resume', () => {
  assert.equal(messageStatusForTerminalRun('completed'), 'finished');
  assert.equal(messageStatusForTerminalRun('failed'), 'error');
  assert.equal(messageStatusForTerminalRun('cancelled'), 'cancelled');
  assert.equal(messageStatusForTerminalRun('awaiting_input'), 'awaiting');
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

test('run terminal detection covers completed and awaiting outcomes', () => {
  for (const status of ['success', 'completed', 'error', 'cancelled', 'awaiting_input']) {
    assert.equal(isTerminalRunStatus(status), true);
  }
  assert.equal(isTerminalRunStatus('running'), false);
  assert.equal(isTerminalRunStatus('queued'), false);
});

test('scope disconnect aborts controllers silently while user abort remains visible', () => {
  const silent = new AbortController();
  const visible = new AbortController();
  abortStreamControllers(new Map([['scope', silent]]), { silent: true });
  abortStreamController(visible);

  assert.equal(silent.signal.aborted, true);
  assert.equal(visible.signal.aborted, true);
  assert.equal(isSilentStreamAbort(silent), true);
  assert.equal(isSilentStreamAbort(visible), false);
});
