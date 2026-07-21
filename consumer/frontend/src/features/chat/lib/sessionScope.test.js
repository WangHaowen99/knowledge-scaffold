import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createSessionScope,
  isCurrentSessionRead,
  isSameSessionScope,
  listResumableSessions,
  mergeSessionCollections,
  resolveSessionRunIndicator,
  selectedSessionStorageKey,
  shouldCommitSessionSelection,
} from './sessionScope.js';

test('workspace and selection epochs reject stale session responses', () => {
  const requestScope = createSessionScope('alpha', 2);
  assert.equal(isSameSessionScope(requestScope, createSessionScope('alpha', 2)), true);
  assert.equal(isSameSessionScope(requestScope, createSessionScope('beta', 3)), false);
  assert.equal(shouldCommitSessionSelection({
    requestScope,
    currentScope: createSessionScope('alpha', 2),
    requestSelectionEpoch: 4,
    currentSelectionEpoch: 5,
  }), false);
  assert.equal(shouldCommitSessionSelection({
    requestScope,
    currentScope: createSessionScope('alpha', 2),
    requestSelectionEpoch: 5,
    currentSelectionEpoch: 5,
  }), true);
});

test('session reads are rejected after a newer read, mutation or deletion', () => {
  const base = {
    requestScope: createSessionScope('alpha', 1),
    currentScope: createSessionScope('alpha', 1),
    requestSequence: 3,
    currentSequence: 3,
    requestMutationEpoch: 2,
    currentMutationEpoch: 2,
  };
  assert.equal(isCurrentSessionRead(base), true);
  assert.equal(isCurrentSessionRead({ ...base, currentSequence: 4 }), false);
  assert.equal(isCurrentSessionRead({ ...base, currentMutationEpoch: 3 }), false);
  assert.equal(isCurrentSessionRead({ ...base, deleted: true }), false);
});

test('stale list responses preserve local creates and omit committed deletes', () => {
  assert.deepEqual(mergeSessionCollections(
    [
      { id: 'old', title: 'stale server title', metadata: { pinned: false } },
      { id: 'deleted', title: 'deleted' },
    ],
    [
      { id: 'old', title: 'local title', metadata: { pinned: true } },
      { id: 'created', title: 'created locally' },
    ],
    ['deleted'],
  ), [
    { id: 'old', title: 'local title', metadata: { pinned: true } },
    { id: 'created', title: 'created locally', metadata: {} },
  ]);
});

test('selected-session storage is isolated by workspace', () => {
  assert.equal(selectedSessionStorageKey('alpha'), 'onto_agent_current_session_alpha');
  assert.equal(selectedSessionStorageKey('beta'), 'onto_agent_current_session_beta');
});

test('all queued and running sessions are selected for background resume', () => {
  assert.deepEqual(listResumableSessions([
    { id: 'queued', active_run_id: 'run-q', active_run_status: 'queued', last_seq: 3 },
    { id: 'running', active_run_id: 'run-r', active_run_status: 'running', last_seq: '8' },
    { id: 'waiting', active_run_id: 'run-w', active_run_status: 'awaiting_input', last_seq: 12 },
    { id: 'idle', active_run_status: 'idle' },
  ]), [
    { sessionId: 'queued', runId: 'run-q', status: 'queued', afterSequence: 3 },
    { sessionId: 'running', runId: 'run-r', status: 'running', afterSequence: 8 },
  ]);
});

test('session run indicators prefer live stream state and preserve awaiting input', () => {
  assert.equal(resolveSessionRunIndicator({ active_run_status: 'queued' }), 'queued');
  assert.equal(resolveSessionRunIndicator({ active_run_status: 'running' }), 'running');
  assert.equal(resolveSessionRunIndicator({ active_run_status: 'awaiting_input' }), 'awaiting_input');
  assert.equal(resolveSessionRunIndicator({ active_run_status: 'awaiting' }), 'awaiting_input');
  assert.equal(resolveSessionRunIndicator(
    { active_run_status: 'idle' },
    { isLoading: true, status: 'queued' },
  ), 'queued');
  for (const status of ['streaming', 'reconnecting', 'finalizing']) {
    assert.equal(resolveSessionRunIndicator(
      { active_run_status: 'idle' },
      { isLoading: true, status },
    ), 'running');
  }
  for (const status of ['awaiting', 'awaiting_input']) {
    assert.equal(resolveSessionRunIndicator(
      { active_run_status: 'running' },
      { isLoading: false, status },
    ), 'awaiting_input');
  }
  for (const status of ['completed', 'failed', 'cancelled']) {
    assert.equal(resolveSessionRunIndicator(
      { active_run_status: 'running' },
      { isLoading: false, status },
    ), '');
  }
});
