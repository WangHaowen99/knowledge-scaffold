import assert from 'node:assert/strict';
import test from 'node:test';

import { applyAgentV2Event, AGENT_STREAM_PROTOCOL } from './streamHelpers.js';
import { buildIndexedSegmentGroups } from './segmentGroups.js';

const envelope = (type, payload, seq = 1, trace = {}) => ({
  protocol: AGENT_STREAM_PROTOCOL,
  type,
  payload,
  seq,
  ts: 1000 + seq,
  ...trace,
});

test('replayed tool events keep one node and preserve the original parent', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  const payload = {
    tool_name: 'business_data_search',
    tool_call_id: 'call-replay',
    message: 'searching',
  };

  state = applyAgentV2Event(
    state,
    envelope('tool.started', payload, 1, {
      span_key: 'tool:main:2:business_data_search:call-replay',
      parent_key: 'iteration:main:2',
    }),
  );
  state = applyAgentV2Event(
    state,
    envelope('tool.started', payload, 1, {
      span_key: 'tool:main:2:business_data_search:call-replay',
      parent_key: 'iteration:main:999',
    }),
  );
  state = applyAgentV2Event(
    state,
    envelope('tool.finished', {
      ...payload,
      status: 'completed',
      output_preview: 'done',
    }, 2, {
      span_key: 'tool:main:2:business_data_search:call-replay',
      parent_key: 'iteration:main:999',
    }),
  );

  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].spanKey, 'tool:main:2:business_data_search:call-replay');
  assert.equal(state.streamSegments[0].parentKey, 'iteration:main:2');
  assert.equal(state.streamSegments[0].finished, true);
});

test('nested subagent groups attach under parent subagent child', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };

  [
    envelope('subagent_group.started', {
      group_id: 'group-root',
      depth: 1,
      tasks: [{ task_id: 'root-task', task: 'Root task', task_index: 0 }],
    }, 1),
    envelope('subagent.started', {
      group_id: 'group-root',
      subagent_id: 'sub-root',
      task_id: 'root-task',
      task: 'Root task',
      task_index: 0,
    }, 2),
    envelope('subagent_group.started', {
      group_id: 'group-child',
      depth: 2,
      parent_subagent_id: 'sub-root',
      tasks: [{ task_id: 'child-task', task: 'Child task', task_index: 0 }],
    }, 3),
    envelope('subagent.started', {
      group_id: 'group-child',
      subagent_id: 'sub-child',
      task_id: 'child-task',
      task: 'Child task',
      task_index: 0,
    }, 4),
    envelope('subagent.delta', {
      group_id: 'group-child',
      subagent_id: 'sub-child',
      task_id: 'child-task',
      channel: 'content',
      text: 'nested output',
    }, 5),
    envelope('subagent.finished', {
      group_id: 'group-child',
      subagent_id: 'sub-child',
      task_id: 'child-task',
      status: 'completed',
      result: { output: 'done' },
    }, 6),
    envelope('subagent_group.finished', {
      group_id: 'group-child',
      depth: 2,
      status: 'completed',
      task_count: 1,
      completed_count: 1,
      error_count: 0,
    }, 7),
  ].forEach((event) => {
    state = applyAgentV2Event(state, event);
  });

  assert.equal(state.streamSegments.length, 1);
  const rootGroup = state.streamSegments[0];
  assert.equal(rootGroup.groupId, 'group-root');
  assert.deepEqual(rootGroup.taskOrder, ['sub-root']);

  const rootChild = rootGroup.children['sub-root'];
  assert.equal(rootChild.taskId, 'root-task');
  assert.equal(rootChild.childGroups.length, 1);

  const nestedGroup = rootChild.childGroups[0];
  assert.equal(nestedGroup.groupId, 'group-child');
  assert.equal(nestedGroup.status, 'completed');
  assert.equal(nestedGroup.finished, true);
  assert.deepEqual(nestedGroup.taskOrder, ['sub-child']);

  const nestedChild = nestedGroup.children['sub-child'];
  assert.equal(nestedChild.status, 'completed');
  assert.equal(nestedChild.events[0].kind, 'token');
  assert.equal(nestedChild.events[0].text, 'nested output');
});

test('cancelled subagent tool event remains a failed terminal event', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };

  [
    envelope('subagent_group.started', {
      group_id: 'group-cancel',
      depth: 1,
      tasks: [{ task_id: 'cancel-task', task: 'Cancel task', task_index: 0 }],
    }, 1),
    envelope('subagent.started', {
      group_id: 'group-cancel',
      subagent_id: 'sub-cancel',
      task_id: 'cancel-task',
      task: 'Cancel task',
      task_index: 0,
    }, 2),
    envelope('tool.started', {
      group_id: 'group-cancel',
      subagent_id: 'sub-cancel',
      tool_name: 'slow_tool',
      tool_call_id: 'call-cancel',
    }, 3),
    envelope('tool.finished', {
      group_id: 'group-cancel',
      subagent_id: 'sub-cancel',
      tool_name: 'slow_tool',
      tool_call_id: 'call-cancel',
      status: 'cancelled',
      error: 'Workflow cancelled by user',
    }, 4),
  ].forEach((event) => {
    state = applyAgentV2Event(state, event);
  });

  const child = state.streamSegments[0].children['sub-cancel'];
  assert.equal(child.events.at(-1).status, 'error');
  assert.equal(child.events.at(-1).finished, true);
  assert.equal(child.events.at(-1).error, 'Workflow cancelled by user');
});

test('turn finished error stores provider detail in message meta', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };
  const error = '您的账户已达到速率限制，请您控制请求频率';

  state = applyAgentV2Event(
    state,
    envelope('turn.finished', { status: 'error', error }, 1),
  );

  assert.equal(state.status, 'error');
  assert.equal(state.error, true);
  assert.equal(state.isLoading, false);
  assert.equal(state.meta.error, error);
});

test('turn finished awaiting input keeps interaction state without error', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };
  const interaction = {
    id: 'interaction_call_user',
    type: 'single_choice',
    prompt: '请选择处理方式',
  };

  state = applyAgentV2Event(
    state,
    envelope('turn.finished', {
      status: 'awaiting_input',
      final_output: {
        type: 'user_interaction',
        text: '请选择处理方式',
        options: [{ id: 'a', label: '方案A', value: 'A', interaction_id: interaction.id }],
        interaction,
      },
    }, 1),
  );

  assert.equal(state.status, 'awaiting');
  assert.equal(state.error, undefined);
  assert.equal(state.isLoading, false);
  assert.equal(state.content, '请选择处理方式');
  assert.equal(state.options[0].id, 'a');
  assert.equal(state.interaction.id, 'interaction_call_user');
});

test('standalone whitespace token deltas do not create empty stream segments', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyAgentV2Event(state, envelope('token.delta', { channel: 'content', text: '\n\n' }, 1));
  state = applyAgentV2Event(state, envelope('token.delta', { channel: 'content', text: '实际内容' }, 2));

  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].text, '实际内容');
});

test('cancelled turn freezes unfinished business data search as terminal error', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyAgentV2Event(state, envelope('tool.started', {
    tool_name: 'business_data_search',
    tool_call_id: 'call-search',
    input: { entity: '客户', query: '查询客户中名称为"示例制造企业"' },
  }, 1));
  state = applyAgentV2Event(state, envelope('tool.progress', {
    tool_name: 'business_data_search',
    tool_call_id: 'call-search',
    progress: {
      business_data_search_event: {
        type: 'intermediate',
        content: '步骤 1: 正在查询客户信息',
      },
    },
  }, 2));
  state = applyAgentV2Event(state, envelope('turn.finished', {
    status: 'cancelled',
    error: 'Workflow cancelled by user',
    cancel_reason: 'user_stop',
  }, 10));

  assert.equal(state.status, 'aborted');
  assert.equal(state.isLoading, false);
  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].finished, true);
  assert.equal(state.streamSegments[0].state, 'error');
  assert.equal(state.streamSegments[0].toolStatus, 'error');
  assert.equal(state.streamSegments[0].endTs, 1010);
});

test('web search progress events are accumulated on the tool segment', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyAgentV2Event(state, envelope('tool.started', {
    tool_name: 'web_search',
    tool_call_id: 'call-web',
    input: { query: '示例企业总部地址' },
  }, 1));
  state = applyAgentV2Event(state, envelope('tool.progress', {
    tool_name: 'web_search',
    tool_call_id: 'call-web',
    query: '示例企业总部地址',
    progress: {
      web_search_event: {
        type: 'start',
        content: '开始联网搜索',
      },
    },
  }, 2));
  state = applyAgentV2Event(state, envelope('tool.progress', {
    tool_name: 'web_search',
    tool_call_id: 'call-web',
    query: '示例企业总部地址',
    progress: {
      web_search_event: {
        type: 'final',
        content: '联网搜索完成，返回 3 条结果',
      },
    },
  }, 3));

  assert.equal(state.streamSegments.length, 1);
  assert.deepEqual(
    state.streamSegments[0].meta.web_search_events.map((event) => event.type),
    ['start', 'final'],
  );
  assert.equal(state.streamSegments[0].meta.web_search_events[0].content, '开始联网搜索');
  assert.equal(state.streamSegments[0].meta.input.query, '示例企业总部地址');
});

test('turn finished appends final output after tool-only stream segments', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyAgentV2Event(state, envelope('tool.started', {
    tool_name: 'business_data_search',
    tool_call_id: 'call-search-1',
    input: { query: '查询处理成本' },
    iteration: 1,
  }, 1));
  state = applyAgentV2Event(state, envelope('tool.finished', {
    tool_name: 'business_data_search',
    tool_call_id: 'call-search-1',
    output_preview: '查询完成',
    status: 'completed',
    iteration: 1,
  }, 2));
  state = applyAgentV2Event(state, envelope('turn.finished', {
    status: 'success',
    final_output: '最终处理成本为 3000 元。',
  }, 3));

  const { completedGroups } = buildIndexedSegmentGroups(state.streamSegments);

  assert.equal(state.status, 'finished');
  assert.equal(completedGroups.length, 1);
  assert.equal(completedGroups[0].processSegs.length, 1);
  assert.equal(completedGroups[0].contentSegs[0].text, '最终处理成本为 3000 元。');
});

test('segment grouping skips whitespace-only content groups', () => {
  const { completedGroups, liveProcessSegs } = buildIndexedSegmentGroups([
    { type: 'tool', text: '加载本体', finished: true },
    { type: 'content', text: '\n\n', finished: true },
    { type: 'content', text: '有效内容', finished: true },
  ]);

  assert.equal(completedGroups.length, 1);
  assert.equal(completedGroups[0].contentSegs.length, 1);
  assert.equal(completedGroups[0].contentSegs[0].text, '有效内容');
  assert.equal(completedGroups[0].processSegs.length, 1);
  assert.equal(liveProcessSegs.length, 0);
});
