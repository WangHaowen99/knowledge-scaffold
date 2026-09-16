// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  decodePublicEventFrame,
  isPublicEvent,
  PublicEventProtocolError,
  publicEventTimestamp,
} from './publicEvent.js';

const event = (overrides = {}) => ({
  schema_version: '1.0',
  event_id: 'run-public:1',
  run_id: 'run-public',
  session_id: 'session-public',
  turn_id: 1,
  seq: 1,
  event_type: 'token.delta',
  status: 'running',
  source: { node: 'agent' },
  payload: { text: 'synthetic output' },
  created_at: '2026-08-23T00:00:00.000Z',
  ...overrides,
});

test('valid PublicEvent frame keeps the canonical payload unchanged', () => {
  const payload = event();
  const decoded = decodePublicEventFrame(
    { event: 'token.delta', id: '1', data: payload },
    { expectedRunId: 'run-public', expectedSessionId: 'session-public' },
  );

  assert.equal(decoded, payload);
  assert.equal(isPublicEvent(decoded), true);
  assert.equal(publicEventTimestamp(decoded), Date.parse(payload.created_at));
});

test('PublicEvent frame rejects mismatched SSE event names and ids', () => {
  assert.throws(
    () => decodePublicEventFrame({ event: 'tool.started', id: '1', data: event() }),
    PublicEventProtocolError,
  );
  assert.throws(
    () => decodePublicEventFrame({ event: 'token.delta', id: '2', data: event() }),
    PublicEventProtocolError,
  );
});

test('PublicEvent frame rejects events from another active run', () => {
  assert.throws(
    () => decodePublicEventFrame(
      { event: 'token.delta', id: '1', data: event() },
      { expectedRunId: 'run-other' },
    ),
    /active run/,
  );
});

test('turn.finished requires a canonical terminal status', () => {
  assert.equal(isPublicEvent(event({ event_type: 'turn.finished', status: 'running' })), false);
  assert.equal(isPublicEvent(event({
    event_type: 'turn.finished',
    status: 'completed',
    payload: { status: 'completed' },
  })), true);
  assert.equal(isPublicEvent(event({
    event_type: 'turn.finished',
    status: 'completed',
    payload: { status: 'failed' },
  })), false);
});

test('unknown PublicEvent types remain forward compatible while legacy envelopes are rejected', () => {
  assert.equal(isPublicEvent(event({ event_type: 'runtime.extension' })), true);
  assert.equal(isPublicEvent({
    protocol: 'agent-stream.v2',
    type: 'token.delta',
    payload: {},
  }), false);
});

test('PublicEvent rejects each invalid required and optional field category', () => {
  const invalidEvents = [
    null,
    event({ schema_version: '2.0' }),
    event({ event_id: '' }),
    event({ run_id: '' }),
    event({ seq: 0 }),
    event({ event_type: '' }),
    event({ status: 'unknown' }),
    event({ payload: [] }),
    event({ source: [] }),
    event({ span_key: 1 }),
    event({ session_id: 1 }),
    event({ turn_id: 0 }),
    event({ created_at: 'invalid-date' }),
  ];

  assert.equal(invalidEvents.every((value) => !isPublicEvent(value)), true);
});
