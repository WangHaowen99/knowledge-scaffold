import assert from 'node:assert/strict';
import test from 'node:test';
import * as D from '../src/api/mock/incremental-data.js';
import { mainItems } from '../src/api/mock/inbox-data.js';
import * as M from '../src/pages/workbench/incremental/model.js';

const P = (n, confidence, state = 'pending', extra = {}) => ({
  id: `cp-${n}`, n, condition: `条件 ${n}`, value: `取值 ${n}`, scope: '陈列量核定', anchor: `file:a#p:${n}`, anchor_label: `09 §${n}`,
  confidence, confidence_note: null, state, rewritten_text: null, impact: [], ...extra,
});
const inboxList = () => mainItems().head.find(([it]) => it.kind === 'change_list')[1];

// ---------- 纯逻辑 ----------
test('执行前的确认计划：低把握没单独确认就拦下，其余没确认的一并接受', () => {
  const pts = [P(1, 'high'), P(2, 'medium'), P(3, 'low'), P(4, 'high', 'accepted'), P(5, 'low', 'rewritten')];
  const plan = M.executePlan(pts);
  assert.deepEqual(plan.blocked.map((p) => p.n), [3]);
  assert.deepEqual(plan.accept, ['cp-1', 'cp-2']);
  pts[2].state = 'accepted';
  assert.deepEqual(M.executePlan(pts), { blocked: [], accept: ['cp-1', 'cp-2'] });
  assert.deepEqual(M.highPending(pts).map((p) => p.n), [1]);
});

test('把握低的悬浮说明、清单副标题、出处短标签', () => {
  // 悬浮只给后端的原因；没有原因时写「把握低」
  assert.equal(M.lowPop({ confidence_note: '修订通知只写了「每批」，没说批的口径' }), '修订通知只写了「每批」，没说批的口径');
  assert.equal(M.lowPop({ confidence_note: ' 是上限还是建议值？ ' }), '是上限还是建议值？');
  assert.equal(M.lowPop({}), '把握低');
  // 副标题只写来源，状态由表格的「把握」列和「需你确认」标记表达：各种状态下都一样
  const src = { path: 'resource/others/09-修订通知.docx', label: '09 修订通知' };
  const subs = [
    { source: src, points: [P(1, 'high'), P(4, 'low')] },
    { source: src, points: [P(1, 'high'), P(4, 'low', 'accepted')] },
    { source: src, points: [P(1, 'high', 'accepted')] },
    { source: src, state: 'executed', points: [] },
  ].map((cl) => M.listSubtitle(cl));
  assert.deepEqual(subs, Array(4).fill('来自 09-修订通知'));
  assert.equal(M.sourceName({ label: '09 修订通知' }), '09 修订通知');
  assert.equal(M.shortAnchor('09 §6.3'), '§6.3');
  assert.equal(M.shortAnchor('§2'), '§2');
  assert.equal(M.shortAnchor('手动补充'), '手动补充');
});

test('表格三列：改写后的说法放在取值列', () => {
  assert.deepEqual(M.pointCells(P(1, 'high')), { condition: '条件 1', value: '取值 1', scope: '陈列量核定' });
  assert.equal(M.pointCells(P(1, 'high', 'rewritten', { rewritten_text: '按到货批次检查' })).value, '按到货批次检查');
  // 弹窗里改了三列再保存的，改写说法就是三列拼起来，照常分列显示
  assert.equal(M.pointCells(P(1, 'high', 'rewritten', { rewritten_text: '条件 1 取值 1 陈列量核定' })).value, '取值 1');
});

test('弹窗的编辑行：改过才算脏，校验，整份提交，底部说明', () => {
  const rows = M.toRows([P(1, 'high', 'accepted'), P(2, 'medium'), P(3, 'low')]);
  assert.equal(M.isDirty(rows), false);
  const edited = rows.map((r) => (r.n === 2 ? { ...r, value: ' 取值 2b ' } : r));
  assert.equal(M.isDirty(edited), true);
  const add = M.newRow(edited);
  assert.equal(add.n, 4);
  assert.equal(M.validateRow(add), '条件不能为空');
  assert.equal(M.validateRow({ ...add, condition: '门店等级' }), '取值和范围至少写一项');
  const inputs = M.toInputs([...edited, { ...add, condition: '门店等级', scope: '补货申请与门店需求' }]);
  assert.deepEqual(inputs[0], { condition: '条件 1', value: '取值 1', scope: '陈列量核定', anchor: 'file:a#p:1', id: 'cp-1' });
  assert.equal(inputs[1].value, '取值 2b');
  assert.equal(inputs[1].rewritten_text, '条件 2 取值 2b 陈列量核定');
  assert.equal(inputs[3].id, undefined, '新增条目不带 id');
  assert.equal(M.dialogNote(rows, new Set([rows[1].key])), '第 2 行在编辑；第 3 行把握低，必须单独确认。');
  assert.equal(M.dialogNote(rows, new Set()), '第 3 行把握低，必须单独确认。');
  const lowEdited = rows.map((r) => (r.n === 3 ? { ...r, value: '每批' } : r));
  assert.equal(M.dialogNote(lowEdited, new Set()), '', '改写过的低把握行不再提示，没有要提示的就不出说明');
});

test('影响面、回归、这一轮的文字', () => {
  const imp = D.impactOf(1800);
  assert.equal(M.secs(imp.duration_ms), '1.8 秒');
  assert.equal(M.phasesText(imp.phases), '按变更点定位产物 0.4 秒 · 沿引用算下游 1.0 秒 · 挑回归场景 0.4 秒');
  const reg = D.regressionCard();
  assert.deepEqual(M.regressionSummary(reg), { passed: 5, total: 6, failed: 1, affected: 4, guard: 2 });
  assert.equal(M.regressionSummary(D.regressionCard(true)).failed, 0);
  const round = D.roundCard({ csId: 'cs-1', nextVersion: 'v0.13' });
  const lines = M.roundLines(round);
  assert.equal(lines[0].verb, '修改');
  assert.equal(lines.at(-1).icon, 'flask');
  assert.equal(lines.at(-1).verb, '刷新');
  assert.equal(M.remainingNote(round), '还有 1 个场景不通过，确认后它留在待处理');
  assert.equal(M.remainingNote({ ...round, state: 'merged' }), '不通过的 1 个场景已留在待处理');
  assert.equal(M.remainingNote(D.roundCard({ csId: 'cs-1', nextVersion: 'v0.13', fixed: true })), '');
  assert.deepEqual(M.feedbackToAnalyze(D.feedbackCard()).map((x) => x.i), [1, 2], '和变更点对上的用例随这次执行一起回归');
  assert.equal(M.feedbackSource({ service: '评测服务', run: 'eval-1101-0910' }), '来自评测服务 · 运行 eval-1101-0910');
  assert.equal(M.feedbackSource({ service: '评测服务', run: '运行 #42' }), '来自评测服务 · 运行 #42');
  const fix = M.fixCommand('cl-09', D.SCENARIOS.affected[2], 'c1');
  assert.deepEqual(fix, { command: '/ontology-update', args: { change_list_id: 'cl-09', fix_scenarios: ['s-06'] }, text: '修复场景 6', origin: { page: 'wb-l2', label: '增量更新' }, conversation_id: 'c1' });
});

test('进入增量更新：从待处理找清单、新对话的名字、没有清单时主推「开始增量更新」', () => {
  assert.equal(M.changeListIdFromInbox([{ id: 'decision:q', kind: 'decision' }, { id: 'change_list:cl-09', kind: 'change_list' }]), 'cl-09');
  assert.equal(M.changeListIdFromInbox([]), null);
  assert.equal(M.conversationTitleFor('resource/others/09-修订通知.docx'), '09 修订通知');
  assert.equal(M.conversationTitleFor(null), '增量更新');
  const server = { mode: 'waiting_decision', primary: { id: 'w', label: '先做上面的决定', disabled: true }, secondary: [{ id: 'i', label: '其他待处理 17', primary: false, action: { type: 'navigate', page: 'inbox' } }], all_commands: true };
  const notice = { kind: 'new_materials', files: ['resource/others/09-修订通知.docx'], text: '资料区有 1 个新文件' };
  const s = M.withStartSuggestion(server, notice);
  assert.equal(s.mode, 'normal');
  assert.deepEqual(s.primary.action, { type: 'start_incremental', reference: 'resource/others/09-修订通知.docx' });
  assert.deepEqual(s.secondary.map((x) => x.id), ['i'], '禁用的主推不留');
  const state = { order: ['a', 'b'], items: { a: { kind: 'card', card: { card_type: 'impact' } }, b: { kind: 'card', card: { card_type: 'change_list', change_list_id: 'cl-09' } } } };
  assert.equal(M.hasChangeListCard(state), true);
  assert.equal(M.latestChangeListCard(state).change_list_id, 'cl-09');
  assert.equal(M.hasChangeListCard({ order: [], items: {} }), false);
});

// ---------- mock 数据 ----------
test('清单就是待处理里的 cl-09：条数、把握、状态照旧，只补三列和所在对话', () => {
  const raw = inboxList();
  const cl = D.overlay(raw);
  assert.equal(cl.id, 'cl-09');
  assert.equal(cl.conversation_id, D.INC_CID);
  assert.equal(cl.points.length, raw.points.length);
  assert.deepEqual(cl.points.map((p) => p.confidence), raw.points.map((p) => p.confidence));
  assert.ok(cl.points.every((p) => p.condition && p.value && p.scope));
  assert.equal(cl.points.find((p) => p.confidence === 'low').n, 4);
  assert.equal(raw.points[0].scope, '', '不改待处理那份');
  // 改过的条目不再套默认三列
  const edited = { ...raw, points: raw.points.map((p, i) => (i ? p : { ...p, condition: '自定', value: 'v', scope: 's' })) };
  assert.equal(D.overlay(edited).points[0].condition, '自定');
});

test('对话历史：等确认时有清单、影响面、外部反馈，建议条主推确认清单并执行；执行后有回归和这一轮', () => {
  const cl = D.overlay(inboxList());
  const impact = D.impactOf();
  const pre = D.incHistory({ pid: 'p', cl, impact, others: 17 });
  const cards = pre.filter((e) => e.type === 'card').map((e) => e.data.card_type);
  assert.deepEqual(cards, ['change_list', 'impact', 'external_feedback']);
  const sug = pre.at(-1);
  assert.equal(sug.type, 'suggestions.updated');
  assert.deepEqual(sug.data.primary.action, { type: 'execute_change_list', change_list_id: 'cl-09' });
  assert.deepEqual(sug.data.secondary[0].action, { type: 'navigate', page: 'incremental', params: { dialog: 'change_list', change_list_id: 'cl-09' } });
  assert.equal(sug.data.secondary[1].label, '其他待处理 17');
  assert.ok(pre.find((e) => e.type === 'progress').data.steps[1].text.includes('6 个产物、6 个回归场景'));
  assert.equal(new Set(pre.map((e) => e.id)).size, pre.length, 'seq 不重复');

  const post = D.incHistory({ pid: 'p', cl, impact, others: 17, phase: 'executed', csId: 'cs-9', version: 'v0.13' });
  const round = post.find((e) => e.type === 'card' && e.data.card_type === 'round_changes').data;
  assert.equal(round.change_list_id, 'cl-09');
  assert.equal(round.state, 'pending_confirm');
  assert.equal(round.remaining_failures, 1);
  assert.equal(post.find((e) => e.type === 'card' && e.data.card_type === 'impact').data.executed, true);
  assert.equal(post.at(-1).data.mode, 'pending_confirm');
  assert.deepEqual(post.at(-1).data.primary.action, { type: 'merge_changeset', changeset_id: 'cs-9' });
  const merged = D.incHistory({ pid: 'p', cl, impact, others: 17, phase: 'merged', csId: 'cs-9', version: 'v0.13' });
  assert.ok(merged.some((e) => e.type === 'notice' && e.data.text === '这一轮已确认生效 · 版本 v0.13'));
  assert.equal(merged.at(-1).data.secondary[0].action.params.v, 'v0.13');
});

test('执行时那句话带上改写过的条目；CS-03 可以只看一个条目', () => {
  const cl = D.overlay(inboxList());
  assert.equal(D.executeText(cl), '确认清单并执行');
  cl.points[3] = { ...cl.points[3], state: 'rewritten', rewritten_text: '临期检查频次 每批' };
  assert.equal(D.executeText(cl), '确认清单并执行（第 4 条已改写：临期检查频次 每批）');
  assert.equal(D.diffItems().length, 6);
  const one = D.diffItems('rule:r11_temp_increase');
  assert.equal(one.length, 1);
  assert.ok(one[0].sections[0].lines.some((l) => l.kind === 'add'));
});

// ---------- 整个 mock 服务端：和待处理共用一份清单，确认 → 执行 → 回归 → 确认生效 ----------
const PID = 'retail-restock-3f9a1c2d';
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

async function server(mock) {
  globalThis.window = { location: { search: mock ? `?mock=${mock}` : '' }, localStorage: { getItem: () => null } };
  const { createMockServer } = await import('../src/api/mock/index.js');
  const s = createMockServer();
  delete globalThis.window;
  const call = async (method, path, body) => {
    const res = await s.fetch(`/api/v1/projects/${PID}${path}`, {
      method, headers: { Authorization: 'Bearer mock.zhangsan.1' }, body: body ? JSON.stringify(body) : undefined,
    });
    return { status: res.status, body: res.status === 204 ? null : await res.json() };
  };
  const conv = (cid) => s.hub.history(`conv:${PID}:${cid}`);
  return { s, call, conv };
}

test('mock：增量更新阶段从接受到确认生效走一遍', async () => {
  const { s, call, conv } = await server('stage:incremental');
  const convs = (await call('GET', '/conversations')).body;
  assert.equal(convs.items[0].id, D.INC_CID);
  assert.equal(convs.live.conversation_id, D.INC_CID);
  const cl = (await call('GET', '/change-lists/cl-09')).body;
  assert.equal(cl.conversation_id, D.INC_CID);
  const before = (await call('GET', '/inbox')).body.counts.total;

  // 低把握的不能批量；高把握一键接受，待处理那边同步
  assert.equal((await call('POST', '/change-lists/cl-09/accept', { point_ids: ['cp-3', 'cp-4'] })).body.code, 'low_confidence_batch');
  const acc = (await call('POST', '/change-lists/cl-09/accept', { high_confidence: true })).body;
  assert.deepEqual(acc.points.filter((p) => p.state === 'accepted').map((p) => p.n), [1, 2, 5]);
  const item = (await call('GET', `/inbox/${encodeURIComponent('change_list:cl-09')}`)).body;
  assert.equal(item.detail.points[0].state, 'accepted');

  // 还有没确认的不能执行
  const no = await call('POST', '/change-lists/cl-09/execute', { base_revision: acc.revision });
  assert.equal(no.status, 400);
  assert.deepEqual(no.body.details.unconfirmed, ['cp-3', 'cp-4']);
  // 改写第 4 条（INC-02 整份提交），再单独接受第 3 条
  const rows = M.toRows(acc.points).map((r) => (r.n === 4 ? { ...r, value: '每批（同一到货批次、同一品类算一批）' } : r));
  assert.equal((await call('PUT', '/change-lists/cl-09', { base_revision: 'stale', points: M.toInputs(rows) })).body.code, 'revision_conflict');
  const saved = (await call('PUT', '/change-lists/cl-09', { base_revision: acc.revision, points: M.toInputs(rows) })).body;
  assert.equal(saved.impact_ms, 1600);
  assert.equal(saved.change_list.points[3].state, 'rewritten');
  const ok = (await call('POST', '/change-lists/cl-09/accept', { point_ids: ['cp-3'] })).body;

  const ex = await call('POST', '/change-lists/cl-09/execute', { base_revision: ok.revision });
  assert.equal(ex.status, 202);
  const cs = ex.body.changeset_id;
  assert.equal((await call('GET', '/inbox')).body.counts.total, before - 1, '执行后清单不再算待处理');
  assert.equal((await call('GET', '/scope')).body.state, 'assistant_running');
  assert.equal((await call('POST', `/changesets/${cs}/merge`)).body.code, 'changeset_closed', '还在跑不能确认');
  assert.ok(conv(D.INC_CID).some((e) => e.type === 'message.user' && e.data.text.startsWith('确认清单并执行（第 4 条已改写')));

  await pause(3700);
  const types = conv(D.INC_CID).filter((e) => e.type === 'card').map((e) => e.data.card_type);
  assert.ok(types.includes('regression') && types.includes('round_changes'));
  assert.equal((await call('GET', '/scope')).body.state, 'pending_confirm');
  assert.equal((await call('GET', `/changesets/${cs}`)).body.next_version_label, 'v0.13');
  assert.equal((await call('GET', `/changesets/${cs}/diff`)).body.items.length, 6);

  const merged = await call('POST', `/changesets/${cs}/merge`);
  assert.equal(merged.status, 200);
  assert.equal(merged.body.version_label, 'v0.13');
  assert.equal((await call('GET', '/scope')).body.state, 'none');
  const last = conv(D.INC_CID).filter((e) => e.type === 'card.updated' && e.data.card_type === 'round_changes').at(-1).data;
  assert.equal(last.state, 'merged');
  assert.equal(last.merged_version_label, 'v0.13');
  assert.equal(s.db.cards.find((c) => c.id === PID).version_label, 'v0.13');
  assert.equal(s.db.flows.get(PID).notices.length, 0, '新资料已经处理完');
});

test('mock：执行后撤回，清单回到可编辑并回到待处理；交给助手修会重跑场景', async () => {
  const { call, conv } = await server('stage:incremental,inc:executed');
  const before = (await call('GET', '/inbox')).body.counts.total;
  assert.equal((await call('GET', '/change-lists/cl-09')).body.state, 'executed');
  assert.equal((await call('GET', '/scope')).body.state, 'pending_confirm');
  assert.equal((await call('PUT', '/change-lists/cl-09', { points: [] })).body.code, 'changeset_closed');

  const fix = await call('POST', '/commands', M.fixCommand('cl-09', D.SCENARIOS.affected[2], D.INC_CID));
  assert.equal(fix.status, 202);
  assert.equal((await call('GET', '/scope')).body.state, 'assistant_running', '修的时候这一轮回到助手运行中');
  await pause(2000);
  const reg = conv(D.INC_CID).filter((e) => e.data?.card_type === 'regression').at(-1).data;
  assert.equal(reg.passed, 6);
  assert.equal((await call('GET', '/scope')).body.state, 'pending_confirm');

  const d = await call('POST', '/changesets/cs-32/discard');
  assert.equal(d.status, 200);
  const cl = (await call('GET', '/change-lists/cl-09')).body;
  assert.equal(cl.state, 'editing');
  assert.equal((await call('GET', '/inbox')).body.counts.total, before + 1);
  assert.equal(conv(D.INC_CID).at(-1).data.primary.action.type, 'execute_change_list');
});

test('mock：其他阶段打开 incremental，第一次取清单时把它所在的对话放进对话列表', async () => {
  const { call } = await server('');
  const before = (await call('GET', '/conversations')).body.items.map((c) => c.id);
  assert.ok(!before.includes(D.INC_CID));
  const cl = (await call('GET', '/change-lists/cl-09')).body;
  const after = (await call('GET', '/conversations')).body.items;
  assert.equal(after[1].id, cl.conversation_id);
  assert.equal(after[1].title, '09 修订通知');
  const hist = (await call('GET', `/conversations/${D.INC_CID}/messages`)).body.items;
  assert.ok(hist.some((e) => e.type === 'card' && e.data.card_type === 'change_list'));
  assert.equal((await call('GET', '/change-lists/cl-09/impact')).body.stale.length, 6);
  assert.equal((await call('GET', '/change-lists/nope')).status, 404);
});
