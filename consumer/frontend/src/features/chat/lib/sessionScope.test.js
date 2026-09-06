import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createSessionScope,
  isCurrentSessionRead,
  isSameSessionScope,
  listResumableSessions,
  mergeSessionPage,
  mergeSessionCollections,
  normalizeSessionPage,
  resolveInitialSessionSelection,
  resolveSessionRunIndicator,
  selectedSessionStorageKey,
  shouldCommitSessionSelection,
} from './sessionScope.js';

test('session page normalization is strict and preserves opaque metadata', () => {
  assert.deepEqual(normalizeSessionPage({
    sessions: [{ id: 'one' }],
    next_cursor: 'opaque-cursor',
    has_more: true,
    index_revision: 7,
  }), {
    sessions: [{ id: 'one' }],
    nextCursor: 'opaque-cursor',
    hasMore: true,
    indexRevision: 7,
  });
  assert.throws(() => normalizeSessionPage({ sessions: null }), /sessions/);
  assert.throws(() => normalizeSessionPage({ sessions: [], has_more: true }), /cursor/);
});

test('later session pages merge without duplicates and retain selected session', () => {
  assert.deepEqual(mergeSessionPage({
    current: [
      { id: 'selected', title: 'Selected' },
      { id: 'two', title: 'Old two' },
    ],
    incoming: [
      { id: 'two', title: 'Fresh two' },
      { id: 'three', title: 'Three' },
    ],
    append: true,
    selectedId: 'selected',
  }), [
    { id: 'selected', title: 'Selected' },
    { id: 'two', title: 'Fresh two', metadata: {} },
    { id: 'three', title: 'Three' },
  ]);
  assert.deepEqual(mergeSessionPage({
    current: [{ id: 'selected', title: 'Selected' }, { id: 'old' }],
    incoming: [{ id: 'new' }],
    append: false,
    selectedId: 'selected',
  }), [{ id: 'new' }, { id: 'selected', title: 'Selected' }]);
});

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

test('persisted selection remains authoritative when it is beyond the first page', () => {
  assert.deepEqual(resolveInitialSessionSelection({
    persistedId: 'session-0051',
    currentId: null,
    sessions: [{ id: 'session-0001' }, { id: 'session-0002' }],
    selectLatest: false,
  }), {
    sessionId: 'session-0051',
    requiresDirectHydration: true,
  });
  assert.deepEqual(resolveInitialSessionSelection({
    persistedId: 'missing',
    currentId: null,
    sessions: [{ id: 'latest' }],
    selectLatest: true,
  }), {
    sessionId: 'latest',
    requiresDirectHydration: false,
  });
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
