import assert from 'node:assert/strict';
import test from 'node:test';

import {
  beginSessionPinUpdate,
  commitSessionPinUpdate,
  isSessionResumableStatus,
  isSessionWaitingForUserStatus,
  resolveSessionPreferences,
  rollbackSessionPinUpdate,
  sortSessions,
} from './sessionModel.js';

const sessions = [
  { id: 'older', updated_at: '2026-07-13T08:00:00Z', metadata: {} },
  { id: 'newer', updated_at: '2026-07-14T08:00:00Z', metadata: {} },
];

test('sessions sort pinned first and then by newest activity', () => {
  assert.deepEqual(sortSessions(sessions).map((session) => session.id), ['newer', 'older']);
  const pinned = beginSessionPinUpdate(sessions, 'older', true, '2026-07-15T08:00:00Z');
  assert.deepEqual(pinned.optimistic.map((session) => session.id), ['older', 'newer']);
  assert.equal(pinned.optimistic[0].metadata.pinned_at, '2026-07-15T08:00:00Z');
});

test('session pin transaction commits server metadata and rolls back only its target', () => {
  const transaction = beginSessionPinUpdate(sessions, 'older', true, '2026-07-15T08:00:00Z');
  const withConcurrentTitle = transaction.optimistic.map((session) => (
    session.id === 'newer' ? { ...session, title: '并发更新' } : session
  ));

  const committed = commitSessionPinUpdate(withConcurrentTitle, 'older', {
    id: 'older',
    metadata: { pinned: true, pinned_at: 'server-time' },
  });
  assert.equal(committed[0].metadata.pinned_at, 'server-time');

  const rolledBack = rollbackSessionPinUpdate(withConcurrentTitle, transaction);
  assert.equal(rolledBack.find((session) => session.id === 'older').metadata.pinned, undefined);
  assert.equal(rolledBack.find((session) => session.id === 'newer').title, '并发更新');
});

test('session metadata restores locked model, ontology and agent mode before local defaults', () => {
  assert.deepEqual(resolveSessionPreferences({
    metadata: {
      agent_mode: 'multi_agent',
      model_profile_id: 'server-model',
      selected_ontology_id: 'server-ontology',
      selected_ontology_version: '2.0.0',
    },
    storedAgentMode: 'quick',
    storedModelProfileId: 'local-model',
    storedOntologyId: 'local-ontology',
    storedOntologyVersion: '1.0.0',
    defaultModelProfileId: 'default-model',
  }), {
    agentMode: 'multi_agent',
    modelProfileId: 'server-model',
    selectedOntologyId: 'server-ontology',
    selectedOntologyVersion: '2.0.0',
  });
});

test('session status separates resumable transport from awaiting interaction', () => {
  assert.equal(isSessionResumableStatus(' queued '), true);
  assert.equal(isSessionResumableStatus('running'), true);
  assert.equal(isSessionResumableStatus('completed'), false);
  assert.equal(isSessionWaitingForUserStatus('AWAITING_INPUT'), true);
  assert.equal(isSessionWaitingForUserStatus('awaiting'), true);
  assert.equal(isSessionWaitingForUserStatus('running'), false);
});
