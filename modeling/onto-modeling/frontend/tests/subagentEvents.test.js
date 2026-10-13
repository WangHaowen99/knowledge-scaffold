// 子代理事件归并（lib/subagentEvents.js）：双内核事件 → 卡片生命周期与步骤流。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildSubagentFlow,
  buildSubagentLifecycle,
  isSubagentCardMessage,
} from '../src/lib/subagentEvents.js';

const dispatch = (seq, id, description) => ({
  seq,
  type: 'assistant_message',
  blocks: [
    {
      kind: 'tool_use',
      tool: 'Task',
      tool_use_id: id,
      input: { description, subagent_type: 'general-purpose' },
    },
  ],
});

test('Jiuwen 步骤按 step_id 合并状态，文字步骤独立成行', () => {
  const events = [
    dispatch(1, 'T1', '分析资料'),
    {
      seq: 2, type: 'subagent_started', task_id: 'T1', tool_use_id: 'T1',
      description: '分析资料', agent_type: 'general-purpose',
    },
    {
      seq: 3, type: 'subagent_activity', task_id: 'T1', tool_use_id: 'T1',
      step_id: 's1', kind: 'tool', tool: 'Read', input: { file_path: 'notes.md' },
    },
    {
      seq: 4, type: 'subagent_activity', task_id: 'T1', tool_use_id: 'T1',
      step_id: 's1', kind: 'tool', tool: 'Read', status: 'ok',
    },
    {
      seq: 5, type: 'subagent_activity', task_id: 'T1', tool_use_id: 'T1',
      step_id: 's2', kind: 'text', text: '已梳理出 6 个候选对象',
    },
    {
      seq: 6, type: 'subagent_finished', task_id: 'T1', tool_use_id: 'T1',
      status: 'completed', usage: { duration_ms: 63000 },
    },
  ];

  const { cards, steps } = buildSubagentFlow(events, new Map());
  const life = buildSubagentLifecycle(events).get('T1');

  assert.deepEqual([...cards], ['T1']);
  const rows = steps.get('T1');
  assert.equal(rows.length, 2);
  assert.deepEqual(
    { kind: rows[0].kind, tool: rows[0].tool, status: rows[0].status, input: rows[0].input },
    { kind: 'tool', tool: 'Read', status: 'ok', input: { file_path: 'notes.md' } },
  );
  assert.equal(rows[1].text, '已梳理出 6 个候选对象');
  assert.deepEqual(
    { done: life.done, status: life.status, durationMs: life.durationMs },
    { done: true, status: 'completed', durationMs: 63000 },
  );
});

test('Claude 子代理消息按 parent_tool_use_id 归卡，状态取工具结果', () => {
  const events = [
    dispatch(1, 'T1', '分析资料'),
    {
      seq: 2, type: 'assistant_message', subagent: true, parent_tool_use_id: 'T1',
      blocks: [
        { kind: 'tool_use', tool: 'Read', tool_use_id: 'R1', input: { file_path: 'notes.md' } },
        { kind: 'text', text: '  读完了  ' },
      ],
    },
  ];
  const toolResults = new Map([['R1', { isError: false, excerpt: '摘录' }]]);

  const rows = buildSubagentFlow(events, toolResults).steps.get('T1');

  assert.deepEqual(rows.map((row) => row.kind), ['tool', 'text']);
  assert.equal(rows[0].status, 'ok');
  assert.equal(rows[0].excerpt, '摘录');
  assert.equal(rows[1].text, '读完了');
  // 工具结果还没回来 → 该行保持运行中
  assert.equal(buildSubagentFlow(events, new Map()).steps.get('T1')[0].status, null);
});

test('多个子代理并行时步骤按父卡隔离', () => {
  const events = [
    dispatch(1, 'T1', '分析资料'),
    dispatch(2, 'T2', '核对指标'),
    {
      seq: 3, type: 'subagent_activity', tool_use_id: 'T2', step_id: 'b1',
      kind: 'tool', tool: 'Grep', input: { pattern: '订单' },
    },
    {
      seq: 4, type: 'assistant_message', subagent: true, parent_tool_use_id: 'T1',
      blocks: [{ kind: 'tool_use', tool: 'Write', tool_use_id: 'W1', input: { file_path: 'a.md' } }],
    },
    {
      seq: 5, type: 'subagent_activity', tool_use_id: 'T2', step_id: 'b1',
      kind: 'tool', tool: 'Grep', status: 'error',
    },
  ];

  const { steps } = buildSubagentFlow(events, new Map());

  assert.deepEqual(steps.get('T1').map((row) => row.tool), ['Write']);
  assert.deepEqual(steps.get('T2').map((row) => [row.tool, row.status]), [['Grep', 'error']]);
});

test('卡片被历史截断时不吞子代理消息（交回主工具组呈现）', () => {
  const events = [
    {
      seq: 1, type: 'assistant_message', subagent: true, parent_tool_use_id: 'T-gone',
      blocks: [{ kind: 'tool_use', tool: 'Read', tool_use_id: 'R9', input: {} }],
    },
  ];

  const { cards, steps } = buildSubagentFlow(events, new Map());

  assert.equal(cards.has('T-gone'), false);
  assert.equal(steps.size, 0);
});

test('终态只带 task_id 时仍更新 tool_use_id 索引的卡片', () => {
  const events = [
    dispatch(1, 'T1', '分析资料'),
    { seq: 2, type: 'subagent_started', task_id: 'task-9', tool_use_id: 'T1', description: '分析资料' },
    { seq: 3, type: 'subagent_finished', task_id: 'task-9', tool_use_id: null, status: 'failed', summary: '失败原因' },
  ];

  const life = buildSubagentLifecycle(events).get('T1');

  assert.deepEqual(
    { status: life.status, summary: life.summary, done: life.done },
    { status: 'failed', summary: '失败原因', done: true },
  );
});

test('折叠进子任务卡的子代理消息不计入主对话新消息', () => {
  const subagentMessage = {
    seq: 2, type: 'assistant_message', subagent: true, parent_tool_use_id: 'T1',
    blocks: [{ kind: 'tool_use', tool: 'Read', tool_use_id: 'R1', input: { file_path: 'notes.md' } }],
  };
  const events = [
    dispatch(1, 'T1', '分析资料'),
    subagentMessage,
    {
      seq: 3, type: 'assistant_message', subagent: true, parent_tool_use_id: 'T1',
      blocks: [{ kind: 'text', text: '读完了' }],
    },
    { seq: 4, type: 'subagent_activity', tool_use_id: 'T1', step_id: 's1', kind: 'tool', tool: 'Grep' },
  ];

  assert.equal(isSubagentCardMessage(subagentMessage), true);
  // 主 agent 自己的消息（含派遣卡本身）照常计数
  assert.equal(isSubagentCardMessage(events[0]), false);
  // 旧历史的子代理消息没有派遣 id，仍在主工具组呈现，维持原计数口径
  assert.equal(
    isSubagentCardMessage({ type: 'assistant_message', subagent: true, blocks: [] }),
    false,
  );
  // 一致性：凡是被折叠进卡片的消息，计数规则都能识别出来
  const foldedSeqs = new Set(
    [...buildSubagentFlow(events, new Map()).steps.values()]
      .flat()
      .map((step) => Number(String(step.key).split('-')[0]))
      .filter(Number.isFinite),
  );
  assert.deepEqual([...foldedSeqs].sort(), [2, 3]);
  for (const seq of foldedSeqs) {
    assert.equal(isSubagentCardMessage(events.find((event) => event.seq === seq)), true);
  }
});
