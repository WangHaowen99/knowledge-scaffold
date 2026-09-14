// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  INTERACTION_CANCELLED_NOTICE,
  applyInteractionCancellation,
  findPendingInteraction,
} from './pendingInteraction.js';

const interaction = { id: 'interaction-1', type: 'single_choice' };

test('returns the latest unresolved interaction', () => {
  assert.equal(
    findPendingInteraction([
      { role: 'user', content: '请选择' },
      { role: 'assistant', status: 'awaiting', interaction },
    ]),
    interaction,
  );
});

test('does not reuse an interaction after the user has responded', () => {
  assert.equal(
    findPendingInteraction([
      { role: 'assistant', status: 'awaiting', interaction },
      { role: 'user', content: 'F001、F002、F003' },
    ]),
    null,
  );
});

test('preserves the run that owns a pending interaction', () => {
  const pending = findPendingInteraction([
    {
      role: 'assistant',
      runId: 'run-pending-interaction',
      status: 'awaiting',
      interaction,
    },
  ]);

  assert.equal(pending.id, interaction.id);
  assert.equal(pending.runId, 'run-pending-interaction');
});

test('does not expose a completed historical interaction', () => {
  assert.equal(
    findPendingInteraction([
      { role: 'assistant', status: 'awaiting', interaction },
      { role: 'user', content: 'F001、F002、F003' },
      { role: 'assistant', status: 'finished', content: '累计成本为 900' },
    ]),
    null,
  );
});

test('cancelling an interaction restores normal input and records a system notice', () => {
  const messages = [
    { role: 'user', content: '开始分析' },
    { role: 'assistant', status: 'awaiting', interaction },
  ];

  const cancelled = applyInteractionCancellation(messages, {
    interaction_id: interaction.id,
    cancelled_at: '2026-07-17T10:00:00',
  });

  assert.equal(cancelled[1].status, 'cancelled');
  assert.equal(cancelled[1].isLoading, false);
  assert.equal(cancelled[2].role, 'system');
  assert.equal(cancelled[2].content, INTERACTION_CANCELLED_NOTICE);
  assert.equal(cancelled[2].metadata.interaction_id, interaction.id);
  assert.equal(findPendingInteraction(cancelled), null);
  assert.equal(messages[1].status, 'awaiting');
});

test('applying the same interaction cancellation twice is idempotent', () => {
  const once = applyInteractionCancellation([
    { role: 'assistant', status: 'awaiting', interaction },
  ], { interaction_id: interaction.id });
  const twice = applyInteractionCancellation(once, { interaction_id: interaction.id });

  assert.equal(twice, once);
  assert.equal(twice.filter((message) => message.metadata?.type === 'interaction_cancelled').length, 1);
});
