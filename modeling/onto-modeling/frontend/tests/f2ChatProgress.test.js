// F2 第 2、5 条：派出的子任务单独成卡（旧版对话页样式）；工具调用之间的话照常进正文。F7：运行状态行说清在做什么、在等什么
import assert from 'node:assert/strict';
import test from 'node:test';
import { applyEvent, applyEvents, initialConversation, runStatus, timelineBlocks } from '../src/state/conversation.js';

let n = 0;
const ev = (type, data) => ({ id: String(++n), type, ts: '2026-11-03T03:56:00.000Z', data });
const delta = (kind, text, id = 'a1') => ({ type: 'message.delta', data: { message_id: id, kind, text } });
const sub = (step, extra = {}) => ev('subagent.updated', {
  task_id: 'call-9', group_id: 'g1', title: '子任务', status: 'running', progress_text: step?.text || '', summary: null, step, ...extra,
});

test('子任务卡：后到的更新盖住先到的，空的进展说法不盖掉最近一步', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    sub(null, { status: 'started' }),
    sub({ id: 'x1', kind: 'tool', text: '读取「a.md」', state: 'running' }),
    sub({ id: 'x1', kind: 'tool', text: null, state: 'done' }, { progress_text: '' }),
    sub(null, { status: 'done', summary: '两份都读完了', progress_text: '' }),
  ]);
  const card = s.items['sub:call-9'];
  assert.equal(card.kind, 'subagent');
  assert.equal(card.status, 'done');
  assert.equal(card.summary, '两份都读完了');
  assert.equal(card.progress_text, '读取「a.md」');
  assert.equal(card.step, undefined);
});

test('子任务卡排在第一次出现的位置，之后的工作过程排在它后面', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    ev('progress', { group_id: 'g1', title: '工作过程', state: 'done', steps: [{ id: 's1', text: '列出资料', state: 'done' }] }),
    sub({ id: 'x1', kind: 'tool', text: '读取「a.md」', state: 'running' }),
    ev('progress', { group_id: 'g1-1', title: '工作过程', state: 'running', steps: [{ id: 's1', text: '读取「b.md」', state: 'running' }] }),
    sub(null, { status: 'done', progress_text: '' }),
  ]);
  assert.deepEqual(timelineBlocks(s)[0].items.map((x) => x.key), ['progress:g1', 'sub:call-9', 'progress:g1-1']);
});

test('运行状态行：子任务在跑时写在等哪个子任务，做完了回到思考中', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('session.state', { state: 'running', turn_started_at: '2026-11-03T03:56:00.000Z' }),
    ev('message.user', { message_id: 'm1', text: '/ontology-validate', delivery: 'immediate' }),
    ev('progress', { group_id: 'g1', title: '校验与走查', state: 'done', steps: [{ id: 's1', text: '列出资料', state: 'done' }] }),
    sub(null, { status: 'running', title: '语义检查' }),
  ]);
  assert.deepEqual(runStatus(s), { title: '校验与走查', doing: '等子任务「语义检查」', since: '2026-11-03T03:56:00.000Z', thinking: '' });
  s = applyEvent(s, sub(null, { status: 'done', title: '语义检查' }));
  assert.equal(runStatus(s).doing, '思考中');
  // 运行中插的话不算开启新的一轮：标题还是这一轮的
  s = applyEvent(s, ev('message.user', { message_id: 'm2', text: '顺便看看规则', delivery: 'steer' }));
  assert.equal(runStatus(s).title, '校验与走查');
  s = applyEvent(s, ev('session.state', { state: 'idle' }));
  assert.equal(runStatus(s), null);
});

test('运行状态行：建模命令开始前平台在等资料转成文本，等完回到思考中', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [
    ev('message.user', { message_id: 'm1', text: '/ontology-extract-object', delivery: 'immediate' }),
    ev('session.state', { state: 'running', turn_started_at: '2026-11-03T03:56:00.000Z', reason: 'waiting_materials' }),
  ]);
  assert.equal(runStatus(s).doing, '等资料转成文本');
  s = applyEvent(s, ev('session.state', { state: 'running', turn_started_at: '2026-11-03T03:56:00.000Z', reason: null }));
  assert.equal(runStatus(s).doing, '思考中');
});

test('运行状态行：在跑的步骤、等你回答；思考片段跟着走，正文到了清掉', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [ev('session.state', { state: 'running' }), ev('message.user', { message_id: 'm1', text: '开始' })]);
  assert.equal(runStatus(s).doing, '思考中');
  s = applyEvents(s, [delta('thinking', 'Let me '), delta('thinking', 'check.')]);
  assert.equal(runStatus(s).thinking, 'Let me check.');
  s = applyEvent(s, ev('message.assistant', { message_id: 'a1', text_md: '好', anchors: [] }));
  assert.equal(runStatus(s).thinking, '');
  s = applyEvent(s, ev('progress', { group_id: 'g2', title: '', state: 'running', steps: [{ id: 's1', text: '运行脚本', state: 'running' }] }));
  assert.equal(runStatus(s).doing, '运行脚本');
  s = applyEvent(s, ev('question.asked', { question_id: 'q1', title: '选哪个', options: [] }));
  assert.equal(runStatus(s).doing, '等你回答');
});

test('思考的片段只留最后一段，不无限增长', () => {
  let s = applyEvent(initialConversation(), { type: 'session.state', id: '1', data: { state: 'running' } });
  for (let i = 0; i < 300; i += 1) s = applyEvent(s, delta('thinking', '0123456789'));
  assert.ok(s.thinking.length <= 2000);
});

test('工具调用之间的话照常进正文：进展组到达时在流的正文不被去掉', () => {
  n = 0;
  let s = applyEvents(initialConversation(), [ev('session.state', { state: 'running' }), delta('text', '先做前置检查。')]);
  s = applyEvent(s, ev('progress', { group_id: 'g1', title: '工作过程', state: 'running', steps: [{ id: 's1', text: '检查命令锁', state: 'running' }] }));
  assert.equal(s.items['asst:a1'].text_md, '先做前置检查。');
  assert.deepEqual(timelineBlocks(s)[0].items.map((x) => x.kind), ['assistant', 'progress']);
});
