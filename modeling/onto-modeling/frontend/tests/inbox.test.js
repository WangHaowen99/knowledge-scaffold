import assert from 'node:assert/strict';
import test from 'node:test';
import { humanNodeItem, mainItems, runItems } from '../src/api/mock/inbox-data.js';
import { createRunStore } from '../src/api/mock/run3.js';
import {
  adoptedIndex, afterResolve, ageText, anchorLabel, changePointText, confirmLabel, confirmPlan, countsWithoutEvidence, impactName, keyAction, keyHints,
  kindStyle, pickHint, sortItems, stepId, summarySub, todayRows, visibleFilters, withoutEvidence,
} from '../src/pages/inbox/inboxModel.js';

const NOW = new Date(2026, 10, 1, 17, 30).getTime();
const at = (d, h, m) => new Date(2026, 10, d, h, m).toISOString();

test('时间文字：自主构建写运行号，其余写相对时间', () => {
  assert.equal(ageText({ run: { run_no: 3, at: at(1, 15, 42) } }, NOW), '运行 #3 · 15:42');
  assert.equal(ageText({ run: { run_no: 3, at: null } }, NOW), '运行 #3');
  assert.equal(ageText({ created_at: new Date(NOW - 18 * 60000).toISOString() }, NOW), '18 分钟前');
  assert.equal(ageText({ created_at: new Date(NOW - 62 * 60000).toISOString() }, NOW), '1 小时前');
  assert.equal(ageText({ created_at: at(1, 9, 10) }, NOW), '今天 09:10');
  assert.equal(ageText({ created_at: at(0 + 31, 17, 40).replace('-12-01', '-10-31') }, NOW), '昨天');
  assert.equal(ageText({ created_at: at(28, 10, 0).replace('-11-', '-10-') }, NOW), '10-28');
});

test('排序、上下条、处理完去哪', () => {
  const items = [{ id: 'a', created_at: at(1, 9, 0), slice: { name: '乙' } }, { id: 'b', created_at: at(1, 12, 0), slice: { name: '甲' } }, { id: 'c', created_at: at(1, 10, 0) }];
  assert.deepEqual(sortItems(items, 'default').map((x) => x.id), ['a', 'b', 'c']);
  assert.deepEqual(sortItems(items, 'newest').map((x) => x.id), ['b', 'c', 'a']);
  assert.equal(sortItems(items, 'slice').at(-1).id, 'c');
  assert.equal(stepId(items, 'a', 1), 'b');
  assert.equal(stepId(items, 'c', 1), 'c');
  assert.equal(stepId(items, 'a', -1), 'a');
  assert.equal(stepId(items, 'zz', 1), 'a');
  assert.equal(afterResolve(items, 'a', 'c'), 'c');
  assert.equal(afterResolve(items, 'a', null), 'b');
  assert.equal(afterResolve(items, 'c', null), 'b');
  assert.equal(afterResolve([{ id: 'a' }], 'a', null), null);
});

test('键盘：J/K、1–4、E、↵、Esc；输入框里只认 ↵ 和 Esc', () => {
  assert.deepEqual(keyAction({ key: 'j' }), { type: 'move', dir: 1 });
  assert.deepEqual(keyAction({ key: 'K' }), { type: 'move', dir: -1 });
  assert.deepEqual(keyAction({ key: '3' }), { type: 'pick', index: 2 });
  assert.equal(keyAction({ key: '5' }), null);
  assert.deepEqual(keyAction({ key: 'e' }), { type: 'rewrite' });
  assert.deepEqual(keyAction({ key: 'Enter' }), { type: 'confirm' });
  assert.equal(keyAction({ key: 'k', metaKey: true }), null);
  assert.equal(keyAction({ key: 'j', inInput: true }), null);
  assert.deepEqual(keyAction({ key: 'Enter', inInput: true }), { type: 'confirm' });
  assert.equal(keyAction({ key: 'Enter', inInput: true, isComposing: true }), null);
  assert.deepEqual(keyAction({ key: 'Escape', inInput: true }), { type: 'cancel' });
});

test('确认并下一条：按类型发对应的动作，没选时提示', () => {
  const q = { options: [{ id: 'A', label: 'a' }, { id: 'B', label: 'b' }, { id: 'D', label: '其他', custom: true }] };
  assert.deepEqual(confirmPlan({ kind: 'decision' }, q, 1, null), { action: 'answer', body: { option_id: 'B' } });
  assert.deepEqual(confirmPlan({ kind: 'decision' }, q, 2, '  按门店取严 '), { action: 'answer', body: { custom_text: '按门店取严' } });
  assert.match(confirmPlan({ kind: 'decision' }, q, null, null).error, /先选一个选项/);
  assert.match(confirmPlan({ kind: 'decision' }, q, 2, '').error, /先写下你的方向/);
  assert.deepEqual(confirmPlan({ kind: 'evidence_conflict' }, { question: q }, 0, null), { action: 'arbitrate', body: { option_id: 'A' } });
  const led = { chosen: '包含', options: [{ id: 'A', label: '不包含' }, { id: 'B', label: '包含冷链温度记录', recommended: true }] };
  assert.equal(adoptedIndex(led.options, led.chosen), 1);
  assert.deepEqual(confirmPlan({ kind: 'review' }, led, 1, null), { action: 'accept', body: {} });
  assert.deepEqual(confirmPlan({ kind: 'review' }, led, 0, null), { action: 'rejudge', body: { option_id: 'A' } });
  assert.deepEqual(confirmPlan({ kind: 'review' }, { group: 'high_confidence', entries: [] }, null, null), { action: 'accept', body: {} });
  assert.match(confirmPlan({ kind: 'change_list' }, { points: [{ state: 'accepted' }, { state: 'pending' }] }).error, /还有 1 条/);
  assert.deepEqual(confirmPlan({ kind: 'change_list' }, { points: [{ state: 'accepted' }] }), { action: 'execute', body: {} });
  assert.match(confirmPlan({ kind: 'suspension' }, { options: [] }, null, null).error, /上传文本版/);
  assert.equal(confirmPlan({ kind: 'stale' }, {}).action, 'refresh');
  assert.equal(confirmPlan({ kind: 'resumable' }, {}).action, 'resume');
  assert.equal(confirmPlan({ kind: 'feedback' }, {}).action, 'send_to_evolution');
  assert.equal(confirmPlan({ kind: 'notification', actions: ['dismiss'] }, {}).action, 'dismiss');
});

test('出处短标签、汇总条副标题、今天的进度、变更点文字、类型样式', () => {
  assert.equal(anchorLabel('file:resource/others/02-门店补货规则.docx#p:9.2'), '§9.2');
  assert.equal(anchorLabel('file:resource/schema/06-商品品类表.xlsx#cell:品类表!D12'), '品类表 D12');
  assert.equal(anchorLabel('db:erp-prod/erp.replenish_std.max_qty'), '数据库');
  assert.equal(summarySub({ started_at: at(1, 15, 10), ended_at: at(1, 17, 2), state: 'completed', cost: { amount: 86, currency: 'CNY' } }), '15:10 – 17:02 · 全部分段完成 · 费用 ¥86');
  assert.equal(summarySub({ started_at: at(1, 15, 10), ended_at: null, state: 'running', cost: null }), '15:10 开始 · 运行中');
  // 只列有剩余的几类，每类只给剩余数
  assert.deepEqual(todayRows({ remaining_by_filter: { decision: 2, autobuild_review: 8, stale: 0 } }), [['决策', '2 剩余'], ['自主构建复核', '8 剩余']]);
  assert.equal(changePointText({ condition: '乳制品陈列上限', value: '120 → 150 箱', scope: '' }), '乳制品陈列上限 120 → 150 箱');
  assert.equal(changePointText({ condition: 'x', rewritten_text: '改写后' }), '改写后');
  assert.equal(impactName({ kind: 'rule', name: 'R-11 临时加量须审批' }), 'R-11');
  assert.equal(impactName({ type: 'trace.scenario', title: '场景 6' }), '场景 6');
  assert.deepEqual(kindStyle('evidence_conflict'), ['conflict', 'alert']);
  assert.deepEqual(kindStyle('format_migration'), ['resume', 'inbox']);
});

test('mock 待处理：主示例项目 18 项，筛选计数与设计稿一致；运行中只有已经发生的复核', () => {
  const count = (store) => {
    const { head, tail, run } = mainItems(store);
    const all = [...head, ...run(), ...tail].map(([it]) => it);
    const by = (fn) => all.filter(fn).length;
    return {
      all: all.length,
      autobuild_review: by((it) => (it.kind === 'review' && it.source === 'autobuild') || it.kind === 'suspension'),
      decision: by((it) => it.kind === 'decision'),
      evidence_conflict: by((it) => it.kind === 'evidence_conflict'),
      change_list: by((it) => it.kind === 'change_list'),
      stale: by((it) => it.kind === 'stale'),
      resumable: by((it) => it.kind === 'resumable'),
      feedback: by((it) => it.kind === 'feedback'),
    };
  };
  assert.deepEqual(count(createRunStore('completed')), { all: 18, autobuild_review: 8, decision: 2, evidence_conflict: 2, change_list: 1, stale: 3, resumable: 1, feedback: 1 });
  assert.equal(count(createRunStore('running')).all, 16);
  const done = createRunStore('completed');
  const hi = runItems(done).find(([it]) => it.high_confidence);
  assert.equal(hi[0].title, '高置信的 13 个自动决定，可以一起接受');
  done.acceptHigh();
  assert.equal(runItems(done).some(([it]) => it.high_confidence), false);
});

test('人工节点的决策卡：没有自定义回答，不提示 E、E 不进改写、只能按选项回答', () => {
  const [it, q] = humanNodeItem();
  assert.equal(it.kind, 'decision');
  assert.equal(it.source, 'plugin');
  assert.equal(q.options.some((o) => o.custom), false);
  // 底栏只提示 1–4 选择，不提示 E
  assert.deepEqual(keyHints(q.options), { rewrite: false });
  assert.equal(pickHint(q.options), '先选一个选项');
  assert.deepEqual(confirmPlan(it, q, null, null), { error: '先选一个选项' });
  assert.deepEqual(confirmPlan(it, q, 1, null), { action: 'answer', body: { option_id: 'B' } });
  // 普通决策卡有「其他，我来说」时照常提示 E
  const withCustom = [{ id: 'A' }, { id: 'D', custom: true }];
  assert.deepEqual(keyHints(withCustom), { rewrite: true });
  assert.match(pickHint(withCustom), /按 E 写下你的方向/);
  // 没有选项的事项（过期、续跑、外部反馈、变更清单）不出键位提示
  assert.equal(keyHints([]), null);
});

test('底栏主按钮按类型写清楚会做什么', () => {
  assert.equal(confirmLabel({ kind: 'decision' }, {}), '确认并下一条');
  assert.equal(confirmLabel({ kind: 'review' }, { group: 'high_confidence' }), '全部接受并下一条');
  assert.equal(confirmLabel({ kind: 'change_list' }, {}), '执行清单并下一条');
  assert.equal(confirmLabel({ kind: 'stale' }, {}), '刷新并下一条');
  assert.equal(confirmLabel({ kind: 'resumable' }, {}), '续跑并下一条');
  assert.equal(confirmLabel({ kind: 'feedback' }, {}), '交给反馈演进并下一条');
  assert.equal(confirmLabel({ kind: 'notification', actions: ['dismiss'] }, {}), '忽略并下一条');
  // 和「确认并下一条」实际发的动作一致
  assert.equal(confirmPlan({ kind: 'stale' }, {}).action, 'refresh');
  assert.equal(confirmPlan({ kind: 'notification', actions: ['dismiss'] }, {}).action, 'dismiss');
});

test('证据源不可用时不列证据冲突（筛选格、列表、计数）', () => {
  const counts = { total: 18, by_filter: { all: 18, evidence_conflict: 2, decision: 2 } };
  assert.ok(visibleFilters(counts, 'all', true).some(([k]) => k === 'evidence_conflict'));
  assert.equal(visibleFilters(counts, 'evidence_conflict', false).some(([k]) => k === 'evidence_conflict'), false);
  assert.equal(visibleFilters({ by_filter: { evidence_conflict: 0 } }, 'all', true).some(([k]) => k === 'evidence_conflict'), false);
  const items = [{ id: 'a', kind: 'decision' }, { id: 'b', kind: 'evidence_conflict' }];
  assert.deepEqual(withoutEvidence(items, false).map((x) => x.id), ['a']);
  assert.equal(withoutEvidence(items, true).length, 2);
  assert.equal(countsWithoutEvidence(counts, false).total, 16);
  assert.equal(countsWithoutEvidence(counts, false).by_filter.evidence_conflict, 0);
  assert.equal(countsWithoutEvidence(counts, false).by_filter.all, 16);
  assert.equal(countsWithoutEvidence(counts, true), counts);
});
