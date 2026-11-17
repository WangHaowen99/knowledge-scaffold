// F7：子任务卡保留逐步明细、思考中，面板合并接口明细与实时更新
import assert from 'node:assert/strict';
import test from 'node:test';
import { SUB_STEPS_MAX, applyEvents, initialConversation } from '../src/state/conversation.js';
import {
  fmtElapsed, outcome, panelHead, panelSteps, stepTime, summaryParts, thinkingText,
} from '../src/pages/workbench/subagentModel.js';

let n = 0;
const T0 = Date.parse('2026-11-03T03:56:00.000Z');
const iso = (s) => new Date(T0 + s * 1000).toISOString();
const ev = (sec, data, transient = false) => ({
  ...(transient ? {} : { id: String(++n) }), type: 'subagent.updated', ts: iso(sec),
  data: { task_id: 'call-9', group_id: 'g1', title: '语义检查', status: 'running', progress_text: '', summary: null, step: null, step_count: 0, brief: null, thinking: null, ...data },
});
const step = (id, state, text = null) => ({ id, kind: 'tool', text, state });

test('子任务卡留下每一步，同一步后到的只改状态，记下开始和结束', () => {
  n = 0;
  const s = applyEvents(initialConversation(), [
    ev(0, { status: 'started', brief: '检查三个操作' }),
    ev(1, { step: step('a', 'running', '读取操作「甲」'), step_count: 1, progress_text: '读取操作「甲」' }),
    ev(4, { step: step('a', 'done'), step_count: 1, progress_text: '读取操作「甲」' }),
    ev(5, { step: { id: 'say', kind: 'text', text: '读完了', state: 'done' }, step_count: 1, progress_text: '读完了' }),
  ]);
  const card = s.items['sub:call-9'];
  assert.equal(card.brief, '检查三个操作');
  assert.equal(card.step, undefined);
  assert.deepEqual(card.steps.map((x) => [x.id, x.state, x.text]), [['a', 'done', '读取操作「甲」'], ['say', 'done', '读完了']]);
  assert.equal(card.steps[0].started_at, iso(1));
  assert.equal(card.steps[0].ended_at, iso(4));
  assert.equal(card.steps[1].ended_at, null);
  assert.equal(card.started_at, iso(0));
});

test('思考中只在最新一条是心跳时成立，有了新的一步、或子任务结束就清掉', () => {
  n = 0;
  const thinking = { since: iso(10), elapsed_ms: 15000 };
  let s = applyEvents(initialConversation(), [
    ev(0, { status: 'started' }),
    ev(25, { thinking, step_count: 3 }, true),
  ]);
  assert.deepEqual(s.items['sub:call-9'].thinking, thinking);
  assert.equal(s.items['sub:call-9'].step_count, 3);
  s = applyEvents(s, [ev(30, { step: step('b', 'running', '搜索「合成」'), step_count: 4 })]);
  assert.equal(s.items['sub:call-9'].thinking, null);
  s = applyEvents(s, [ev(40, { thinking }, true), ev(41, { status: 'done', summary: '发现 2 个问题', step_count: 4 })]);
  const card = s.items['sub:call-9'];
  assert.equal(card.thinking, null);
  assert.equal(card.ended_at, iso(41));
  assert.equal(card.summary, '发现 2 个问题');
});

test('卡片上的明细只留最近 200 步', () => {
  n = 0;
  const events = [ev(0, { status: 'started' })];
  for (let i = 0; i < SUB_STEPS_MAX + 10; i += 1) events.push(ev(i + 1, { step: step(`s${i}`, 'running', `第 ${i} 步`) }));
  const card = applyEvents(initialConversation(), events).items['sub:call-9'];
  assert.equal(card.steps.length, SUB_STEPS_MAX);
  assert.equal(card.steps[0].id, 's10');
});

test('面板：接口明细在前，实时到的同一步只改状态，新的一步接在后面', () => {
  const fetched = [
    { id: 'a', kind: 'tool', text: '读取「甲」', state: 'done', started_at: iso(1), ended_at: iso(3), duration_ms: 2000 },
    { id: 'b', kind: 'tool', text: '读取「乙」', state: 'running', started_at: iso(4), ended_at: null, duration_ms: null },
  ];
  const live = [
    { id: 'a', kind: 'tool', text: null, state: 'running', started_at: iso(1), ended_at: null },
    { id: 'b', kind: 'tool', text: null, state: 'done', started_at: iso(4), ended_at: iso(9) },
    { id: 'c', kind: 'tool', text: '搜索「丙」', state: 'running', started_at: iso(10), ended_at: null },
  ];
  const steps = panelSteps(fetched, live);
  assert.deepEqual(steps.map((x) => [x.id, x.state, x.text]), [['a', 'done', '读取「甲」'], ['b', 'done', '读取「乙」'], ['c', 'running', '搜索「丙」']]);
  assert.equal(stepTime(steps[0], T0), '2 秒');
  assert.equal(stepTime(steps[1], T0), '5 秒');
  assert.equal(stepTime(steps[2], T0 + 70 * 1000), '1 分 0 秒');
  assert.equal(stepTime({ id: 'say', kind: 'text', text: '说一句' }, T0), '');
  // 工具本身不满 1 秒（实测几十到几百毫秒）不写耗时，免得满屏「0 秒」；在做的也要满 1 秒才走秒
  assert.equal(stepTime({ id: 'r', kind: 'tool', state: 'done', started_at: iso(1), ended_at: iso(1), duration_ms: 120 }, T0), '');
  assert.equal(stepTime({ id: 'q', kind: 'tool', state: 'running', started_at: iso(10), ended_at: null }, Date.parse(iso(10)) + 400), '');
});

test('面板顶部：状态、已运行多久、做了几步；结束后是用时和结果', () => {
  const sub = { status: 'running', started_at: iso(0), step_count: 4 };
  assert.deepEqual(panelHead(sub, null, false, T0 + 190 * 1000), { status: 'running', text: '运行中 · 已运行 3 分 10 秒 · 4 步' });
  const task = { status: 'done', started_at: iso(-60), ended_at: iso(615), step_count: 12 };
  assert.deepEqual(panelHead(sub, task, false, T0 + 999 * 1000), { status: 'done', text: '已完成 · 用时 11 分 15 秒 · 12 步' });
  // 会话已停下、没收到终态：已中断
  assert.equal(panelHead({ status: 'running', started_at: iso(0) }, null, true, T0).status, 'interrupted');
  // 那一轮判定执行超时：用时停在结束时刻，结果处写明
  const timedOut = panelHead({ status: 'running', started_at: iso(0), ended_by: 'timeout' }, null, false, T0 + 60 * 1000);
  assert.equal(timedOut.status, 'timeout');
  assert.ok(timedOut.text.startsWith('执行超时'));
  assert.deepEqual(outcome('timeout', null), { tone: 'gray', label: '执行超时', text: '长时间没有响应，已判定执行超时', details: [] });
  assert.deepEqual(outcome('failed', ''), { tone: 'red', label: '失败原因', text: '子任务出错，没有交回结果', details: [] });
  assert.equal(outcome('done', '没有发现问题').text, '没有发现问题');
  assert.equal(outcome('running', null), null);
});

test('结果：第一行是结论（卡片第二行只放它），其后每行一条明细', () => {
  const summary = '发现 2 个问题：错误 1 · 警告 1\n· 错误 · 操作「门店补货」：Wiki 链接 [[补货单]] 无对应文件\n· 警告 · 对象「门店」：缺少取值说明';
  assert.deepEqual(summaryParts(summary), {
    head: '发现 2 个问题：错误 1 · 警告 1',
    details: ['错误 · 操作「门店补货」：Wiki 链接 [[补货单]] 无对应文件', '警告 · 对象「门店」：缺少取值说明'],
  });
  assert.deepEqual(outcome('done', summary).details, summaryParts(summary).details);
  assert.deepEqual(summaryParts(null), { head: '', details: [] });
  assert.deepEqual(outcome('done', null), { tone: 'green', label: '结果', text: '已完成，结果交回给助手', details: [] });
});

test('思考中：按开始时刻算想了多久，有输出 token 时一起说', () => {
  assert.equal(thinkingText(null, T0), '');
  assert.equal(thinkingText({ since: iso(0), elapsed_ms: 15000 }, T0 + 190 * 1000), '思考中 · 3 分 10 秒');
  assert.equal(thinkingText({ since: iso(0), elapsed_ms: 0, output_tokens: 12000 }, T0 + 20 * 1000), '思考中 · 已输出 1.2 万 token · 20 秒');
  assert.equal(fmtElapsed(2 * 3600 * 1000 + 5 * 60 * 1000), '2 小时 5 分');
});
