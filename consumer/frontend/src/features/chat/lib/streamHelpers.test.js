// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyLocalRunTermination,
  applyPublicEvent,
  friendlyRuntimeError,
} from './streamHelpers.js';
import { buildTraceViewModel } from './traceModel.js';

const canonicalStatus = (status) =>
  ({
    success: 'completed',
    error: 'failed',
    canceled: 'cancelled',
  })[status] ||
  status ||
  'completed';

const publicEvent = (eventType, payload = {}, seq = 1, trace = {}) => {
  const {
    ts = 1000 + seq,
    span_key: spanKey = '',
    parent_key: parentKey = '',
    source = {},
    ...overrides
  } = trace;
  const status =
    eventType === 'turn.finished' ?
      canonicalStatus(payload?.status) :
      'running';
  return {
    schema_version: '1.0',
    event_id: `run-public:${seq}`,
    run_id: 'run-public',
    seq,
    event_type: eventType,
    status,
    source: {
      node: 'agent',
      ...source,
      ...(spanKey ? { span_key: spanKey } : {}),
      ...(parentKey ? { parent_key: parentKey } : {}),
    },
    payload: eventType === 'turn.finished' ? { ...payload, status } : payload,
    created_at: new Date(ts).toISOString(),
    ...overrides,
  };
};

test('runtime capacity failures are presented as incomplete results with a retry action', () => {
  assert.deepEqual(friendlyRuntimeError('RUNTIME_EVENT_BUFFER_FULL'), {
    message: '执行中断，当前结果不完整，请重试。',
    incomplete: true,
  });
  assert.deepEqual(friendlyRuntimeError('BUSINESS_SUMMARY_INCONSISTENT'), {
    message: '结果口径不一致，当前结论未通过校验，请检查调整项后重试。',
    incomplete: true,
  });
});

test('replayed tool events keep one node and preserve the original parent', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  const payload = {
    tool_name: 'business_data_search',
    tool_call_id: 'call-replay',
    message: 'searching',
  };

  state = applyPublicEvent(
    state,
    publicEvent('tool.started', payload, 1, {
      span_key: 'tool:main:2:business_data_search:call-replay',
      parent_key: 'iteration:main:2',
    }),
  );
  state = applyPublicEvent(
    state,
    publicEvent('tool.started', payload, 1, {
      span_key: 'tool:main:2:business_data_search:call-replay',
      parent_key: 'iteration:main:999',
    }),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.finished',
      {
        ...payload,
        status: 'completed',
        output_preview: 'done',
      },
      2,
      {
        span_key: 'tool:main:2:business_data_search:call-replay',
        parent_key: 'iteration:main:999',
      },
    ),
  );

  assert.equal(state.streamSegments.length, 1);
  assert.equal(
    state.streamSegments[0].spanKey,
    'tool:main:2:business_data_search:call-replay',
  );
  assert.equal(state.streamSegments[0].parentKey, 'iteration:main:2');
  assert.equal(state.streamSegments[0].finished, true);
  assert.equal(state.streamSegments[0].startTs, 1001);
  assert.equal(state.streamSegments[0].endTs, 1002);
});

test('tool progress uses span id when legacy locators conflict', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'web_search',
        tool_call_id: 'shared-call',
        message: 'first',
      },
      1,
      {
        span_id: '1111111111111111',
        parent_span_id: 'aaaaaaaaaaaaaaaa',
      },
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'web_search',
        tool_call_id: 'shared-call',
        message: 'second',
      },
      2,
      {
        span_id: '2222222222222222',
        parent_span_id: 'bbbbbbbbbbbbbbbb',
      },
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.progress',
      {
        tool_name: 'different_name',
        tool_call_id: 'different-call',
        message: 'first-progress',
      },
      3,
      {
        span_id: '1111111111111111',
        parent_span_id: 'aaaaaaaaaaaaaaaa',
      },
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.finished',
      {
        tool_name: 'different_name',
        tool_call_id: 'different-call',
        status: 'completed',
      },
      4,
      {
        span_id: '1111111111111111',
        parent_span_id: 'aaaaaaaaaaaaaaaa',
      },
    ),
  );

  assert.equal(state.streamSegments.length, 2);
  assert.equal(state.streamSegments[0].spanId, '1111111111111111');
  assert.equal(state.streamSegments[0].text, 'first-progress');
  assert.equal(state.streamSegments[0].finished, true);
  assert.equal(state.streamSegments[1].spanId, '2222222222222222');
  assert.equal(state.streamSegments[1].text, 'second');
  assert.equal(state.streamSegments[1].finished, false);
});

test('token deltas inherit their iteration parent and do not merge across iterations', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };

  state = applyPublicEvent(
    state,
    publicEvent(
      'token.delta',
      {
        channel: 'content',
        text: '第一轮输出',
        iteration: 1,
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'token.delta',
      {
        channel: 'content',
        text: '第二轮输出',
        iteration: 2,
      },
      2,
    ),
  );

  assert.deepEqual(
    state.streamSegments.map((segment) => segment.parentKey),
    ['iteration:main:1', 'iteration:main:2'],
  );
  assert.deepEqual(
    state.streamSegments.map((segment) => segment.text),
    ['第一轮输出', '第二轮输出'],
  );
});

test('terminal tool batches ignore trailing model output until the turn finishes', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'token.delta',
      {
        channel: 'content',
        text: '有效内容',
        iteration: 1,
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'iteration.completed',
      {
        iteration: 1,
        status: 'terminal_tool_batch',
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'token.delta',
      {
        channel: 'content',
        text: '<user-interaction-response>伪响应</user-interaction-response>',
        iteration: 2,
      },
      3,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'reasoning.summary',
      {
        summary: '无效尾部思考',
        reasoning_block_id: 'reason-2',
        iteration: 2,
      },
      4,
    ),
  );

  assert.equal(state.content, '有效内容');
  assert.equal(state.terminalToolBatchReached, true);
  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].text, '有效内容');
  assert.equal(JSON.stringify(state).includes('伪响应'), false);
  assert.equal(JSON.stringify(state).includes('无效尾部思考'), false);
});

test('tool finish without a start event derives its timing from elapsed_ms', () => {
  const state = applyPublicEvent(
    { streamSegments: [], status: 'running', isLoading: true },
    publicEvent(
      'tool.finished',
      {
        tool_name: 'ontology_context',
        tool_call_id: 'call-late',
        status: 'completed',
        elapsed_ms: 250,
        output_preview: 'done',
      },
      5,
      {
        span_key: 'tool:main:1:ontology_context:call-late',
        parent_key: 'iteration:main:1',
      },
    ),
  );

  assert.equal(state.streamSegments[0].startTs, 755);
  assert.equal(state.streamSegments[0].endTs, 1005);
  assert.equal(
    state.streamSegments[0].spanKey,
    'tool:main:1:ontology_context:call-late',
  );
  assert.equal(state.streamSegments[0].parentKey, 'iteration:main:1');
});

const nestedSubagentEvents = () => [
  publicEvent(
    'subagent_group.started',
    {
      group_id: 'group-root',
      depth: 1,
      tasks: [{ task_id: 'root-task', task: 'Root task', task_index: 0 }],
    },
    1,
  ),
  publicEvent(
    'subagent.started',
    {
      group_id: 'group-root',
      subagent_id: 'sub-root',
      task_id: 'root-task',
      task: 'Root task',
      task_index: 0,
    },
    2,
  ),
  publicEvent(
    'subagent_group.started',
    {
      group_id: 'group-child',
      depth: 2,
      parent_subagent_id: 'sub-root',
      tasks: [{ task_id: 'child-task', task: 'Child task', task_index: 0 }],
    },
    3,
  ),
  publicEvent(
    'subagent.started',
    {
      group_id: 'group-child',
      subagent_id: 'sub-child',
      task_id: 'child-task',
      task: 'Child task',
      task_index: 0,
    },
    4,
  ),
  publicEvent(
    'subagent.delta',
    {
      group_id: 'group-child',
      subagent_id: 'sub-child',
      task_id: 'child-task',
      channel: 'content',
      text: 'nested output',
    },
    5,
    {
      trace_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      span_id: 'bbbbbbbbbbbbbbbb',
      parent_span_id: 'cccccccccccccccc',
    },
  ),
  publicEvent(
    'subagent.finished',
    {
      group_id: 'group-child',
      subagent_id: 'sub-child',
      task_id: 'child-task',
      status: 'completed',
      result: { output: 'done' },
    },
    6,
  ),
  publicEvent(
    'subagent_group.finished',
    {
      group_id: 'group-child',
      depth: 2,
      status: 'completed',
      task_count: 1,
      completed_count: 1,
      error_count: 0,
    },
    7,
  ),
];

test('nested subagent groups attach under parent subagent child', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };

  nestedSubagentEvents().forEach((event) => {
    state = applyPublicEvent(state, event);
  });

  assert.equal(state.streamSegments.length, 1);
  const [rootGroup] = state.streamSegments;
  assert.equal(rootGroup.groupId, 'group-root');
  assert.deepEqual(rootGroup.taskOrder, ['sub-root']);

  const rootChild = rootGroup.children['sub-root'];
  assert.equal(rootChild.taskId, 'root-task');
  assert.equal(rootChild.childGroups.length, 1);

  const [nestedGroup] = rootChild.childGroups;
  assert.equal(nestedGroup.groupId, 'group-child');
  assert.equal(nestedGroup.status, 'completed');
  assert.equal(nestedGroup.finished, true);
  assert.deepEqual(nestedGroup.taskOrder, ['sub-child']);

  const nestedChild = nestedGroup.children['sub-child'];
  assert.equal(nestedChild.status, 'completed');
  assert.equal(nestedChild.events[0].kind, 'token');
  assert.equal(nestedChild.events[0].text, 'nested output');
  assert.equal(nestedChild.events[0].finished, true);
  assert.equal(nestedChild.events[0].status, 'completed');
  assert.equal(
    nestedChild.events[0].traceId,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  );
  assert.equal(nestedChild.events[0].spanId, 'bbbbbbbbbbbbbbbb');
  assert.equal(nestedChild.events[0].parentSpanId, 'cccccccccccccccc');
});

test('cancelled subagent tool event remains a distinct cancelled terminal event', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };

  [
    publicEvent(
      'subagent_group.started',
      {
        group_id: 'group-cancel',
        depth: 1,
        tasks: [{ task_id: 'cancel-task', task: 'Cancel task', task_index: 0 }],
      },
      1,
    ),
    publicEvent(
      'subagent.started',
      {
        group_id: 'group-cancel',
        subagent_id: 'sub-cancel',
        task_id: 'cancel-task',
        task: 'Cancel task',
        task_index: 0,
      },
      2,
    ),
    publicEvent(
      'tool.started',
      {
        group_id: 'group-cancel',
        subagent_id: 'sub-cancel',
        tool_name: 'slow_tool',
        tool_call_id: 'call-cancel',
      },
      3,
    ),
    publicEvent(
      'tool.finished',
      {
        group_id: 'group-cancel',
        subagent_id: 'sub-cancel',
        tool_name: 'slow_tool',
        tool_call_id: 'call-cancel',
        status: 'cancelled',
        error: 'Workflow cancelled by user',
        output_ref: '/agentic/sessions/s1/runs/r1/tool-outputs/call-cancel',
        output_size: 1024,
        output_truncated: true,
      },
      4,
    ),
  ].forEach((event) => {
    state = applyPublicEvent(state, event);
  });

  const child = state.streamSegments[0].children['sub-cancel'];
  assert.equal(child.events.at(-1).status, 'cancelled');
  assert.equal(child.events.at(-1).finished, true);
  assert.equal(child.events.at(-1).error, 'Workflow cancelled by user');
  assert.equal(
    child.events.at(-1).outputRef,
    '/agentic/sessions/s1/runs/r1/tool-outputs/call-cancel',
  );
  assert.equal(child.events.at(-1).outputSize, 1024);
  assert.equal(child.events.at(-1).outputTruncated, true);
});

test('subagent ontology evidence search preserves its artifact reference', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  const outputPreview = JSON.stringify({
    contract: 'OntologyEvidenceSearch/v1',
    success: true,
    hits: [],
  });
  const outputRef = '0123456789abcdef0123456789abcdef';

  [
    publicEvent(
      'subagent_group.started',
      {
        group_id: 'group-evidence',
        tasks: [{ task_id: 'evidence-task', task: '查找证据', task_index: 0 }],
      },
      1,
    ),
    publicEvent(
      'subagent.started',
      {
        group_id: 'group-evidence',
        subagent_id: 'sub-evidence',
        task_id: 'evidence-task',
        task: '查找证据',
        task_index: 0,
      },
      2,
    ),
    publicEvent(
      'tool.started',
      {
        group_id: 'group-evidence',
        subagent_id: 'sub-evidence',
        tool_name: 'ontology_evidence_search',
        tool_call_id: 'call-evidence',
      },
      3,
    ),
    publicEvent(
      'tool.finished',
      {
        group_id: 'group-evidence',
        subagent_id: 'sub-evidence',
        tool_name: 'ontology_evidence_search',
        tool_call_id: 'call-evidence',
        status: 'completed',
        output_preview: outputPreview,
        output_ref: outputRef,
        output_size: 8192,
        output_truncated: true,
      },
      4,
    ),
  ].forEach((event) => {
    state = applyPublicEvent(state, event);
  });

  const toolEvent =
    state.streamSegments[0].children['sub-evidence'].events.at(-1);
  assert.equal(toolEvent.output, outputPreview);
  assert.equal(toolEvent.outputRef, outputRef);
  assert.equal(toolEvent.outputSize, 8192);
  assert.equal(toolEvent.outputTruncated, true);
});

test('subagent business data search preserves its Runtime Fact artifact reference', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  const outputPreview = JSON.stringify({
    success: true,
    runtime_evidence: { evidence_id: 'ev_69067acabf6749079898fd9ade68ea6f' },
  });
  const outputRef = 'fedcba9876543210fedcba9876543210';

  [
    publicEvent(
      'subagent_group.started',
      {
        group_id: 'group-runtime-evidence',
        tasks: [
          { task_id: 'runtime-task', task: '查询业务数据', task_index: 0 },
        ],
      },
      1,
    ),
    publicEvent(
      'subagent.started',
      {
        group_id: 'group-runtime-evidence',
        subagent_id: 'sub-runtime-evidence',
        task_id: 'runtime-task',
        task: '查询业务数据',
        task_index: 0,
      },
      2,
    ),
    publicEvent(
      'tool.started',
      {
        group_id: 'group-runtime-evidence',
        subagent_id: 'sub-runtime-evidence',
        tool_name: 'business_data_search',
        tool_call_id: 'call-runtime-evidence',
      },
      3,
    ),
    publicEvent(
      'tool.finished',
      {
        group_id: 'group-runtime-evidence',
        subagent_id: 'sub-runtime-evidence',
        tool_name: 'business_data_search',
        tool_call_id: 'call-runtime-evidence',
        status: 'completed',
        output_preview: outputPreview,
        output_ref: outputRef,
        output_size: 16384,
        output_truncated: true,
      },
      4,
    ),
  ].forEach((event) => {
    state = applyPublicEvent(state, event);
  });

  const toolEvent =
    state.streamSegments[0].children['sub-runtime-evidence'].events.at(-1);
  assert.equal(toolEvent.output, outputPreview);
  assert.equal(toolEvent.outputRef, outputRef);
  assert.equal(toolEvent.outputSize, 16384);
  assert.equal(toolEvent.outputTruncated, true);
});

test('turn finished error stores provider detail in message meta', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };
  const error = '您的账户已达到速率限制，请您控制请求频率';

  state = applyPublicEvent(
    state,
    publicEvent('turn.finished', { status: 'error', error }, 1),
  );

  assert.equal(state.status, 'error');
  assert.equal(state.error, true);
  assert.equal(state.isLoading, false);
  assert.equal(state.meta.error, error);
});

test('turn finished extracts a display message from structured runtime errors', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'failed',
        error: {
          error_code: 'MODEL_PROVIDER_ERROR',
          message: '模型服务请求失败',
          retriable: true,
          source: 'model',
          details: {},
        },
        recovery: {
          action: 'continue',
          label: '继续执行',
          message: 'Continue from the preserved context.',
        },
      },
      1,
    ),
  );

  assert.equal(state.status, 'error');
  assert.equal(state.meta.error, '模型服务请求失败');
  assert.equal(state.meta.error_code, 'MODEL_PROVIDER_ERROR');
  assert.equal(state.meta.error_retriable, true);
  assert.deepEqual(state.meta.recovery, {
    action: 'continue',
    label: '继续执行',
    message: 'Continue from the preserved context.',
  });
});

test('interrupted turn preserves both incomplete output and continuation recovery', () => {
  const recovery = {
    action: 'continue',
    label: '继续执行',
    message: 'Continue the synthetic task from preserved context.',
  };
  const state = applyPublicEvent(
    { streamSegments: [], status: 'running', isLoading: true },
    publicEvent('turn.finished', {
      status: 'failed',
      content: 'synthetic partial answer',
      error: {
        error_code: 'RUNTIME_EVENT_BUFFER_FULL',
        message: 'synthetic event buffer overflow',
        retriable: true,
      },
      recovery,
    }),
  );

  assert.equal(state.status, 'error');
  assert.equal(state.content, 'synthetic partial answer');
  assert.equal(state.meta.incomplete, true);
  assert.equal(state.meta.error_retriable, true);
  assert.deepEqual(state.meta.recovery, recovery);
});

test('truncated turn preserves partial output and records an incomplete error code', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'failed',
        content: 'synthetic partial answer',
        error: {
          error_code: 'MODEL_OUTPUT_TRUNCATED',
          message: '回答达到输出上限，内容尚未完成',
          retriable: true,
          source: 'model',
          details: {},
        },
      },
      1,
    ),
  );

  assert.equal(state.status, 'error');
  assert.equal(state.content, 'synthetic partial answer');
  assert.equal(state.streamSegments[0].text, 'synthetic partial answer');
  assert.equal(state.meta.error_code, 'MODEL_OUTPUT_TRUNCATED');
  assert.equal(state.meta.error, '回答达到输出上限，内容尚未完成');
});

test('local transport termination updates UI state without fabricating a PublicEvent', () => {
  const state = applyLocalRunTermination(
    { streamSegments: [], status: 'running', isLoading: true },
    {
      status: 'failed',
      error: 'Synthetic transport failure',
      timestamp: 2000,
    },
  );

  assert.equal(state.status, 'error');
  assert.equal(state.isLoading, false);
  assert.equal(state.endTs, 2000);
  assert.equal(state.meta.error, 'Synthetic transport failure');
  assert.equal('event_id' in state, false);
  assert.equal('schema_version' in state, false);
});

test('completed tools with an empty error field do not gain a fallback error', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'ontology_context',
        tool_call_id: 'call-success',
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.finished',
      {
        tool_name: 'ontology_context',
        tool_call_id: 'call-success',
        status: 'completed',
        output_preview: 'synthetic result',
        error: '',
      },
      2,
    ),
  );

  const [tool] = state.streamSegments;
  assert.equal(tool.toolStatus, 'completed');
  assert.equal(tool.meta.error, '');

  const trace = buildTraceViewModel([
    {
      id: 'success-with-empty-error',
      role: 'assistant',
      status: 'finished',
      streamSegments: state.streamSegments,
    },
  ]);
  assert.equal(trace.turns[0].nodes[0].status, 'finished');
  assert.equal(trace.turns[0].nodes[0].error, '');
});

test('turn finished awaiting input keeps interaction state without error', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };
  const interaction = {
    id: 'interaction_call_user',
    type: 'single_choice',
    prompt: '请选择处理方式',
  };

  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'awaiting_input',
        final_output: {
          type: 'user_interaction',
          text: '请选择处理方式',
          options: [
            {
              id: 'a',
              label: '方案A',
              value: 'A',
              interaction_id: interaction.id,
            },
          ],
          interaction,
        },
      },
      1,
    ),
  );

  assert.equal(state.status, 'awaiting');
  assert.equal(state.error, undefined);
  assert.equal(state.isLoading, false);
  assert.equal(state.content, '请选择处理方式');
  assert.equal(state.options[0].id, 'a');
  assert.equal(state.interaction.id, 'interaction_call_user');
});

test('interaction requested exposes its public prompt before the terminal event', () => {
  const state = applyPublicEvent(
    { streamSegments: [], status: 'running', isLoading: true },
    publicEvent(
      'interaction.requested',
      {
        interaction_id: 'interaction_synthetic_input',
        type: 'input',
        prompt: 'Provide a synthetic input value',
        options: [],
      },
      1,
      { status: 'awaiting_input' },
    ),
  );

  assert.equal(state.interaction.id, 'interaction_synthetic_input');
  assert.equal(state.interaction.prompt, 'Provide a synthetic input value');
  assert.equal(state.content, 'Provide a synthetic input value');
  assert.equal(state.messageType, 'user_interaction');
  assert.equal(state.status, 'awaiting');
  assert.equal(state.isLoading, false);
  assert.equal(state.endTs, 1001);
});

test('standalone whitespace token deltas do not create empty stream segments', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent('token.delta', { channel: 'content', text: '\n\n' }, 1),
  );
  state = applyPublicEvent(
    state,
    publicEvent('token.delta', { channel: 'content', text: '实际内容' }, 2),
  );

  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].text, '实际内容');
});

test('cancelled turn freezes unfinished business data search as cancelled', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-search',
        input: { entity: '客户', query: '查询客户中名称为"示例制造企业"' },
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.progress',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-search',
        progress: {
          business_data_search_event: {
            type: 'intermediate',
            content: '步骤 1: 正在查询客户信息',
          },
        },
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'cancelled',
        error: 'Workflow cancelled by user',
        cancel_reason: 'user_stop',
      },
      10,
    ),
  );

  assert.equal(state.status, 'aborted');
  assert.equal(state.isLoading, false);
  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].finished, true);
  assert.equal(state.streamSegments[0].state, 'cancelled');
  assert.equal(state.streamSegments[0].toolStatus, 'cancelled');
  assert.equal(state.streamSegments[0].endTs, 1010);
});

test('business data provider errors remain visible while empty progress is ignored', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-unreachable',
        input: {
          entity: '主CHANGE_ORDER',
          query: '查询所有主CHANGE_ORDER编号',
        },
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.progress',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-unreachable',
        progress: {
          business_data_search_event: { type: 'start', content: '' },
        },
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.progress',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-unreachable',
        progress: {
          business_data_search_event: {
            type: 'error',
            content: 'MindsDB request failed',
          },
        },
      },
      3,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.finished',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-unreachable',
        status: 'error',
        output_preview: 'Error: 业务数据查询失败：MindsDB request failed',
        error: 'Error: 业务数据查询失败：MindsDB request failed',
      },
      4,
    ),
  );

  const [segment] = state.streamSegments;
  assert.equal(segment.toolStatus, 'error');
  assert.equal(
    segment.meta.error,
    'Error: 业务数据查询失败：MindsDB request failed',
  );
  assert.deepEqual(segment.meta.db_events, [
    {
      type: 'error',
      content: 'MindsDB request failed',
      ts: 1003,
      seq: 3,
    },
  ]);
});

test('nested groups split surrounding subagent token events without reordering them', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  const simultaneousEnvelope = (type, payload, seq) =>
    publicEvent(type, payload, seq, { ts: 1000 });
  [
    simultaneousEnvelope(
      'subagent_group.started',
      {
        group_id: 'outer',
        tasks: [{ task_id: 'outer-task', task: 'Outer', task_index: 0 }],
      },
      1,
    ),
    simultaneousEnvelope(
      'subagent.started',
      {
        group_id: 'outer',
        subagent_id: 'outer-agent',
        task_id: 'outer-task',
        task: 'Outer',
      },
      2,
    ),
    simultaneousEnvelope(
      'token.delta',
      {
        group_id: 'outer',
        subagent_id: 'outer-agent',
        channel: 'content',
        text: 'before',
      },
      3,
    ),
    simultaneousEnvelope(
      'subagent_group.started',
      {
        group_id: 'inner',
        parent_subagent_id: 'outer-agent',
        tasks: [],
      },
      4,
    ),
    simultaneousEnvelope(
      'subagent_group.finished',
      {
        group_id: 'inner',
        status: 'completed',
        task_count: 0,
        completed_count: 0,
        error_count: 0,
      },
      5,
    ),
    simultaneousEnvelope(
      'token.delta',
      {
        group_id: 'outer',
        subagent_id: 'outer-agent',
        channel: 'content',
        text: 'after',
      },
      6,
    ),
  ].forEach((event) => {
    state = applyPublicEvent(state, event);
  });
  const child = state.streamSegments[0].children['outer-agent'];

  assert.deepEqual(
    child.events.map((event) => event.text),
    ['before', 'after'],
  );
  assert.deepEqual(
    child.events.map((event) => event.sourceOrder),
    [3, 6],
  );
  assert.deepEqual(
    child.events.map((event) => event.startTs),
    [1000, 1000],
  );
  assert.equal(child.childGroups[0].sourceOrder, 4);
  assert.equal(child.childGroups[0].startTs, 1000);
});

test('cancelled turns recursively close unfinished subagent tools as cancelled', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  [
    publicEvent(
      'subagent_group.started',
      {
        group_id: 'cancel-group',
        tasks: [{ task_id: 'cancel-task', task: 'Slow task', task_index: 0 }],
      },
      1,
    ),
    publicEvent(
      'subagent.started',
      {
        group_id: 'cancel-group',
        subagent_id: 'cancel-agent',
        task_id: 'cancel-task',
        task: 'Slow task',
      },
      2,
    ),
    publicEvent(
      'tool.started',
      {
        group_id: 'cancel-group',
        subagent_id: 'cancel-agent',
        task_id: 'cancel-task',
        tool_name: 'slow_tool',
        tool_call_id: 'slow-call',
      },
      3,
    ),
    publicEvent(
      'subagent.finished',
      {
        group_id: 'cancel-group',
        subagent_id: 'cancel-agent',
        task_id: 'cancel-task',
        status: 'cancelled',
        error: 'Workflow cancelled by user',
      },
      4,
    ),
    publicEvent(
      'subagent_group.finished',
      {
        group_id: 'cancel-group',
        status: 'cancelled',
        task_count: 1,
        completed_count: 0,
        error_count: 1,
      },
      5,
    ),
    publicEvent(
      'turn.finished',
      {
        status: 'cancelled',
        error: 'Workflow cancelled by user',
        cancel_reason: 'user_stop',
      },
      6,
    ),
  ].forEach((event) => {
    state = applyPublicEvent(state, event);
  });

  const [group] = state.streamSegments;
  const child = group.children['cancel-agent'];
  const [tool] = child.events;
  assert.equal(state.status, 'aborted');
  assert.equal(group.status, 'cancelled');
  assert.equal(group.finished, true);
  assert.equal(child.status, 'cancelled');
  assert.equal(tool.status, 'cancelled');
  assert.equal(tool.finished, true);
  assert.equal(tool.endTs, 1004);
  const trace = buildTraceViewModel([
    {
      id: 'cancelled-trace',
      role: 'assistant',
      status: state.status,
      streamSegments: state.streamSegments,
    },
  ]);
  const eventNodes = trace.turns[0].nodes[0].children[0].children[0].children;
  assert.equal(
    eventNodes.every((node) => node.terminal && node.status !== 'running'),
    true,
  );
});

test('successful subagent completion closes token events before Trace rendering', () => {
  let state = { streamSegments: [], status: 'idle', isLoading: false };
  [
    publicEvent(
      'subagent_group.started',
      {
        group_id: 'success-group',
        tasks: [
          { task_id: 'success-task', task: 'Complete task', task_index: 0 },
        ],
      },
      1,
    ),
    publicEvent(
      'subagent.started',
      {
        group_id: 'success-group',
        subagent_id: 'success-agent',
        task_id: 'success-task',
        task: 'Complete task',
      },
      2,
    ),
    publicEvent(
      'token.delta',
      {
        group_id: 'success-group',
        subagent_id: 'success-agent',
        channel: 'reasoning',
        text: 'reason',
      },
      3,
    ),
    publicEvent(
      'token.delta',
      {
        group_id: 'success-group',
        subagent_id: 'success-agent',
        channel: 'content',
        text: 'answer',
      },
      4,
    ),
    publicEvent(
      'subagent.finished',
      {
        group_id: 'success-group',
        subagent_id: 'success-agent',
        task_id: 'success-task',
        status: 'completed',
      },
      5,
    ),
    publicEvent(
      'subagent_group.finished',
      {
        group_id: 'success-group',
        status: 'completed',
        task_count: 1,
        completed_count: 1,
        error_count: 0,
      },
      6,
    ),
    publicEvent(
      'turn.finished',
      { status: 'success', final_output: 'done' },
      7,
    ),
  ].forEach((event) => {
    state = applyPublicEvent(state, event);
  });

  const child = state.streamSegments[0].children['success-agent'];
  assert.deepEqual(
    child.events.map((event) => event.status),
    ['completed', 'completed'],
  );
  assert.equal(
    child.events.every((event) => event.finished),
    true,
  );
  const trace = buildTraceViewModel([
    {
      id: 'success-trace',
      role: 'assistant',
      status: state.status,
      streamSegments: state.streamSegments,
    },
  ]);
  const eventNodes = trace.turns[0].nodes[0].children[0].children[0].children;
  assert.deepEqual(
    eventNodes.map((node) => node.type),
    ['reasoning', 'content'],
  );
  assert.equal(
    eventNodes.every((node) => node.terminal && node.status === 'finished'),
    true,
  );
});

test('web search progress events are accumulated on the tool segment', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'web_search',
        tool_call_id: 'call-web',
        input: { query: '示例企业总部地址' },
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.progress',
      {
        tool_name: 'web_search',
        tool_call_id: 'call-web',
        query: '示例企业总部地址',
        progress: {
          web_search_event: {
            type: 'start',
            content: '开始联网搜索',
          },
        },
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.progress',
      {
        tool_name: 'web_search',
        tool_call_id: 'call-web',
        query: '示例企业总部地址',
        progress: {
          web_search_event: {
            type: 'final',
            content: '联网搜索完成，返回 3 条结果',
          },
        },
      },
      3,
    ),
  );

  assert.equal(state.streamSegments.length, 1);
  assert.deepEqual(
    state.streamSegments[0].meta.web_search_events.map((event) => event.type),
    ['start', 'final'],
  );
  assert.equal(
    state.streamSegments[0].meta.web_search_events[0].content,
    '开始联网搜索',
  );
  assert.equal(state.streamSegments[0].meta.input.query, '示例企业总部地址');
});

test('solver events resume by event_seq and retain only public authoritative fields', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'solve_synthetic_route_aaaaaaaa',
        tool_call_id: 'call-solver',
        input: { item_count: 2 },
      },
      1,
    ),
  );
  const progress = {
    tool_name: 'solve_synthetic_route_aaaaaaaa',
    tool_call_id: 'call-solver',
    run_id: 'run-synthetic',
    job_id: 'job-synthetic',
    attempt_id: 'attempt-synthetic',
    event_seq: 2,
    phase: 'solve',
    message: '正在求解',
    problem_ir: { must_not: 'persist' },
  };
  state = applyPublicEvent(state, publicEvent('solver.progress', progress, 2));
  state = applyPublicEvent(state, publicEvent('solver.progress', progress, 3));
  state = applyPublicEvent(
    state,
    publicEvent(
      'solver.verification.completed',
      {
        ...progress,
        event_seq: 3,
        phase: 'verify',
        message: '独立校验完成',
      },
      4,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'solver.result.authoritative',
      {
        ...progress,
        event_seq: 4,
        status: 'completed',
        authoritative: true,
        feasibility: 'feasible',
        optimality: 'not_proven',
        proof_kind: 'constraint_recalculation',
        summary: { candidate_id: 'ALL_0' },
        result_artifact_ref: 'artifact://result-synthetic',
        verification_evidence_ref: 'artifact://evidence-synthetic',
        candidate_results_artifact_ref:
          'artifact://candidate_results/synthetic',
        candidate_results_artifact_digest: 'sha256:candidate-results-synthetic',
        fact_snapshot_digest: 'sha256:synthetic',
      },
      5,
    ),
  );

  const [segment] = state.streamSegments;
  assert.equal(segment.meta.solver_events.length, 3);
  assert.deepEqual(
    segment.meta.solver_events.map((event) => event.event_seq),
    [2, 3, 4],
  );
  assert.equal(segment.meta.solver_result.authoritative, true);
  assert.equal(segment.meta.solver_result.optimality, 'not_proven');
  assert.equal(
    segment.meta.solver_result.candidate_results_artifact_ref,
    'artifact://candidate_results/synthetic',
  );
  assert.equal(
    segment.meta.solver_result.candidate_results_artifact_digest,
    'sha256:candidate-results-synthetic',
  );
  assert.equal('problem_ir' in segment.meta.solver_events[0], false);
  assert.equal(JSON.stringify(segment).includes('must_not'), false);
});

test('solver progress without a tool start creates a canonical traced tool segment', () => {
  const state = applyPublicEvent(
    { streamSegments: [], status: 'running', isLoading: true },
    publicEvent(
      'solver.progress',
      {
        tool_name: 'solve_synthetic_route_aaaaaaaa',
        tool_call_id: 'call-solver-orphan',
        job_id: 'job-synthetic-orphan',
        event_seq: 1,
        phase: 'solve',
        message: 'Solving',
      },
      2,
      {
        span_key: 'tool:main:1:solve:call-solver-orphan',
        parent_key: 'iteration:main:1',
      },
    ),
  );

  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].startTs, 1002);
  assert.equal(
    state.streamSegments[0].spanKey,
    'tool:main:1:solve:call-solver-orphan',
  );
  assert.equal(state.streamSegments[0].parentKey, 'iteration:main:1');
});

test('turn finished appends final output after tool-only stream segments', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-search-1',
        input: { query: '查询处理成本' },
        iteration: 1,
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.finished',
      {
        tool_name: 'business_data_search',
        tool_call_id: 'call-search-1',
        output_preview: '查询完成',
        status: 'completed',
        iteration: 1,
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'iteration.completed',
      {
        iteration: 1,
        status: 'final_output',
        total_elapsed_ms: 20,
      },
      3,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'success',
        final_output: '最终处理成本为 3000 元。',
      },
      4,
    ),
  );

  assert.equal(state.status, 'finished');
  assert.deepEqual(
    state.streamSegments.map((segment) => segment.type),
    ['tool', 'content'],
  );
  assert.equal(state.streamSegments[0].meta.output_preview, '查询完成');
  assert.equal(state.streamSegments[1].text, '最终处理成本为 3000 元。');
  assert.equal(state.streamSegments[1].parentKey, 'iteration:main:1');
});

test('streamed agent output is grouped under its runtime iteration', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'token.delta',
      {
        text: 'synthetic answer',
        iteration: 2,
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'iteration.completed',
      {
        iteration: 2,
        status: 'final_output',
        total_elapsed_ms: 25,
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'completed',
        content: 'synthetic answer',
      },
      3,
    ),
  );

  const trace = buildTraceViewModel([
    {
      id: 'streamed-output-iteration',
      role: 'assistant',
      status: 'finished',
      iterationMetrics: state.iterationMetrics,
      streamSegments: state.streamSegments,
    },
  ]);
  const [iteration] = trace.turns[0].iterations;
  assert.equal(iteration.metrics.iteration, 2);
  assert.deepEqual(
    iteration.children.map((node) => node.type),
    ['content'],
  );
  assert.equal(
    trace.turns[0].nodes.filter((node) => node.type === 'content').length,
    0,
  );
});

test('public async runtime tool payload uses the contracted name field', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.started',
      {
        name: 'ontology_context',
        tool_call_id: 'call-public-1',
        input_preview: { resource: 'root' },
        iteration: 1,
      },
      1,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'tool.finished',
      {
        name: 'ontology_context',
        tool_call_id: 'call-public-1',
        status: 'completed',
        elapsed_ms: 10,
        output_preview: 'synthetic output',
        output_size: 128,
        output_truncated: false,
        iteration: 1,
      },
      2,
    ),
  );
  state = applyPublicEvent(
    state,
    publicEvent(
      'iteration.completed',
      {
        iteration: 1,
        status: 'continue',
        total_elapsed_ms: 10,
      },
      3,
    ),
  );

  assert.equal(state.streamSegments.length, 1);
  assert.equal(state.streamSegments[0].meta.name, 'ontology_context');
  assert.equal(state.streamSegments[0].text, '调用工具: ontology_context');
  assert.equal(state.streamSegments[0].finished, true);
  const trace = buildTraceViewModel([
    {
      id: 'public-tool-contract',
      role: 'assistant',
      status: 'finished',
      iterationMetrics: state.iterationMetrics,
      streamSegments: state.streamSegments,
    },
  ]);
  const [tool] = trace.turns[0].iterations[0].children;
  assert.equal(tool.toolName, 'ontology_context');
  assert.deepEqual(tool.input, { resource: 'root' });
  assert.equal(tool.outputSize, 128);
});

test('public async runtime terminal uses content and structured_content fields', () => {
  let state = { streamSegments: [], status: 'running', isLoading: true };

  state = applyPublicEvent(
    state,
    publicEvent(
      'turn.finished',
      {
        status: 'completed',
        content: '公共契约最终结果',
      },
      1,
    ),
  );

  assert.equal(state.status, 'finished');
  assert.equal(state.content, '公共契约最终结果');
  assert.equal(state.streamSegments[0].text, '公共契约最终结果');
});

test('public deterministic terminal keeps structured output and artifacts', () => {
  const state = applyPublicEvent(
    { streamSegments: [], status: 'running', isLoading: true },
    publicEvent(
      'turn.finished',
      {
        status: 'completed',
        content: 'synthetic deterministic result',
        structured_content: {
          status: 'success',
          text: 'synthetic deterministic result',
          structured_result: { value: 'synthetic-value' },
          artifacts: [
            {
              name: 'result.json',
              media_type: 'application/json',
              size: 27,
              output_ref: 'synthetic-output-ref',
            },
          ],
        },
      },
      1,
    ),
  );

  assert.equal(state.status, 'finished');
  assert.deepEqual(
    state.streamSegments.map((segment) => segment.type),
    ['content', 'artifacts'],
  );
  assert.equal(
    state.streamSegments[1].meta.artifacts[0].output_ref,
    'synthetic-output-ref',
  );
});

test('public async runtime reasoning delta maps to the reasoning channel', () => {
  const state = applyPublicEvent(
    { streamSegments: [], status: 'running', isLoading: true },
    publicEvent('reasoning.delta', { text: '分析步骤' }, 1),
  );

  assert.equal(state.reasoning, '分析步骤');
  assert.equal(state.streamSegments[0].type, 'reasoning');
  assert.equal(state.content || '', '');
});
