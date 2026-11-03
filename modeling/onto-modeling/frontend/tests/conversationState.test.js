import assert from 'node:assert/strict';
import test from 'node:test';
import {
  applyEvent, applyEvents, applySnapshot, initialConversation, isBusy, openQuestions, timelineBlocks,
  subagentStatus,
} from '../src/state/conversation.js';

let n = 0;
const ev = (type, data, id) => ({ id: id === null ? undefined : String(id ?? ++n), type, ts: '2026-11-01T07:21:00.000Z', data });

test('按 seq 去重：回放的旧事件不重复进时间线', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [ev('message.user', { message_id: 'm1', text: '继续' }), ev('notice', { kind: 'info', text: 'x', align: 'center' })]);
  s = applyEvent(s, { id: '1', type: 'message.user', data: { message_id: 'm1', text: '重复' } });
  assert.equal(s.order.length, 2);
  assert.equal(s.items['msg:m1'].text, '继续');
  assert.equal(s.lastSeq, 2);
});

test('流式片段拼接，完整消息到达后原位替换', () => {
  let s = initialConversation();
  s = applyEvent(s, { type: 'message.delta', data: { message_id: 'a1', kind: 'text', text: '好。' } });
  s = applyEvent(s, { type: 'message.delta', data: { message_id: 'a1', kind: 'text', text: '这一段' } });
  s = applyEvent(s, { type: 'message.delta', data: { message_id: 'a1', kind: 'thinking', text: '内部' } });
  assert.equal(s.items['asst:a1'].text_md, '好。这一段');
  assert.equal(s.items['asst:a1'].streaming, true);
  s = applyEvent(s, ev('message.assistant', { message_id: 'a1', text_md: '好。这一段要比较。', anchors: [], continued: false }, 9));
  assert.equal(s.order.length, 1);
  assert.equal(s.items['asst:a1'].streaming, false);
  assert.equal(s.items['asst:a1'].text_md, '好。这一段要比较。');
});

test('卡片、进展组按 id 原位替换；子代理挂到进展组', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('progress', { group_id: 'g1', title: '正在修改', state: 'running', steps: [{ id: 's1', text: 'a', state: 'running' }] }),
    ev('card', { card_id: 'c1', card_type: 'round_changes', state: 'pending_confirm' }),
    ev('subagent.updated', { task_id: 't1', group_id: 'g1', title: '语义检查', status: 'running', progress_text: '第 6 / 11 个操作' }),
    ev('progress.updated', { group_id: 'g1', title: '已完成 3 步', state: 'done', steps: [] }),
    ev('card.updated', { card_id: 'c1', card_type: 'round_changes', state: 'merged', merged_version_label: 'v0.13' }),
    ev('subagent.updated', { task_id: 't1', group_id: 'g1', status: 'done' }),
  ]);
  assert.deepEqual(s.order, ['progress:g1', 'card:c1']);
  assert.equal(s.items['progress:g1'].title, '已完成 3 步');
  assert.equal(s.items['progress:g1'].kind, 'progress');
  assert.equal(s.items['progress:g1'].subagents.length, 1);
  assert.equal(s.items['progress:g1'].subagents[0].status, 'done');
  assert.equal(s.items['progress:g1'].subagents[0].title, '语义检查');
  assert.equal(s.items['card:c1'].card.state, 'merged');
});

test('决策问题：回答后收起成一行，重复的「已决定」提示不再单独显示', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('message.assistant', { message_id: 'a1', text_md: '先停在这里', anchors: [] }),
    ev('question.asked', { question_id: 'q1', kind: 'decision', title: '补货标准从哪里来？', options: [], state: 'open' }),
  ]);
  assert.equal(openQuestions(s).length, 1);
  let blocks = timelineBlocks(s);
  assert.deepEqual(blocks.map((b) => b.type), ['assistant', 'question']);
  assert.equal(blocks[1].item.question.kind, 'decision');
  s = applyEvents(s, [
    ev('question.answered', { question_id: 'q1', answer: { option_id: 'A' }, by: 'user', summary: '步骤 2 选了 A：按适用标准逐类取上限，再和门店申请取严' }),
    ev('notice', { kind: 'decided', text: '步骤 2 选了 A：按适用标准逐类取上限，再和门店申请取严', align: 'left' }),
  ]);
  blocks = timelineBlocks(s);
  assert.deepEqual(blocks.map((b) => b.type), ['assistant', 'decided']);
  assert.equal(openQuestions(s).length, 0);
});

test('连续的助手侧内容合成一块，用户消息和系统提示切开', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    ev('day.marker', { label: '今天' }),
    ev('notice', { kind: 'merged', text: '分段已确认生效 · 版本 v0.12', align: 'center' }),
    ev('message.user', { message_id: 'u1', text: '继续' }),
    ev('message.assistant', { message_id: 'a1', text_md: '好', anchors: [] }),
    ev('progress', { group_id: 'g', title: '已完成 3 步', state: 'done', steps: [] }),
    ev('message.assistant', { message_id: 'a2', text_md: '抽出了', anchors: [], continued: true }),
    ev('card', { card_id: 'c', card_type: 'step_output' }),
    ev('tool.activity', { tool_use_id: 'x', visibility: 'internal', tool: 'Read', summary: 's', state: 'done' }),
  ]);
  const blocks = timelineBlocks(s);
  assert.deepEqual(blocks.map((b) => b.type), ['day', 'notice', 'user', 'assistant']);
  assert.equal(blocks[3].items.length, 4);
  assert.equal(blocks[1].item.notice_kind, 'merged');
});

test('会话状态、排队、上下文、建议、用量', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('session.state', { state: 'queued', since: 't' }),
    ev('session.queued', { position: 2, ahead: 1, pool: { active: 12, max: 12 }, progress_pct: 55 }),
  ]);
  assert.equal(s.queue.position, 2);
  assert.ok(isBusy(s));
  s = applyEvents(s, [
    ev('session.state', { state: 'running', turn_started_at: '2026-11-01T07:20:00.000Z' }),
    { type: 'context.usage', data: { used: 84000, window: 200000, pct: 42, compact_at_pct: 80 } },
    ev('suggestions.updated', { mode: 'pending_confirm', primary: { id: 'm', label: '确认生效' }, secondary: [] }),
    ev('turn.completed', { turn_id: 't1', duration_ms: 1, is_error: false, usage: { input_tokens: 10, output_tokens: 5 }, cost: { amount: 0.1, currency: 'CNY' } }),
    ev('session.state', { state: 'idle' }),
  ]);
  assert.equal(s.queue, null);
  assert.equal(s.context.pct, 42);
  assert.equal(s.suggestions.mode, 'pending_confirm');
  assert.equal(s.usage.output_tokens, 5);
  assert.equal(isBusy(s), false);
});

test('配置待生效 → 重新连接后清掉；确认卡处理后标记结果', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('session.config_pending', { changes: [{ kind: 'assembly', summary: '建模能力有更新' }], effective: 'next_message' }),
    ev('permission.requested', { request_id: 'r1', title: '助手请求执行一个操作', description: 'd', timeout_s: 600 }),
  ]);
  assert.ok(s.configPending);
  s = applyEvents(s, [
    ev('session.reconnected', { reason: 'config_changed', changes: [], context_kept: true, duration_ms: 800 }),
    ev('permission.resolved', { request_id: 'r1', behavior: 'deny', by: 'timeout' }),
  ]);
  assert.equal(s.configPending, null);
  assert.equal(s.items['perm:r1'].resolved.by, 'timeout');
  assert.equal(s.items['perm:r1'].kind, 'permission');
});

test('S-05 快照校正会话状态', () => {
  const s = applySnapshot(initialConversation(), { state: 'queued', queue_position: 3, context: { pct: 10 }, pool: { max: 12, active: 12 } });
  assert.equal(s.session.state, 'queued');
  assert.equal(s.queue.ahead, 2);
  assert.equal(s.context.pct, 10);
});

test('子任务先于它的进展组进时间线（历史窗口从组中间开始）：组出现后收进组里，不留一条一直「执行中」的（V 阶段截图）', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    ev('subagent.updated', { task_id: 't1', group_id: 'g1', title: '子任务', status: 'running', progress_text: '读取资料' }),
    ev('subagent.updated', { task_id: 't1', group_id: 'g1', status: 'running', progress_text: '汇总结果' }),
    ev('progress.updated', { group_id: 'g1', title: '校验与走查', state: 'done', steps: [] }),
    ev('subagent.updated', { task_id: 't1', group_id: 'g1', status: 'done', progress_text: '' }),
  ]);
  const free = s.items['subagents:free'];
  assert.ok(!free || !free.subagents.some((x) => x.task_id === 't1'));
  const group = s.items['progress:g1'].subagents;
  assert.equal(group.length, 1);
  assert.equal(group[0].status, 'done');
  assert.equal(group[0].title, '子任务');
  assert.ok(!timelineBlocks(s).some((b) => b.item?.kind === 'subagents' && b.item.subagents.length === 0));
});

test('子任务行状态以终态为准：工作过程或会话结束后，没有终态的显示已中断（ADR-0009）', () => {
  assert.equal(subagentStatus({ status: 'running' }, false), 'running');
  assert.equal(subagentStatus({ status: 'started' }, false), 'started');
  assert.equal(subagentStatus({ status: 'running' }, true), 'interrupted');
  assert.equal(subagentStatus({ status: 'started' }, true), 'interrupted');
  assert.equal(subagentStatus({ status: 'done' }, true), 'done');
  assert.equal(subagentStatus({ status: 'failed' }, false), 'failed');
});
