import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { StreamHub } from '../src/api/mock/hub.js';
import { register } from '../src/api/mock/routes-studio.js';
import {
  anchorChipLabel, backlinkView, commitSummary, evidenceShortLabel, fieldErrorMap, filterRows, flowPreviewLayout, flowchartNodeSummary,
  formFromEntry, kindSegments, lockActionTarget, lockCopy, mergeFlowchartRows, rawNotices, rejectSummary, relationText, resolutionLabel,
  rowQuery, rowStatus, summarizeFormChanges,
} from '../src/pages/studio/studioModel.js';
import { boxHeight, erData, erLayout, hasErView } from '../src/pages/studio/erModel.js';

const ref = (kind, key, name) => ({ kind, id: `${kind}:${key}`, key, name });
const DISPLAY = ref('object', 'display_qty_std', '陈列量标准');
const lock = (state) => ({
  state, changeset_id: 'cs-31', conversation_id: 'c1', run_id: null, slice: { id: 's5', name: '超量与审批提示' }, node: null,
  entries: [DISPLAY, ref('rule', 'r09_store_stricter', 'R-09 门店申请取严')],
  artifacts: [{ type: 'diagram.flowchart', key: 'overage-flow', title: '超量处理流程' }], paths: [], reason_text: '', since: null,
});

test('条目表：类型分段带计数，图示包没启用时没有「流程图」；关键字查名称和 Key', () => {
  const counts = { all: 5, object: 2, operation: 1, rule: 1, metric: 0, script: 0, flowchart: 1 };
  assert.deepEqual(kindSegments(counts, { flowchartEnabled: true }).map((s) => s.k), ['all', 'object', 'operation', 'rule', 'metric', 'flowchart', 'script']);
  assert.equal(kindSegments(counts, { flowchartEnabled: true }).find((s) => s.k === 'flowchart').plugin, true);
  assert.ok(!kindSegments({ ...counts, flowchart: null }, { flowchartEnabled: true }).some((s) => s.k === 'flowchart'));
  assert.ok(!kindSegments(counts, { flowchartEnabled: false }).some((s) => s.k === 'flowchart'));
  const rows = [{ kind: 'object', name: '陈列量标准', key: 'display_qty_std' }, { kind: 'rule', name: 'R-09 门店申请取严', key: 'r09' }];
  assert.equal(filterRows(rows, 'all', 'DISPLAY').length, 1);
  assert.equal(filterRows(rows, 'rule', '').length, 1);
  assert.equal(filterRows(rows, 'object', 'r09').length, 0);
});

test('条目表：点一行时带着对话链接给的搜索词，点页签（地址不带 q）才清掉', () => {
  assert.deepEqual(rowQuery({ q: '陈列量标准' }, 'object:display_qty_std'), { entry: 'object:display_qty_std', q: '陈列量标准' });
  assert.deepEqual(rowQuery({ entry: 'rule:r09' }, 'object:display_qty_std'), { entry: 'object:display_qty_std' });
  assert.deepEqual(rowQuery(undefined, 'rule:r09'), { entry: 'rule:r09' });
});

test('状态列：本人有草稿显示「编辑中」，其余按显示态', () => {
  assert.deepEqual(rowStatus({ status: 'editing' }).chip[0], '编辑中');
  assert.equal(rowStatus({ status: 'pending' }).st, 'pending');
  assert.equal(rowStatus({ status: 'confirmed', needs_review: true }).st, 'needs_review');
});

test('写入范围锁：不在范围里没有说法；运行中「回到对话」，合入冲突「去处理」（F3）', () => {
  assert.equal(lockCopy(lock('assistant_running'), ref('object', 'store', '门店'), '门店'), null);
  assert.equal(lockCopy({ state: 'none', entries: [DISPLAY] }, DISPLAY, '陈列量标准'), null);
  const run = lockCopy(lock('assistant_running'), DISPLAY, '陈列量标准');
  assert.equal(run.chip, '运行中');
  assert.equal(run.title, '助手正在改这部分内容，这一轮结束前只读');
  assert.equal(run.action.kind, 'conversation');
  assert.equal(run.pop, '在「超量与审批提示」的写入范围里，这一轮结束前只读');
  const wait = lockCopy(lock('pending_confirm'), DISPLAY, '陈列量标准');
  assert.equal(wait.chip, '有冲突');
  assert.equal(wait.action.label, '去处理');
  assert.equal(wait.title, '这部分内容和助手这一轮的修改有冲突，选好保留哪边前只读');
  // 自主构建：做完这部分前只读，按钮去运行页；项目内不说「合入」。F8 之前按段跑的锁带分段名
  const auto = lockCopy(lock('autobuild_slice'), DISPLAY, '陈列量标准');
  assert.equal(auto.title, '自主构建正在做「超量与审批提示」，完成前只读');
  assert.equal(auto.pop, '在「超量与审批提示」的写入范围里，自主构建做完这部分前只读');
  assert.equal(auto.action.kind, 'run');
  // F8 起不分段（「全部」）：不提段
  const full = lockCopy({ ...lock('autobuild_slice'), slice: { id: 'all', name: '全部' } }, DISPLAY, '陈列量标准');
  assert.equal(full.title, '自主构建正在改这部分内容，完成前只读');
  assert.equal(full.pop, '在写入范围里，自主构建做完这部分前只读');
  [run, wait, auto].forEach((c) => assert.doesNotMatch(`${c.title}${c.pop}${c.sub}`, /合入|主线/));
  // 决策卡改动预览里列了它：悬停换成更具体的说法，锁定条不变
  const hl = lockCopy(lock('assistant_running'), DISPLAY, '陈列量标准', { affected: [DISPLAY.id] });
  assert.equal(hl.pop, '等你决定的口径会改到它，决定前只读');
  assert.notEqual(hl.pop, run.pop);
  assert.equal(hl.title, run.title);
  // 流程图按产物判断
  assert.ok(lockCopy(lock('pending_confirm'), { type: 'diagram.flowchart', key: 'overage-flow' }, '超量处理流程'));
  assert.deepEqual(lockActionTarget({ kind: 'confirm' }, lock('pending_confirm')), { page: 'inbox' });
  assert.deepEqual(lockActionTarget({ kind: 'run' }, { run_id: 'run-3' }), { page: 'l4-run', query: { run: 'run-3' } });
});

const ENTRY = {
  id: DISPLAY.id, kind: 'object', key: 'display_qty_std', name: '陈列量标准', description: '按商品品类和到货周期确定的陈列量上下限。',
  sections: [{ level: 1, title: '陈列量标准', content_md: '…' }],
  fields: [
    { name: '商品品类', column: 'category_code', type: '文本', evidence: { anchor: 'db:a', label: 'ERP · 商品品类', scheme: 'db', confidence: 'declared', expert_confirmed: false, missing: false } },
    { name: '生效日期', column: 'effective_from', type: '日期', evidence: { anchor: null, label: '', scheme: null, confidence: null, expert_confirmed: false, missing: true } },
  ],
  relations: { declared: [{ target: ref('object', 'goods_category', '商品品类'), label: '按品类查' }, { target: ref('operation', 'display_qty_check', '陈列量核定'), label: '被读取' }], mentions: [] },
  backlinks: [{ source: ref('operation', 'display_qty_check', '陈列量核定'), relation: '读取陈列量标准' }],
  data_source: { kind: 'table', value: 'erp.replenish_std', label: '业务系统 ERP · 补货标准表' },
};

test('表单：关联只取本条目自己声明的（反向的归对方）；草稿改动摘要', () => {
  const base = formFromEntry(ENTRY);
  assert.deepEqual(base.relations, ['object:goods_category']);
  assert.deepEqual(base.data_source, { kind: 'table', value: 'erp.replenish_std' });
  assert.deepEqual(summarizeFormChanges(ENTRY, base), []);
  const form = {
    ...base,
    description: '按商品品类和到货周期确定的陈列量上下限，乳制品另有区域配额上限。',
    fields: [base.fields[0], { name: '区域配额上限', column: 'region_quota_max', type: '数值 · 箱', evidence_anchor: 'file:09#p:2' }, base.fields[1]],
  };
  const changes = summarizeFormChanges(ENTRY, form);
  assert.deepEqual(changes, ['新增属性「区域配额上限」', '修改描述']);
  assert.equal(
    commitSummary(changes, form, (a) => (a === 'file:09#p:2' ? '09 修订通知 第 2 条' : null)),
    '改动：新增属性「区域配额上限」（证据：09 修订通知 第 2 条）、修改描述。',
  );
  const removed = { ...base, fields: [base.fields[0]], relations: [], data_source: { kind: 'none' } };
  assert.deepEqual(summarizeFormChanges(ENTRY, removed), ['删除属性「生效日期」', '修改关联对象', '修改数据来源']);
  const renamedColumn = { ...base, fields: [{ ...base.fields[0], column: 'cat' }, base.fields[1]] };
  assert.deepEqual(summarizeFormChanges(ENTRY, renamedColumn), ['修改属性「商品品类」']);
});

test('提交未通过：一句话汇总错误和警告，单元格按 field 标红', () => {
  const r = {
    result: 'rejected',
    errors: [{ code: 'E', level: 'error', message: '属性「陈列上限」缺少英文名（真实列名）', field: 'fields[2].column', hint: '必填' }],
    warnings: [{ code: 'W', level: 'warning', message: '属性「生效日期」没有证据锚点也没有专家确认' }],
  };
  assert.equal(rejectSummary(r), '属性「陈列上限」缺少英文名（真实列名）；属性「生效日期」没有证据锚点也没有专家确认（警告）。');
  assert.deepEqual(fieldErrorMap(r), { 'fields[2].column': '必填' });
});

test('证据标签：库表取来源简称、推断写「推断」，资料取章节号', () => {
  assert.equal(evidenceShortLabel({ label: 'ERP · 商品品类', scheme: 'db', confidence: 'declared' }), 'ERP');
  assert.equal(evidenceShortLabel({ label: 'ERP · 适用标准', scheme: 'db', confidence: 'inferred' }), '推断');
  assert.equal(evidenceShortLabel({ label: '补货规则 §7.3', scheme: 'file' }), '§7.3');
  assert.equal(evidenceShortLabel({ label: '09 修订通知 第 2 条', scheme: 'file' }), '09 修订');
  assert.equal(evidenceShortLabel({ label: '商品陈列说明 第 3 节', scheme: 'file' }), '第 3 节');
  assert.equal(evidenceShortLabel({ missing: true }), '');
  assert.equal(anchorChipLabel('file:resource/others/02-门店补货规则.docx#p:11.4'), '§11.4');
  assert.equal(anchorChipLabel('file:resource/others/09-修订通知.docx#p:2'), '第 2 节');
  assert.equal(resolutionLabel({ resolved: true, scheme: 'file', source_label: '09-修订通知.docx', location_label: '第 2 条' }), '09 修订通知 第 2 条');
  assert.equal(resolutionLabel({ resolved: true, scheme: 'db', source_label: '业务系统 ERP', location_label: '补货标准表 · 标准上限' }), 'ERP · 标准上限');
  assert.equal(resolutionLabel({ resolved: false }), null);
});

test('编辑原文：恢复草稿、助手改过（草稿基于旧版本）、大文件只读', () => {
  const file = { revision: 'r2', truncated: false, size: 900, last_change: { source: 'assistant', at: 't', version_label: 'v0.13' } };
  assert.deepEqual(rawNotices(file, null), { restored: null, changed: null, big: null });
  const n = rawNotices(file, { content: 'x', updated_at: 'u', base_revision: 'r1' });
  assert.deepEqual(n.restored, { at: 'u' });
  assert.deepEqual(n.changed, { by: 'assistant', at: 't', version_label: 'v0.13' });
  assert.equal(rawNotices(file, { content: 'x', updated_at: 'u', base_revision: 'r2' }).changed, null);
  const big = rawNotices({ ...file, truncated: true, size: 1258291 }, { content: 'x', base_revision: 'r1' });
  assert.deepEqual(big, { restored: null, changed: null, big: { shown: 524288, size: 1258291 } });
});

test('流程图：预览坐标把连线裁到节点边上；概况计数；AR-02 的行换掉 ST-03 的流程图行', () => {
  const model = {
    nodes: [{ id: 'a', type: 'start', x: 100, y: 50 }, { id: 'b', type: 'decision', x: 100, y: 200 }, { id: 'c', type: 'step', x: 300, y: 200 }],
    edges: [{ id: 'e1', type: 'seq', from: 'a', to: 'b' }, { id: 'e2', type: 'cond', from: 'b', to: 'c', label: '是' }, { id: 'e3', from: 'b', to: 'zz' }],
  };
  const g = flowPreviewLayout(model, 10);
  assert.equal(g.viewBox, '18 21 358 225');
  assert.deepEqual(g.edges.map((e) => [e.x1, e.y1, e.x2, e.y2]), [[100, 69, 100, 164], [172, 200, 234, 200]]);
  assert.equal(flowPreviewLayout({ nodes: [] }), null);
  assert.equal(flowchartNodeSummary(model), '3 个 · 1 个判断');
  const rows = [{ id: 'object:a', kind: 'object' }, { id: 'flowchart:x', kind: 'flowchart', key: 'x', refs_out: 5, refs_in: 1 }, { id: 'script:s', kind: 'script' }];
  const merged = mergeFlowchartRows(rows, [{ key: 'x', title: '超量处理流程', status: { value: 'draft' }, locked: true }, { key: 'y', title: '新流程', status: null }]);
  assert.deepEqual(merged.map((r) => r.id), ['object:a', 'flowchart:x', 'flowchart:y', 'script:s']);
  assert.equal(merged[1].refs_out, 5);
  assert.equal(merged[1].status, 'draft');
  assert.equal(merged[1].locked, true);
  assert.equal(mergeFlowchartRows(rows, null), rows);
});

test('被谁引用：条目和产物的说法', () => {
  assert.deepEqual(backlinkView({ type: 'trace.scenario', key: 's-04', title: '场景 4' }), { name: '场景 4', kind: '场景', icon: 'flask', artifact: true });
  assert.equal(backlinkView(ref('operation', 'x', '陈列量核定')).icon, 'flow');
  assert.equal(relationText('operation_input_object'), '读取');
  assert.equal(relationText('读取陈列量标准'), '读取陈列量标准');
});

// ---------- mock 路由（只装 routes-studio.js，不依赖其他分组） ----------
function mockCtx(sc = {}) {
  const routes = [];
  const db = {};
  const hub = new StreamHub();
  const events = [];
  let seq = 100;
  let scope = { state: 'none', entries: [], artifacts: [], paths: [] };
  const card = { ...F.projectCards()[0] };
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(body == null ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    wait: () => Promise.resolve(),
    db, hub, sc, F,
    uid: () => (++seq).toString(16),
    cardOf: (id) => (id === F.MAIN_PID ? card : null),
    convKey: (pid, cid) => `conv:${pid}:${cid}`,
    convsOf: () => F.conversations(),
    emitConv: (...a) => events.push(['conv', ...a]),
    emitProj: (...a) => events.push(['proj', ...a]),
    scopeOf: () => scope,
    runReply: () => {},
  };
  register(ctx);
  const call = async (method, path, body) => {
    const u = new URL(`http://mock.local/api/v1${path}`);
    const r = routes.find((x) => x.method === method && x.re.test(u.pathname));
    assert.ok(r, `没有路由 ${method} ${path}`);
    const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {} });
    return { status: res.status, body: res.status === 204 ? null : await res.json() };
  };
  return { call, events, setScope: (s) => { scope = s; }, card };
}
const P = `/projects/${F.MAIN_PID}`;
const ID = encodeURIComponent(DISPLAY.id);

test('mock：条目表带流程图行和计数，默认选中的陈列量标准有草稿', async () => {
  const { call } = mockCtx();
  const r = await call('GET', `${P}/studio/entries`);
  assert.equal(r.body.counts.flowchart, 2);
  assert.equal(r.body.counts.all, r.body.items.length);
  assert.equal(r.body.items.find((x) => x.id === DISPLAY.id).status, 'editing');
  const e = await call('GET', `${P}/studio/entries/${ID}`);
  assert.ok(e.body.draft.form.fields.some((f) => f.column === 'region_quota_max'));
  assert.deepEqual(summarizeFormChanges(e.body, e.body.draft.form), ['新增属性「区域配额上限」', '修改描述']);
  assert.equal((await call('GET', `${P}/studio/entries/${encodeURIComponent('flowchart:overage-flow')}`)).body.code, 'endpoint_removed');
  assert.equal((await call('GET', `${P}/studio/entries/${encodeURIComponent('object:store')}/flow`)).status, 400);
});

test('mock：提交三路结果与写入范围锁', async () => {
  const pass = mockCtx();
  const r = await pass.call('POST', `${P}/studio/entries/${ID}/submit`);
  assert.equal(r.body.result, 'committed');
  assert.equal(r.body.version_label, 'v0.13');
  assert.equal(r.body.entry.draft, null);
  assert.ok(pass.events.some(([k, , type, d]) => k === 'proj' && type === 'workspace.changed' && d.source === 'studio'));
  assert.equal((await pass.call('POST', `${P}/studio/entries/${ID}/submit`)).status, 404);

  const fail = mockCtx({ submit: 'fail' });
  const f = await fail.call('POST', `${P}/studio/entries/${ID}/submit`);
  assert.equal(f.body.result, 'rejected');
  assert.equal(f.body.errors[0].field, 'fields[2].column');

  const review = mockCtx({ submit: 'review' });
  const v = await review.call('POST', `${P}/studio/entries/${ID}/submit`);
  assert.equal(v.body.result, 'needs_review');
  assert.equal(v.body.review_points.length, 2);
  assert.ok(review.events.some(([k, , , type]) => k === 'conv' && type === 'message.user'));

  const locked = mockCtx();
  locked.setScope(lock('assistant_running'));
  const l = await locked.call('POST', `${P}/studio/entries/${ID}/submit`);
  assert.equal(l.status, 423);
  assert.equal(l.body.code, 'write_scope_locked');
  // 锁住时草稿照样能暂存
  assert.equal((await locked.call('PUT', `${P}/studio/entries/${ID}/draft`, { base_revision: 'x', form: formFromEntry(ENTRY) })).status, 200);
});

test('mock：编辑原文读写、助手改过返回 409、大文件截断', async () => {
  const { call } = mockCtx({ raw: 'changed' });
  const e = (await call('GET', `${P}/studio/entries/${ID}`)).body;
  const file = (await call('GET', `${P}/files/content?path=${encodeURIComponent(e.path)}`)).body;
  assert.notEqual(file.revision, e.draft.base_revision);
  assert.equal(file.last_change.source, 'assistant');
  const conflict = await call('PUT', `${P}/files/content`, { path: e.path, content: 'x', base_revision: e.draft.base_revision });
  assert.equal(conflict.status, 409);
  assert.equal(conflict.body.details.changed_by, 'assistant');
  const ok = await call('PUT', `${P}/files/content`, { path: e.path, content: '# 陈列量标准\n', base_revision: file.revision });
  assert.equal(ok.status, 200);
  assert.equal((await call('GET', `${P}/studio/entries/${ID}`)).body.draft, null);

  const big = mockCtx({ raw: 'big' });
  const b = (await big.call('GET', `${P}/files/content?path=${encodeURIComponent(e.path)}`)).body;
  assert.equal(b.truncated, true);
  assert.equal(b.size, 1258291);
  assert.equal((await big.call('GET', `${P}/files/content?path=${encodeURIComponent('../x')}`)).status, 403);
});

test('mock：新建重名、删除被引用的条目、证据被谁引用', async () => {
  const { call } = mockCtx();
  const dup = await call('POST', `${P}/studio/entries`, { kind: 'object', name: '陈列量标准' });
  assert.equal(dup.status, 409);
  assert.equal(dup.body.details.kind, 'name');
  const ok = await call('POST', `${P}/studio/entries`, { kind: 'object', name: '促销备货', key: 'promo_stock' });
  assert.equal(ok.status, 201);
  assert.equal(ok.body.id, 'object:promo_stock');
  const ref09 = await call('DELETE', `${P}/studio/entries/${ID}`, { base_revision: 'x' });
  assert.equal(ref09.body.code, 'entry_referenced');
  assert.ok((await call('DELETE', `${P}/studio/entries/${encodeURIComponent('object:promo_stock')}`, {})).status === 200);
  const wu = await call('GET', `${P}/evidence/where-used?anchor=${encodeURIComponent('db:erp-prod/erp.replenish_std.category_code')}`);
  assert.ok(wu.body.items.some((x) => x.target.id === DISPLAY.id));
  const other = await call('GET', '/projects/bike-fleet-7b20e4a1/studio/entries');
  assert.equal(other.body.items.length, 0);
});

test('ER 视图：包选用了 er_view 才有；只画对象和对象之间的关系声明，属性最多列 8 个', () => {
  assert.equal(hasErView({ view_choices: [{ id: 'er_view', pack: 'diagram' }] }), true);
  assert.equal(hasErView({ view_choices: [{ id: 'schema_browser', pack: 'reverse' }] }), false);
  assert.equal(hasErView({}), false);
  assert.equal(hasErView(F.extensions(['trace', 'knowledge', 'codegen', 'diagram'])), true);
  assert.equal(hasErView(F.extensions(['trace'])), false);
  const graph = {
    nodes: [
      { id: 'object:a', kind: 'object', label: '补货单' }, { id: 'object:b', kind: 'object', label: '门店' },
      { id: 'object:c', kind: 'object', label: '品类' }, { id: 'operation:x', kind: 'operation', label: '提交补货' },
    ],
    edges: [
      { id: 'd1', source: 'object:a', target: 'object:b', kind: 'declared', label: '关联门店' },
      { id: 'd1b', source: 'object:a', target: 'object:b', kind: 'declared', label: '关联门店' },
      { id: 'd2', source: 'operation:x', target: 'object:a', kind: 'declared', label: '输出' },
      { id: 'm1', source: 'object:a', target: 'object:c', kind: 'mention', label: '提及' },
    ],
  };
  const many = Array.from({ length: 11 }, (_, i) => ({ name: `属性${i}`, column: `col_${i}` }));
  const data = erData(graph, { 'object:a': many, 'object:b': [{ name: '门店编号', column: 'store_code' }] });
  assert.deepEqual(data.entities.map((e) => e.id), ['object:a', 'object:b', 'object:c']);
  assert.deepEqual(data.relations, [{ id: 'd1', from: 'object:a', to: 'object:b', label: '关联门店' }]);
  const a = data.entities[0];
  assert.equal(a.fields.length, 8);
  assert.equal(a.more, 3);
  assert.equal(data.entities[2].fields.length, 0);
  const lay = erLayout(data);
  assert.equal(lay.boxes.length, 3);
  const box = lay.boxes.find((b) => b.id === 'object:a');
  assert.equal(box.h, boxHeight(a));
  // 从左到右：关系的起点实体在终点左边，连线有路径
  assert.ok(box.x < lay.boxes.find((b) => b.id === 'object:b').x);
  assert.match(lay.edges[0].d, /^M\d+ \d+( L\d+ \d+)+$/);
  assert.ok(lay.width >= 320 && lay.height >= 200);
});
