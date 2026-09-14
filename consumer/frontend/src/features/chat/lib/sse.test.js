// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import { parseSSEMessage, takeSSEFrames } from './sse.js';

test('SSE parser preserves event id and joins multiline JSON data', () => {
  assert.deepEqual(parseSSEMessage([
    ': heartbeat',
    'id: 17',
    'event: token.delta',
    'data: {"schema_version":"1.0",',
    'data: "event_type":"token.delta","seq":17}',
  ].join('\n')), {
    event: 'token.delta',
    id: '17',
    data: { schema_version: '1.0', event_type: 'token.delta', seq: 17 },
  });
});

test('SSE frame extraction keeps partial data and flushes an EOF tail', () => {
  const partial = takeSSEFrames('event: one\ndata: {"n":1}\n\nevent: two\ndata: {');
  assert.equal(partial.frames.length, 1);
  assert.equal(partial.remainder, 'event: two\ndata: {');

  const complete = takeSSEFrames(`${partial.remainder}"n":2}`, { done: true });
  assert.equal(complete.frames.length, 1);
  assert.equal(complete.remainder, '');
  assert.deepEqual(parseSSEMessage(complete.frames[0]), {
    event: 'two',
    id: '',
    data: { n: 2 },
  });
});

test('SSE parser ignores done sentinels and malformed payloads', () => {
  assert.equal(parseSSEMessage('data: [DONE]'), null);
  assert.equal(parseSSEMessage('data: {broken'), null);
});
