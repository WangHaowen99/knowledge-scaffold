import assert from 'node:assert/strict';
import test from 'node:test';

import { buildConversationViewModel } from './conversationModel.js';
import {
  LEGACY_AGENT_STREAM_EVENT,
  LEGACY_AGENT_STREAM_PROTOCOL,
} from './legacyAgentEvent.js';
import { buildMessagesFromEvents } from './messageBuilder.js';

const hydrateLegacyEnvelopes = (envelopes) => buildMessagesFromEvents(
  envelopes.map((data) => ({ event: LEGACY_AGENT_STREAM_EVENT, data })),
);

const assistantMessage = {
  id: 'turn-message-1',
  runId: 'run-1',
  role: 'assistant',
  kind: 'agent_step',
  status: 'finished',
  isLoading: false,
  startTs: 1000,
  endTs: 1800,
  content: '最终答案',
  streamSegments: [
    {
      type: 'reasoning',
      state: 'streaming',
      text: '先分析问题',
      meta: { reasoning_block_id: 'reason-1', reasoning_summary: '问题分析' },
      finished: true,
      startTs: 1010,
      endTs: 1100,
    },
    {
      type: 'tool',
      state: 'completed',
      text: '查询业务数据',
      meta: {
        tool_name: 'business_data_search',
        tool_call_id: 'call-db',
        input: { query: '处理成本' },
        output_preview: '成本为 3000',
        output_ref: '/agentic/sessions/s1/runs/run-1/tool-outputs/out-db',
        output_size: 8192,
        db_events: [
          { type: 'intermediate', content: '正在查询', seq: 3, ts: 1200 },
          { type: 'final', content: '查询完成', seq: 4, ts: 1250 },
        ],
      },
      finished: true,
      startTs: 1150,
      endTs: 1300,
      spanKey: 'tool:call-db',
      parentKey: 'iteration:1',
    },
    {
      type: 'content',
      state: 'final',
      text: '最终答案',
      meta: {},
      finished: true,
      startTs: 1400,
      endTs: 1700,
    },
  ],
  iterationMetrics: [{ iteration: 1, status: 'completed', totalElapsedMs: 700, ts: 1750 }],
};

test('conversation model exposes the incomplete-output error code', () => {
  const model = buildConversationViewModel([{
    id: 'synthetic-truncated-turn',
    runId: 'synthetic-run',
    role: 'assistant',
    status: 'failed',
    content: 'synthetic partial answer',
    meta: {
      error: '回答达到输出上限，内容尚未完成',
      error_code: 'MODEL_OUTPUT_TRUNCATED',
    },
  }]);

  assert.equal(model.turns[0].status, 'error');
  assert.equal(model.turns[0].content, 'synthetic partial answer');
  assert.equal(model.turns[0].errorCode, 'MODEL_OUTPUT_TRUNCATED');
});

test('system cancellation notice remains a standalone non-turn message', () => {
  const model = buildConversationViewModel([{
    id: 'interaction-cancelled-1',
    role: 'system',
    content: '用户取消了操作',
    metadata: { type: 'interaction_cancelled' },
  }]);

  assert.equal(model.messages[0].role, 'system');
  assert.equal(model.messages[0].kind, 'message');
  assert.equal(model.messages[0].content, '用户取消了操作');
  assert.equal(model.turns.length, 0);
});

test('conversation blocks keep source segment order and add lifecycle last', () => {
  const model = buildConversationViewModel([assistantMessage]);
  const turn = model.turns[0];

  assert.equal(model.kind, 'conversation');
  assert.equal(turn.runId, 'run-1');
  assert.deepEqual(turn.blocks.map((block) => block.kind), [
    'reasoning',
    'business_data',
    'content',
    'status',
  ]);
  assert.deepEqual(turn.blocks.map((block) => block.order), [0, 1, 2, 3]);
  assert.equal(turn.blocks[0].summary, '问题分析');
  assert.equal(turn.blocks[1].toolCallId, 'call-db');
  assert.equal(turn.blocks[1].outputRef, '/agentic/sessions/s1/runs/run-1/tool-outputs/out-db');
  assert.equal(turn.blocks[1].outputSize, 8192);
  assert.deepEqual(turn.blocks[1].progress.map((item) => item.type), ['intermediate', 'final']);
  assert.equal(turn.blocks.at(-1).status, 'finished');
});

test('solver tool segments project progress and authoritative result separately', () => {
  const model = buildConversationViewModel([{
    id: 'solver-message',
    role: 'assistant',
    runId: 'run-synthetic',
    status: 'success',
    streamSegments: [{
      type: 'tool',
      state: 'completed',
      finished: true,
      meta: {
        tool_name: 'solve_synthetic_route_aaaaaaaa',
        tool_call_id: 'call-solver',
        solver_events: [
          { type: 'solver.progress', event_seq: 2, phase: 'solve', message: '正在求解' },
          { type: 'solver.verification.completed', event_seq: 3, phase: 'verify', message: '独立校验完成' },
        ],
        solver_result: {
          authoritative: true,
          feasibility: 'feasible',
          optimality: 'not_proven',
          proof_kind: 'constraint_recalculation',
          summary: { candidate_id: 'ALL_0' },
          result_artifact_ref: 'artifact://result-synthetic',
          verification_evidence_ref: 'artifact://evidence-synthetic',
        },
      },
    }],
  }]);

  const block = model.turns[0].blocks[0];
  assert.equal(block.kind, 'solver');
  assert.deepEqual(block.progress.map((item) => item.phase), ['solve', 'verify']);
  assert.equal(block.solverResult.authoritative, true);
  assert.equal(block.solverResult.optimality, 'not_proven');
});

test('deterministic artifact segments normalize download metadata', () => {
  const message = {
    ...assistantMessage,
    streamSegments: [
      ...assistantMessage.streamSegments,
      {
        type: 'artifacts',
        state: 'final',
        text: '',
        meta: {
          artifacts: [{
            name: 'result.json',
            media_type: 'application/json',
            size: 14,
            sha256: 'sha256:abc',
            output_ref: 'artifact-ref',
          }],
        },
        finished: true,
      },
    ],
  };

  const model = buildConversationViewModel([message]);
  const block = model.turns[0].blocks.find((item) => item.kind === 'artifacts');

  assert.ok(block);
  assert.deepEqual(block.artifacts[0], {
    key: 'artifact-ref',
    name: 'result.json',
    mediaType: 'application/json',
    size: 14,
    sha256: 'sha256:abc',
    outputRef: 'artifact-ref',
  });
});

test('conversation keys and timing are deterministic across rebuilds', () => {
  const first = buildConversationViewModel([assistantMessage]);
  const second = buildConversationViewModel([assistantMessage]);
  const firstTurn = first.turns[0];
  const secondTurn = second.turns[0];

  assert.equal(firstTurn.key, secondTurn.key);
  assert.deepEqual(
    firstTurn.blocks.map((block) => block.key),
    secondTurn.blocks.map((block) => block.key),
  );
  assert.equal(firstTurn.durationMs, 800);
  assert.equal(firstTurn.blocks[1].durationMs, 150);
  assert.equal(firstTurn.terminal, true);
});

test('streaming fallback block keys stay stable while segment text grows', () => {
  const build = (text) => buildConversationViewModel([{
    id: 'streaming-turn',
    runId: 'streaming-run',
    role: 'assistant',
    status: 'running',
    isLoading: true,
    streamSegments: [{ type: 'content', state: 'streaming', text }],
  }]).turns[0].blocks[0].key;

  assert.equal(build('a'), build('answer still streaming'));
});

test('awaiting interaction is a terminal renderer block with a stable lifecycle key', () => {
  const interaction = { id: 'ask-1', type: 'single_choice', prompt: '请选择方案' };
  const running = buildConversationViewModel([{
    ...assistantMessage,
    status: 'running',
    isLoading: true,
    endTs: null,
  }]).turns[0];
  const awaiting = buildConversationViewModel([{
    ...assistantMessage,
    status: 'awaiting_input',
    interaction,
  }]).turns[0];

  assert.equal(awaiting.status, 'awaiting');
  assert.equal(awaiting.terminal, true);
  assert.equal(awaiting.blocks.at(-1).kind, 'awaiting');
  assert.equal(awaiting.blocks.at(-1).interaction.id, 'ask-1');
  assert.equal(awaiting.blocks.at(-1).key, running.blocks.at(-1).key);
});

test('structured runtime errors render their public message instead of object text', () => {
  const turn = buildConversationViewModel([{
    ...assistantMessage,
    status: 'error',
    content: '',
    streamSegments: [],
    meta: {
      error: {
        error_code: 'MODEL_PROVIDER_ERROR',
        message: '模型服务请求失败',
        source: 'model',
      },
      error_code: 'MODEL_PROVIDER_ERROR',
      error_retriable: true,
      recovery: {
        action: 'continue',
        label: '继续执行',
        message: 'Continue from the preserved context.',
      },
    },
  }]).turns[0];

  assert.equal(turn.error, '模型服务请求失败');
  assert.equal(turn.errorPresentation.message, '模型服务暂时不可用，请稍后继续执行。');
  assert.equal(turn.blocks.at(-1).text, '模型服务请求失败');
  assert.equal(turn.recovery.action, 'continue');
  assert.equal(turn.recovery.label, '继续执行');
});

test('web search and generic tools remain distinct renderer kinds', () => {
  const message = {
    ...assistantMessage,
    streamSegments: [
      {
        type: 'tool',
        state: 'completed',
        text: '搜索网页',
        meta: {
          tool_name: 'web_search',
          tool_call_id: 'call-web',
          web_search_events: [{ type: 'final', content: '返回 3 条来源', seq: 8, ts: 1500 }],
          output_preview: JSON.stringify({
            ok: true,
            provider: 'tavily',
            query: '示例企业总部地址',
            response_time: 0.42,
            results: [
              { title: 'Source A', url: 'https://example.com/a', snippet: 'A' },
              { title: 'Source B', url: 'https://example.cn/b', snippet: 'B' },
            ],
          }),
        },
        finished: true,
      },
      {
        type: 'tool',
        state: 'error',
        text: '调用失败',
        meta: { tool_name: 'other_tool', tool_call_id: 'call-other', error: 'boom' },
        finished: true,
      },
    ],
  };
  const blocks = buildConversationViewModel([message]).turns[0].blocks;

  assert.deepEqual(blocks.map((block) => block.kind), ['web_search', 'tool', 'content', 'status']);
  assert.equal(blocks[0].progress[0].text, '返回 3 条来源');
  assert.equal(blocks[0].query, '示例企业总部地址');
  assert.equal(blocks[0].provider, 'tavily');
  assert.equal(blocks[0].resultCount, 2);
  assert.deepEqual(blocks[0].sources.map((source) => source.domain), ['example.com', 'example.cn']);
  assert.equal(blocks[0].responseTime, 0.42);
  assert.equal(blocks[1].status, 'error');
  assert.equal(blocks[1].error, 'boom');
});

test('canonical tool name fields select specialized renderer kinds', () => {
  const message = {
    ...assistantMessage,
    streamSegments: [{
      type: 'tool',
      state: 'completed',
      meta: {
        name: 'business_data_search',
        tool_call_id: 'call-canonical-business-data',
        db_events: [{ type: 'final', content: 'Synthetic query completed', seq: 2, ts: 1200 }],
      },
      finished: true,
    }],
  };

  const block = buildConversationViewModel([message]).turns[0].blocks[0];
  assert.equal(block.kind, 'business_data');
  assert.equal(block.toolName, 'business_data_search');
  assert.equal(block.progress[0].text, 'Synthetic query completed');
});

test('business data exposes progress and parsed structured output', () => {
  const block = buildConversationViewModel([assistantMessage]).turns[0].blocks[1];
  const structured = buildConversationViewModel([{
    ...assistantMessage,
    streamSegments: [{
      ...assistantMessage.streamSegments[1],
      meta: {
        ...assistantMessage.streamSegments[1].meta,
        output_preview: JSON.stringify({ columns: ['cost'], rows: [[3000]], total: 1 }),
      },
    }],
  }]).turns[0].blocks[0];

  assert.deepEqual(block.progress.map((event) => event.type), ['intermediate', 'final']);
  assert.deepEqual(structured.parsedOutput, { columns: ['cost'], rows: [[3000]], total: 1 });
});

test('business data progress omits empty entries and keeps provider errors', () => {
  const block = buildConversationViewModel([{
    ...assistantMessage,
    streamSegments: [{
      ...assistantMessage.streamSegments[1],
      state: 'completed',
      meta: {
        ...assistantMessage.streamSegments[1].meta,
        output_preview: '',
        db_events: [
          { type: 'start', content: '', seq: 1, ts: 1200 },
          { type: 'error', content: 'MindsDB request failed', seq: 2, ts: 1210 },
        ],
      },
    }],
  }]).turns[0].blocks[0];

  assert.equal(block.status, 'error');
  assert.equal(block.error, 'MindsDB request failed');
  assert.deepEqual(block.progress.map((event) => event.type), ['error']);
  assert.equal(block.progress[0].text, 'MindsDB request failed');
});

test('raw envelopes hydrate into the same ordered conversation model', () => {
  const envelope = (type, payload, seq, ts) => ({
    protocol: LEGACY_AGENT_STREAM_PROTOCOL,
    type,
    payload,
    run_id: 'run-envelope',
    event_id: `run-envelope:${seq}`,
    seq,
    ts,
  });
  const envelopes = [
    envelope('turn.started', {}, 1, 100),
    envelope('token.delta', { channel: 'reasoning', text: '思考' }, 2, 110),
    envelope('tool.started', { tool_name: 'lookup', tool_call_id: 'call-1' }, 3, 120),
    envelope('tool.finished', {
      tool_name: 'lookup',
      tool_call_id: 'call-1',
      status: 'completed',
      output_preview: 'done',
    }, 4, 130),
    envelope('token.delta', { channel: 'content', text: '回答' }, 5, 140),
    envelope('turn.finished', { status: 'success', final_output: '回答' }, 6, 150),
  ];

  const model = buildConversationViewModel(hydrateLegacyEnvelopes(envelopes));
  assert.equal(model.turns[0].runId, 'run-envelope');
  assert.deepEqual(
    model.turns[0].blocks.map((block) => block.kind),
    ['reasoning', 'tool', 'content', 'status'],
  );
  assert.equal(model.turns[0].blocks[1].output, 'done');
});

test('duplicate fallback identities receive deterministic unique keys', () => {
  const model = buildConversationViewModel([{
    role: 'assistant',
    runId: 'duplicate-run',
    status: 'running',
    streamSegments: [
      { type: 'reasoning', text: 'same', state: 'streaming' },
      { type: 'reasoning', text: 'same', state: 'streaming' },
    ],
  }]);
  const [first, second] = model.turns[0].blocks;

  assert.notEqual(first.key, second.key);
  assert.match(first.key, /reasoning:segment:0$/);
  assert.match(second.key, /reasoning:segment:1$/);
});

test('finished narrative segments cannot remain visually running', () => {
  const blocks = buildConversationViewModel([{
    id: 'finished-narrative',
    role: 'assistant',
    status: 'finished',
    streamSegments: [
      { type: 'reasoning', state: 'streaming', text: '分析完成', finished: true },
      { type: 'content', state: 'streaming', text: '回答完成', finished: true },
    ],
  }]).turns[0].blocks;

  assert.deepEqual(blocks.slice(0, 2).map((block) => block.status), ['finished', 'finished']);
  assert.deepEqual(blocks.slice(0, 2).map((block) => block.terminal), [true, true]);
});

test('raw tool envelopes preserve elapsed timing including orphan completions', () => {
  const rawEnvelope = (type, payload, seq, ts) => ({
    protocol: LEGACY_AGENT_STREAM_PROTOCOL,
    type,
    payload,
    run_id: 'run-timing',
    event_id: `run-timing:${seq}`,
    seq,
    ts,
  });
  const messages = hydrateLegacyEnvelopes([
    rawEnvelope('turn.started', {}, 1, 100),
    rawEnvelope('tool.started', { tool_name: 'lookup', tool_call_id: 'timed' }, 2, 120),
    rawEnvelope('tool.finished', {
      tool_name: 'lookup', tool_call_id: 'timed', status: 'completed', elapsed_ms: 45,
    }, 3, 180),
    rawEnvelope('tool.finished', {
      tool_name: 'orphan', tool_call_id: 'orphan', status: 'completed', elapsed_ms: 25,
    }, 4, 220),
    rawEnvelope('turn.finished', { status: 'success', final_output: 'done' }, 5, 240),
  ]);
  const model = buildConversationViewModel(messages);
  const tools = model.turns[0].blocks.filter((block) => block.kind === 'tool');

  assert.deepEqual(tools.map((block) => block.durationMs), [45, 25]);
  assert.equal(tools[0].startedAt, 120);
  assert.equal(tools[0].endedAt, 180);
  assert.equal(tools[1].startedAt, 195);
  assert.equal(tools[1].endedAt, 220);
});

test('web search projection survives a truncated preview and rejects unsafe links', () => {
  const projection = {
    provider: 'tavily',
    result_count: 12,
    results: [
      { title: 'Safe source', url: 'https://example.com/result', snippet: 'safe' },
      { title: 'Unsafe source', url: 'data:text/html,<script>alert(1)</script>', snippet: 'plain text only' },
    ],
  };
  const block = buildConversationViewModel([{
    id: 'projected-search',
    role: 'assistant',
    status: 'finished',
    streamSegments: [{
      type: 'tool',
      state: 'completed',
      finished: true,
      meta: {
        tool_name: 'web_search',
        tool_call_id: 'projected-web',
        output_preview: '{'.padEnd(4096, 'x'),
        output_truncated: true,
        output_projection: projection,
      },
    }],
  }]).turns[0].blocks[0];

  assert.equal(block.provider, 'tavily');
  assert.equal(block.resultCount, 12);
  assert.equal(block.sources[0].url, 'https://example.com/result');
  assert.equal(block.sources[0].domain, 'example.com');
  assert.equal(block.sources[1].url, '');
  assert.equal(block.sources[1].title, 'Unsafe source');
});
