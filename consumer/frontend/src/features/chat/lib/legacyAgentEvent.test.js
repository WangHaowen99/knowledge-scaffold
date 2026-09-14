// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  legacyAgentEventToPublicEvent,
  LEGACY_AGENT_STREAM_EVENT,
  LEGACY_AGENT_STREAM_PROTOCOL,
} from './legacyAgentEvent.js';
import { isPublicEvent } from './publicEvent.js';

test('stored agent.v2 terminal events are normalized only at the hydration boundary', () => {
  const event = legacyAgentEventToPublicEvent({
    event: LEGACY_AGENT_STREAM_EVENT,
    data: {
      protocol: LEGACY_AGENT_STREAM_PROTOCOL,
      event_id: 'run-history:4',
      run_id: 'run-history',
      turn_id: 2,
      seq: 4,
      ts: 1004,
      type: 'turn.finished',
      source: { node: 'agent' },
      payload: { status: 'success', content: 'synthetic answer' },
    },
  });

  assert.equal(isPublicEvent(event), true);
  assert.equal(event.event_type, 'turn.finished');
  assert.equal(event.status, 'completed');
  assert.equal(event.payload.status, 'completed');
  assert.equal(Date.parse(event.created_at), 1004);
});

test('legacy events with missing identities receive deterministic hydration identities', () => {
  const event = legacyAgentEventToPublicEvent({
    protocol: LEGACY_AGENT_STREAM_PROTOCOL,
    run_id: 'run-history',
    type: 'token.delta',
    payload: { text: 'synthetic token' },
  }, { index: 2, now: 2000 });

  assert.equal(event.seq, 3);
  assert.equal(event.event_id, 'run-history:legacy:3');
  assert.equal(event.status, 'running');
  assert.equal(Date.parse(event.created_at), 2000);
});
