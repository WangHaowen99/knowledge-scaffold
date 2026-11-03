import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { mockData, register, stages } from '../src/api/mock/routes-materials.js';
import {
  addSlice, blueprintPayload, budgetText, depAfter, depLabel, editableSlices, goalPending, goalQuestion, isDirty,
  mergeSlices, moveSlice, orderLabel, renameSlice, sliceInputs, toggleExcluded,
} from '../src/pages/workbench/materials/model.js';

const bp = () => mockData.blueprintOf();
const editOf = (b) => ({
  slices: editableSlices(b.slices),
  terms: Object.fromEntries(b.terms.ambiguous.map((t) => [t.id, t.chosen])),
  answers: Object.fromEntries(b.questions.map((q) => [q.id, null])),
});

test('依赖显示成当前序号；拖动后序号跟着变，依赖排到后面会提醒', () => {
  const list = editableSlices(bp().slices);
  assert.equal(depLabel(list[0], list), '无');
  assert.equal(depLabel(list[4], list), '2、3、4');
  const moved = moveSlice(list, 0, 4);
  assert.equal(moved[4].id, 's1');
  assert.equal(depLabel(moved[0], moved), '5');
  assert.ok(depAfter(moved[0], moved));
  assert.equal(depAfter(list[1], list), false);
  assert.equal(moveSlice(list, 0, 9), list);
});

test('顺序标签：没动过用服务端推荐，动过写「第 n 个」，排除的写「已排除」', () => {
  const list = editableSlices(bp().slices);
  assert.deepEqual(orderLabel(list[0], 0), { text: '推荐先做', tone: 'blue' });
  assert.deepEqual(orderLabel(list[4], 4), { text: '建议最后做', tone: 'amber' });
  const moved = moveSlice(list, 4, 0);
  assert.deepEqual(orderLabel(moved[0], 0), { text: '第 1 个', tone: 'faint' });
  assert.deepEqual(orderLabel({ ...list[1], excluded: true }, 1), { text: '已排除', tone: 'faint' });
});

test('合并：放在第一段的位置，依赖取并集并改指向新段，记下 merged_from', () => {
  const list = editableSlices(bp().slices);
  const { list: next, merged } = mergeSlices(list, ['s3', 's4']);
  assert.equal(next.length, 4);
  assert.equal(next[2].id, merged.id);
  assert.equal(merged.name, '陈列量核定、临期与损耗检查');
  assert.deepEqual(merged.merged_from, ['s3', 's4']);
  assert.deepEqual(merged.depends_on, ['s1']);
  assert.deepEqual(merged.scale, { objects: 5, rules: 4 });
  assert.deepEqual(next[3].depends_on, ['s2', merged.id]);
  assert.equal(mergeSlices(list, ['s1']).merged, null);
});

test('排除再点一次就恢复；改名去空格、空名不改；新增一段', () => {
  const list = editableSlices(bp().slices);
  const ex = toggleExcluded(list, ['s2']);
  assert.equal(ex[1].excluded, true);
  assert.equal(toggleExcluded(ex, ['s2'])[1].excluded, false);
  assert.equal(renameSlice(list, 's3', '  陈列量与到货周期核定 ')[2].name, '陈列量与到货周期核定');
  assert.equal(renameSlice(list, 's3', '   '), list);
  const { list: added, added: s } = addSlice(list);
  assert.equal(added.length, 6);
  assert.equal(s.name, '新分段');
});

test('BP-02 请求体：只带改过的部分；分段按数组顺序给整份列表', () => {
  const b = bp();
  const edit = editOf(b);
  assert.equal(isDirty(blueprintPayload(b, edit, 'adjust')), false);
  const { list } = mergeSlices(edit.slices, ['s3', 's4']);
  const body = blueprintPayload(b, { ...edit, slices: list, terms: { ...edit.terms, 't-expiry': '临期' }, answers: { ...edit.answers, 'bq-ledger': 'A' } }, 'adjust');
  assert.equal(body.base_revision, b.revision);
  assert.equal(body.mode, 'adjust');
  // 「不在范围」已取消（F4b），请求体里不再有这一项
  assert.equal(body.out_of_scope, undefined);
  assert.equal(b.out_of_scope, undefined);
  assert.equal(body.slices.length, 4);
  assert.deepEqual(body.slices[2].merged_from, ['s3', 's4']);
  assert.equal(body.slices[0].merged_from, undefined);
  assert.deepEqual(body.term_choices, [{ term_id: 't-expiry', chosen: '临期' }]);
  assert.deepEqual(body.answers, [{ question_id: 'bq-ledger', option_id: 'A' }]);
  assert.ok(isDirty(body));
  assert.deepEqual(Object.keys(sliceInputs(edit.slices)[0]).sort(), ['core_flow', 'depends_on', 'excluded', 'id', 'name']);
});

test('自主构建预算文案', () => {
  assert.equal(budgetText({ max_duration_min: 120, max_tokens: 2000000, max_cost: { amount: 80, currency: 'CNY' } }), '时长 ≤ 2 小时 · token ≤ 2M · 费用 ≤ ¥80');
  assert.equal(budgetText({ max_duration_min: 45, max_tokens: 1500000 }), '时长 ≤ 45 分钟 · token ≤ 1.5M');
  assert.equal(budgetText(null), '—');
});

// mock 路由：BP-01 / BP-02 / BP-03
function fakeServer(stage) {
  const routes = [];
  const events = [];
  let n = 0;
  const flow = stages[stage].flow(F.flow());
  register({
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => ({ status, body }),
    err: (status, code, message, details) => ({ status, body: { code, message, details } }),
    db: { uploads: new Map() },
    uid: () => String((n += 1)),
    convsOf: () => [{ id: mockData.CID }],
    flowOf: () => flow,
    stageOf: () => stage,
    emitConv: (pid, cid, type, data) => events.push({ type, data }),
    emitProj: (pid, type, data) => events.push({ type, data }),
    runReply: () => {},
  });
  const call = (method, path, body = {}) => {
    const full = `/api/v1/projects/retail-restock-3f9a1c2d${path}`;
    const r = routes.find((x) => x.method === method && x.re.test(full));
    return r.handler({ m: full.match(r.re), body, query: new URLSearchParams(), form: null });
  };
  return { call, events, flow };
}

test('BP-02：版本号冲突 409、依赖顺序不对 400、保存调整草案号加 1 并更新蓝图卡', () => {
  const s = fakeServer('blueprint');
  const cur = s.call('GET', '/blueprint').body;
  assert.equal(cur.version_label, '草案 v2');
  assert.equal(s.call('PUT', '/blueprint', { base_revision: 'old', mode: 'adjust' }).body.code, 'revision_conflict');
  const bad = sliceInputs(moveSlice(editableSlices(cur.slices), 0, 4));
  assert.equal(s.call('PUT', '/blueprint', { base_revision: cur.revision, mode: 'adjust', slices: bad }).body.details.reason, 'dependency_order');
  const { list } = mergeSlices(editableSlices(cur.slices), ['s3', 's4']);
  const r = s.call('PUT', '/blueprint', { base_revision: cur.revision, mode: 'adjust', slices: sliceInputs(list) });
  assert.equal(r.status, 200);
  assert.equal(r.body.version_label, '草案 v3');
  assert.equal(r.body.slices.length, 4);
  assert.deepEqual(r.body.slices[2].scale, { objects: 5, rules: 4 });
  const card = s.events.filter((e) => e.type === 'card.updated').at(-1).data;
  assert.equal(card.title, '建模蓝图 · 草案 v3');
  const draft = s.call('PUT', '/blueprint', { base_revision: r.body.revision, mode: 'draft', term_choices: [] });
  assert.equal(draft.body.version_label, '草案 v3');
});

test('目标问题：没答之前卡片不能确认；目标问题放在目标下面答（F4b）', () => {
  assert.equal(goalPending({ goal_question: { id: 'q1', title: '以哪个为准？', answered: false } }), true);
  assert.equal(goalPending({ goal_question: { id: 'q1', title: '以哪个为准？', answered: true } }), false);
  assert.equal(goalPending({ goal_question: null }), false);
  const goal = { id: 'q2', kind: 'goal', title: '以哪个为准？', options: [{ id: 'A', label: '改成：按盘点生成补货单' }, { id: 'B', label: '保持原目标' }] };
  assert.equal(goalQuestion([{ id: 'q1', title: '台账要不要建成对象？' }, goal]), goal);
  assert.equal(goalQuestion([{ id: 'q1', title: '台账要不要建成对象？' }]), null);
});

test('BP-03：L2 发「开始第一段」并把资料分析标为已确认；L4 返回自主构建预览', () => {
  const s = fakeServer('blueprint');
  const cur = s.call('GET', '/blueprint').body;
  const l2 = s.call('POST', '/blueprint/confirm', { base_revision: cur.revision, next: 'L2' });
  assert.equal(l2.body.blueprint.state, 'confirmed');
  assert.match(l2.body.command_task.title, /开始第一段「补货申请与门店需求」/);
  assert.equal(s.flow.nodes.find((x) => x.id === 'core.research').state, 'confirmed');
  assert.equal(s.flow.nodes.find((x) => x.id === 'core.model').progress.label, '0/5 段');
  assert.ok(s.events.some((e) => e.type === 'message.user' && e.data.origin.page === 'wb-blueprint'));
  const s2 = fakeServer('blueprint');
  const c2 = s2.call('GET', '/blueprint').body;
  const l4 = s2.call('POST', '/blueprint/confirm', { base_revision: c2.revision, next: 'L4' });
  assert.ok(l4.body.autobuild_preview.preview_token);
  assert.equal(l4.body.autobuild_preview.slices.length, 5);
  assert.equal(l4.body.command_task, undefined);
});
