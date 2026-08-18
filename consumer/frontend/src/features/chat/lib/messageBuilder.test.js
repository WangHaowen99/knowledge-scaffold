import assert from 'node:assert/strict';
import test from 'node:test';

import { buildMessagesFromEvents, historyToMessages } from './messageBuilder.js';
import { AGENT_STREAM_EVENT, AGENT_STREAM_PROTOCOL } from './streamHelpers.js';

test('durable awaiting-input event restores interaction after page refresh', () => {
  const messages = buildMessagesFromEvents([{
    event: AGENT_STREAM_EVENT,
    data: {
      protocol: AGENT_STREAM_PROTOCOL,
      type: 'turn.finished',
      run_id: 'run-awaiting',
      seq: 8,
      ts: 1008,
      payload: {
        status: 'awaiting_input',
        final_output: {
          type: 'user_interaction',
          text: '请选择审批结果',
          options: [{ id: 'approve', label: '通过', value: 'approve' }],
          interaction: { id: 'interaction-1', type: 'single_choice' },
        },
      },
    },
  }]);

  assert.equal(messages.length, 1);
  assert.equal(messages[0].status, 'awaiting');
  assert.equal(messages[0].interaction.id, 'interaction-1');
  assert.equal(messages[0].options[0].id, 'approve');
  assert.equal(messages[0].isLoading, false);
});

test('run.started does not create a phantom turn during replay', () => {
  const envelope = (type, turnId, seq, payload = {}) => ({
    event: AGENT_STREAM_EVENT,
    data: {
      protocol: AGENT_STREAM_PROTOCOL,
      type,
      run_id: 'run-existing-session',
      turn_id: turnId,
      seq,
      ts: 1000 + seq,
      payload,
    },
  });

  const messages = buildMessagesFromEvents([
    envelope('run.started', 1, 1, { status: 'running' }),
    envelope('turn.started', 3, 2, { turn_id: 3 }),
    envelope('turn.finished', 3, 3, {
      status: 'completed',
      content: 'synthetic answer',
    }),
  ]);

  assert.equal(messages.length, 1);
  assert.equal(messages[0].turnId, '3');
  assert.equal(messages[0].status, 'finished');
});

test('successive interactions in one run restore as separate turn bubbles', () => {
  const waitingOutput = (interactionId, text) => ({
    status: 'awaiting_input',
    final_output: {
      type: 'user_interaction',
      text,
      interaction: { id: interactionId, type: 'input', prompt: text },
    },
  });
  const messages = buildMessagesFromEvents([
    {
      event: AGENT_STREAM_EVENT,
      data: {
        protocol: AGENT_STREAM_PROTOCOL,
        type: 'turn.finished',
        run_id: 'run-successive-interactions',
        turn_id: 1,
        seq: 3,
        payload: waitingOutput('interaction-first', 'Provide the first synthetic value'),
      },
    },
    {
      event: 'chat_message',
      data: { role: 'user', content: 'synthetic response', metadata: {} },
    },
    {
      event: AGENT_STREAM_EVENT,
      data: {
        protocol: AGENT_STREAM_PROTOCOL,
        type: 'turn.finished',
        run_id: 'run-successive-interactions',
        turn_id: 2,
        seq: 7,
        payload: waitingOutput('interaction-second', 'Provide the second synthetic value'),
      },
    },
  ]);

  assert.equal(messages.length, 3);
  assert.equal(messages[0].turnId, '1');
  assert.equal(messages[0].interaction.id, 'interaction-first');
  assert.equal(messages[1].role, 'user');
  assert.equal(messages[2].turnId, '2');
  assert.equal(messages[2].interaction.id, 'interaction-second');
  assert.equal(messages[2].status, 'awaiting');
});

test('durable cancellation marker clears an awaiting interaction after refresh', () => {
  const messages = buildMessagesFromEvents([
    {
      event: AGENT_STREAM_EVENT,
      data: {
        protocol: AGENT_STREAM_PROTOCOL,
        type: 'turn.finished',
        run_id: 'run-awaiting',
        seq: 8,
        ts: 1008,
        payload: {
          status: 'awaiting_input',
          final_output: {
            interaction: { id: 'interaction-1', type: 'input' },
          },
        },
      },
    },
    {
      event: 'chat_message',
      data: {
        role: 'system',
        content: '用户取消了操作',
        metadata: {
          type: 'interaction_cancelled',
          interaction_id: 'interaction-1',
          cancelled_at: '2026-07-17T10:00:00',
        },
      },
    },
  ]);

  assert.equal(messages.length, 2);
  assert.equal(messages[0].status, 'cancelled');
  assert.equal(messages[0].isLoading, false);
  assert.equal(messages[1].role, 'system');
  assert.equal(messages[1].content, '用户取消了操作');
  assert.equal(messages[1].metadata.type, 'interaction_cancelled');
});

test('history restoration preserves run and session-lock metadata', () => {
  const [message] = historyToMessages([{
    id: 'message-1',
    role: 'user',
    content: '分析数据',
    metadata: {
      run_id: 'run-1',
      model_profile_id: 'model-1',
      selected_ontology_id: 'ontology-1',
      agent_mode: 'thinking',
    },
  }]);

  assert.equal(message.runId, 'run-1');
  assert.equal(message.metadata.model_profile_id, 'model-1');
  assert.equal(message.metadata.selected_ontology_id, 'ontology-1');
  assert.equal(message.metadata.agent_mode, 'thinking');
});
