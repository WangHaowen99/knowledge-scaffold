import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildRunCancelUrl,
  buildRunEventsUrl,
  buildRunUrl,
  classifyPublicEvent,
  normalizeSequence,
} from './streamResume.js';

test('normalizes cursor values', () => {
  assert.equal(normalizeSequence('12'), 12);
  assert.equal(normalizeSequence('-1'), 0);
});

test('rejects replayed PublicEvents by cursor or stable event id', () => {
  const seen = new Set(['run-a:5']);
  assert.equal(classifyPublicEvent({ event_id: 'run-a:4', seq: 4 }, 4, seen).duplicate, true);
  assert.equal(classifyPublicEvent({ event_id: 'run-a:5', seq: 5 }, 0, seen).duplicate, true);

  const fresh = classifyPublicEvent({ event_id: 'run-a:6', seq: 6 }, 4, seen);
  assert.equal(fresh.duplicate, false);
  assert.equal(fresh.nextCursor, 6);
});

test('builds an encoded resumable event stream URL', () => {
  assert.equal(buildRunEventsUrl('run/with space', 9), '/agentic/runs/run%2Fwith%20space/events?after_seq=9');
  assert.equal(buildRunEventsUrl('run-a'), '/agentic/runs/run-a/events?after_seq=0');
  assert.equal(buildRunUrl('run/with space'), '/agentic/runs/run%2Fwith%20space');
  assert.equal(buildRunCancelUrl('run/with space'), '/agentic/runs/run%2Fwith%20space/cancel');
});
