import assert from 'node:assert/strict';
import test from 'node:test';

import { buildConversationViewModel } from './conversationModel.js';
import { buildTraceViewModel } from './traceModel.js';

const nestedSubagentSegment = {
  type: 'subagent_group',
  groupId: 'group-root',
  status: 'completed',
  finished: true,
  startTs: 100,
  endTs: 500,
  taskOrder: ['sub-1'],
  children: {
    'sub-1': {
      subagentId: 'sub-1',
      taskId: 'task-1',
      taskIndex: 0,
      task: '检索资料',
      status: 'completed',
      startTs: 120,
      endTs: 450,
      result: { output: 'done' },
      events: [
        { kind: 'token', channel: 'reasoning', text: '分析资料', ts: 150 },
        {
          kind: 'tool',
          toolCallId: 'sub-tool-1',
          toolName: 'web_search',
          status: 'completed',
          finished: true,
          input: { query: '资料' },
          output: '3 results',
          outputRef: '/agentic/sessions/s1/runs/run-trace-1/tool-outputs/sub-output',
          outputSize: 4096,
          outputTruncated: true,
          startTs: 200,
          endTs: 300,
          progress: [{
            web: { type: 'final', content: '搜索完成', seq: 9, ts: 280 },
            ts: 280,
          }],
        },
      ],
      childGroups: [],
    },
  },
  meta: { group_id: 'group-root' },
};

const messages = [
  { id: 'user-1', role: 'user', content: '请分析' },
  {
    id: 'assistant-1',
    role: 'assistant',
    kind: 'agent_step',
    runId: 'run-trace-1',
    status: 'awaiting',
    isLoading: false,
    startTs: 100,
    endTs: 600,
    interaction: { id: 'approval-1', type: 'approval', prompt: '是否继续' },
    streamSegments: [
      { type: 'reasoning', text: '制定计划', finished: true, startTs: 100, endTs: 150 },
      nestedSubagentSegment,
      {
        type: 'tool',
        state: 'completed',
        text: '查询数据',
        finished: true,
        startTs: 510,
        endTs: 550,
        meta: { tool_name: 'business_data_search', tool_call_id: 'db-1' },
      },
    ],
    iterationMetrics: [{ iteration: 2, status: 'completed', totalElapsedMs: 400, ts: 580 }],
  },
];

test('trace groups assistant work by turn and run while preserving root order', () => {
  const trace = buildTraceViewModel(messages);
  const [turn] = trace.turns;

  assert.equal(trace.kind, 'trace');
  assert.equal(trace.turns.length, 1);
  assert.equal(turn.runId, 'run-trace-1');
  assert.deepEqual(turn.nodes.map((node) => node.type), [
    'reasoning',
    'subagent',
    'business_data',
    'awaiting',
  ]);
  assert.deepEqual(turn.nodes.map((node) => node.order), [0, 1, 2, 3]);
  assert.equal(turn.status, 'awaiting');
  assert.equal(turn.terminal, true);
});

test('trace and conversation share stable source keys', () => {
  const conversation = buildConversationViewModel(messages);
  const trace = buildTraceViewModel(conversation);
  const [turn] = trace.turns;

  assert.equal(turn.sourceKey, conversation.turns[0].key);
  assert.deepEqual(
    turn.nodes.map((node) => node.sourceKey),
    conversation.turns[0].blocks.map((block) => block.key),
  );
  assert.deepEqual(
    buildTraceViewModel(conversation).turns[0].nodes.map((node) => node.key),
    turn.nodes.map((node) => node.key),
  );
});

test('nested subagent events keep task and event order with typed tool progress', () => {
  const [firstTurn] = buildTraceViewModel(messages).turns;
  const subagentNode = firstTurn.nodes[1];
  const [groupNode] = subagentNode.children;
  const [taskNode] = groupNode.children;

  assert.equal(groupNode.subtype, 'group');
  assert.equal(taskNode.subagentId, 'sub-1');
  assert.deepEqual(taskNode.children.map((node) => node.type), ['reasoning', 'web_search']);
  assert.equal(taskNode.children[1].toolCallId, 'sub-tool-1');
  assert.equal(taskNode.children[1].outputRef, '/agentic/sessions/s1/runs/run-trace-1/tool-outputs/sub-output');
  assert.equal(taskNode.children[1].outputSize, 4096);
  assert.equal(taskNode.children[1].outputTruncated, true);
  assert.equal(taskNode.children[1].text, '');
  assert.equal(taskNode.children[1].children[0].type, 'web_search');
  assert.equal(taskNode.children[1].children[0].text, '搜索完成');
  assert.equal(taskNode.children[1].children[0].startedAt, 280);
  assert.equal(taskNode.children[1].durationMs, 100);
});

test('trace tool nodes forward full-output references and web result metadata', () => {
  const trace = buildTraceViewModel([{
    id: 'web-turn',
    role: 'assistant',
    runId: 'run-web',
    status: 'finished',
    streamSegments: [{
      type: 'tool',
      state: 'completed',
      finished: true,
      meta: {
        tool_name: 'web_search',
        tool_call_id: 'web-call',
        input: { query: 'query' },
        output_ref: '/agentic/sessions/s1/runs/run-web/tool-outputs/web-output',
        output_size: 2048,
        output_truncated: true,
        output_preview: JSON.stringify({
          provider: 'tavily',
          results: [{ title: 'Result', url: 'https://example.com/result' }],
        }),
      },
    }],
  }]);
  const [node] = trace.turns[0].nodes;

  assert.equal(node.outputRef, '/agentic/sessions/s1/runs/run-web/tool-outputs/web-output');
  assert.equal(node.outputSize, 2048);
  assert.equal(node.outputTruncated, true);
  assert.equal(node.query, 'query');
  assert.equal(node.provider, 'tavily');
  assert.equal(node.resultCount, 1);
  assert.equal(node.sources[0].domain, 'example.com');
});

test('trace exposes stable iteration metrics separately from ordered event nodes', () => {
  const [turn] = buildTraceViewModel(messages).turns;

  assert.equal(turn.iterations.length, 1);
  assert.equal(turn.iterations[0].key, `${turn.key}:iteration:2`);
  assert.equal(turn.iterations[0].durationMs, 400);
  assert.equal(turn.iterations[0].metrics.iteration, 2);
  assert.deepEqual(turn.nodes.map((node) => node.type), [
    'reasoning',
    'subagent',
    'business_data',
    'awaiting',
  ]);
});

test('narrative trace nodes use compact labels and only expose non-duplicate detail', () => {
  const turn = buildTraceViewModel([{
    id: 'narrative-turn',
    role: 'assistant',
    runId: 'narrative-run',
    status: 'finished',
    streamSegments: [
      { type: 'reasoning', text: '详细分析过程', meta: {} },
      { type: 'content', text: '一段很长的最终回答', meta: {} },
    ],
  }]).turns[0];

  assert.deepEqual(turn.nodes.slice(0, 2).map((node) => node.text), ['模型思考', 'Agent 输出']);
  assert.deepEqual(turn.nodes.slice(0, 2).map((node) => node.detail), ['详细分析过程', '一段很长的最终回答']);
});

test('status nodes carry errors and terminal time without a second derivation', () => {
  const trace = buildTraceViewModel([{
    id: 'failed-turn',
    role: 'assistant',
    runId: 'run-failed',
    status: 'error',
    isLoading: false,
    startTs: 1000,
    endTs: 1200,
    meta: { error: 'provider timeout' },
    streamSegments: [],
  }]);
  const [turn] = trace.turns;
  const [status] = turn.nodes;

  assert.equal(status.type, 'status');
  assert.equal(status.status, 'error');
  assert.equal(status.text, 'provider timeout');
  assert.equal(status.terminal, true);
  assert.equal(turn.durationMs, 200);
});

test('nested child groups remain between events in chronological order', () => {
  const segment = structuredClone(nestedSubagentSegment);
  const child = segment.children['sub-1'];
  child.events = [
    { kind: 'token', channel: 'content', text: 'before', ts: 1000, sourceOrder: 3 },
    { kind: 'token', channel: 'content', text: 'after', ts: 1000, sourceOrder: 6 },
  ];
  child.childGroups = [{
    type: 'subagent_group',
    groupId: 'nested-between',
    status: 'completed',
    finished: true,
    startTs: 1000,
    endTs: 1000,
    sourceOrder: 4,
    taskOrder: [],
    children: {},
  }];
  const trace = buildTraceViewModel([{
    id: 'nested-order', role: 'assistant', runId: 'nested-order-run', status: 'finished',
    streamSegments: [segment],
  }]);
  const [rootNode] = trace.turns[0].nodes;
  const [groupNode] = rootNode.children;
  const [taskNode] = groupNode.children;

  assert.deepEqual(taskNode.children.map((node) => node.subtype), ['event', 'group', 'event']);
  assert.deepEqual(taskNode.children.map((node) => node.text), ['before', 'nested-between', 'after']);
});

test('trace attaches tools to their declared iterations', () => {
  const trace = buildTraceViewModel([{
    id: 'iteration-tree', role: 'assistant', runId: 'iteration-tree-run', status: 'finished',
    streamSegments: [
      {
        type: 'tool', state: 'completed', finished: true,
        parentKey: 'iteration:main:1',
        meta: { tool_name: 'first_tool', tool_call_id: 'first' },
      },
      {
        type: 'tool', state: 'completed', finished: true,
        parentKey: 'iteration:main:2',
        meta: { tool_name: 'second_tool', tool_call_id: 'second' },
      },
    ],
    iterationMetrics: [
      { iteration: 1, status: 'completed', totalElapsedMs: 100, ts: 100 },
      { iteration: 2, status: 'completed', totalElapsedMs: 200, ts: 200 },
    ],
  }]);
  const [turn] = trace.turns;

  assert.deepEqual(turn.iterations.map((iteration) => iteration.children[0].toolName), ['first_tool', 'second_tool']);
  assert.deepEqual(turn.iterations.map((iteration) => iteration.children[0].parentKey), turn.iterations.map((iteration) => iteration.key));
  assert.deepEqual(turn.nodes.map((node) => node.type), ['status']);
});

test('trace parent span id overrides conflicting legacy iteration keys', () => {
  const trace = buildTraceViewModel([{
    id: 'otel-parent-tree', role: 'assistant', runId: 'otel-parent-run', status: 'finished',
    streamSegments: [
      {
        type: 'tool', state: 'completed', finished: true,
        spanId: '1111111111111111', parentSpanId: 'bbbbbbbbbbbbbbbb',
        parentKey: 'iteration:main:1',
        meta: { tool_name: 'belongs_to_second', tool_call_id: 'first' },
      },
      {
        type: 'tool', state: 'completed', finished: true,
        spanId: '2222222222222222', parentSpanId: 'aaaaaaaaaaaaaaaa',
        parentKey: 'iteration:main:2',
        meta: { tool_name: 'belongs_to_first', tool_call_id: 'second' },
      },
    ],
    iterationMetrics: [
      { iteration: 1, spanId: 'aaaaaaaaaaaaaaaa', status: 'completed', totalElapsedMs: 100, ts: 100 },
      { iteration: 2, spanId: 'bbbbbbbbbbbbbbbb', status: 'completed', totalElapsedMs: 200, ts: 200 },
    ],
  }]);
  const [turn] = trace.turns;

  assert.deepEqual(
    turn.iterations.map((iteration) => iteration.children[0].toolName),
    ['belongs_to_first', 'belongs_to_second'],
  );
  assert.deepEqual(
    turn.iterations.map((iteration) => iteration.children[0].parentKey),
    turn.iterations.map((iteration) => iteration.key),
  );
});

test('trace attaches narrative output to iterations from persisted segment metadata', () => {
  const trace = buildTraceViewModel([{
    id: 'iteration-output', role: 'assistant', runId: 'iteration-output-run', status: 'finished',
    streamSegments: [
      {
        type: 'content', state: 'completed', text: '第一轮结果', finished: true,
        meta: { iteration: 1 },
      },
      {
        type: 'reasoning', state: 'completed', text: '第二轮思考', finished: true,
        meta: { iteration: 2 },
      },
    ],
    iterationMetrics: [
      { iteration: 1, status: 'completed', totalElapsedMs: 100, ts: 100 },
      { iteration: 2, status: 'completed', totalElapsedMs: 200, ts: 200 },
    ],
  }]);
  const [turn] = trace.turns;

  assert.deepEqual(
    turn.iterations.map((iteration) => iteration.children.map((node) => node.type)),
    [['content'], ['reasoning']],
  );
  assert.deepEqual(
    turn.iterations.map((iteration) => iteration.children[0].detail),
    ['第一轮结果', '第二轮思考'],
  );
  assert.deepEqual(turn.nodes.map((node) => node.type), ['status']);
});
