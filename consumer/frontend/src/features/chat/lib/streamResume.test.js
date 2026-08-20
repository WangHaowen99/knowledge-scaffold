import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildRunCancelUrl,
  buildRunEventsUrl,
  buildRunUrl,
  classifyEnvelope,
  getEnvelopeIdentity,
  getEnvelopeSequence,
  normalizeSequence,
} from './streamResume.js';

test('normalizes cursor values and falls back to event id sequence', () => {
  assert.equal(normalizeSequence('12'), 12);
  assert.equal(normalizeSequence('-1'), 0);
  assert.equal(getEnvelopeSequence({ seq: 8, event_id: 'run-a:7' }), 8);
  assert.equal(getEnvelopeSequence({ event_id: 'run-a:7' }), 7);
  assert.equal(getEnvelopeIdentity({ run_id: 'run-a', seq: 4 }), 'run-a:4');
});

test('rejects replayed envelopes by cursor or stable event id', () => {
  const seen = new Set(['run-a:5']);
  assert.equal(classifyEnvelope({ run_id: 'run-a', seq: 4 }, 4, seen).duplicate, true);
  assert.equal(classifyEnvelope({ run_id: 'run-a', seq: 5 }, 0, seen).duplicate, true);

  const fresh = classifyEnvelope({ run_id: 'run-a', seq: 6 }, 4, seen);
  assert.equal(fresh.duplicate, false);
  assert.equal(fresh.nextCursor, 6);
});

test('builds an encoded resumable event stream URL', () => {
  assert.equal(buildRunEventsUrl('run/with space', 9), '/agentic/runs/run%2Fwith%20space/events?after_seq=9');
  assert.equal(buildRunEventsUrl('run-a'), '/agentic/runs/run-a/events?after_seq=0');
  assert.equal(buildRunUrl('run/with space'), '/agentic/runs/run%2Fwith%20space');
  assert.equal(buildRunCancelUrl('run/with space'), '/agentic/runs/run%2Fwith%20space/cancel');
});
