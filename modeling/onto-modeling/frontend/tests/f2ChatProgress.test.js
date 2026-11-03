// F2 第 2、5 条：子代理的每一步进工作过程；思考中有动静；过程话收进工作过程后流式片段不留在正文
import assert from 'node:assert/strict';
import test from 'node:test';
import { applyEvent, applyEvents, initialConversation, liveActivity, timelineBlocks } from '../src/state/conversation.js';

let n = 0;
const ev = (type, data) => ({ id: String(++n), type, ts: '2026-11-03T03:56:00.000Z', data });
const delta = (kind, text, id = 'a1') => ({ type: 'message.delta', data: { message_id: id, kind, text } });
const sub = (step, extra = {}) => ev('subagent.updated', {
  task_id: 'call-9', group_id: 'g1', title: '子任务', status: 'running', progress_text: step?.text || '', summary: null, step, ...extra,
});

test('子代理的步骤按 id 累积：收尾的只改状态，说法为 null 时沿用先到的', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    ev('progress', { group_id: 'g1', title: '工作过程', state: 'running', steps: [{ id: 's1', text: '派出子任务', state: 'running' }] }),
    sub(null, { status: 'started' }),
    sub({ id: 'x1', kind: 'tool', text: '读取「a.md」', state: 'running' }),
    sub({ id: 'x2', kind: 'tool', text: '读取表格结构', state: 'running' }),
    sub({ id: 'x1', kind: 'tool', text: null, state: 'done' }, { progress_text: '' }),
    sub({ id: 'x3', kind: 'text', text: '两份都读完了', state: 'done' }),
  ]);
  const [agent] = s.items['progress:g1'].subagents;
  assert.deepEqual(agent.steps.map((x) => [x.id, x.text, x.state]), [
    ['x1', '读取「a.md」', 'done'], ['x2', '读取表格结构', 'running'], ['x3', '两份都读完了', 'done'],
  ]);
  assert.equal(agent.progress_text, '两份都读完了');
});

test('进展组整体替换时子代理的步骤还在', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('progress', { group_id: 'g1', title: '工作过程', state: 'running', steps: [] }),
    sub({ id: 'x1', kind: 'tool', text: '读取「a.md」', state: 'running' }),
  ]);
  s = applyEvent(s, ev('progress.updated', { group_id: 'g1', title: '工作过程', state: 'running', steps: [{ id: 's1', text: 'b', state: 'done' }] }));
  assert.equal(s.items['progress:g1'].subagents[0].steps.length, 1);
});

test('思考的流式片段攒成「思考中」，正文、步骤、回答到了就清掉', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [ev('session.state', { state: 'running' }), ev('message.user', { message_id: 'm1', text: '开始' })]);
  assert.deepEqual(liveActivity(s), { kind: 'waiting', thinking: '' });
  s = applyEvents(s, [delta('thinking', 'Let me '), delta('thinking', 'check.')]);
  assert.deepEqual(liveActivity(s), { kind: 'thinking', thinking: 'Let me check.' });
  s = applyEvent(s, delta('text', '好'));
  assert.equal(liveActivity(s), null); // 正文在流：看正文
  s = applyEvent(s, ev('message.assistant', { message_id: 'a1', text_md: '好', anchors: [] }));
  assert.deepEqual(liveActivity(s), { kind: 'waiting', thinking: '' });
  s = applyEvent(s, ev('progress', { group_id: 'g2', title: '工作过程', state: 'running', steps: [{ id: 's1', text: '运行脚本', state: 'running' }] }));
  assert.equal(liveActivity(s), null); // 有在跑的步骤：看步骤
  s = applyEvent(s, ev('session.state', { state: 'idle' }));
  assert.equal(liveActivity(s), null);
});

test('思考的片段只留最后一段，不无限增长', () => {
  let s = applyEvent(initialConversation(), { type: 'session.state', id: '1', data: { state: 'running' } });
  for (let i = 0; i < 300; i += 1) s = applyEvent(s, delta('thinking', '0123456789'));
  assert.ok(s.thinking.length <= 2000);
});

test('过程话收进工作过程：没等到完整消息的流式片段在进展组到达时去掉', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [ev('session.state', { state: 'running' }), delta('text', '先做前置检查。')]);
  assert.equal(s.items['asst:a1'].streaming, true);
  s = applyEvent(s, ev('progress', { group_id: 'g1', title: '工作过程', state: 'running', steps: [{ id: 's1', text: '先做前置检查。', state: 'info' }] }));
  assert.equal(s.items['asst:a1'], undefined);
  assert.deepEqual(timelineBlocks(s)[0].items.map((x) => x.kind), ['progress']);
});
