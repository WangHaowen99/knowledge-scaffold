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
