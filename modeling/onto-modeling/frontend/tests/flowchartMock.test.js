// mock 的插件产物接口（AR-01～AR-09，diagram.flowchart）：按契约的形状、版本号、冲突与锁
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register, stages } from '../src/api/mock/routes-artifacts.js';

function server({ sc = {}, lock = null } = {}) {
  const routes = [];
  const events = [];
  const cards = F.projectCards();
  const scope = { lock };
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {},
    sc,
    uid: (() => { let i = 0; return () => (i += 1).toString(16); })(),
    F,
    cardOf: (id) => cards.find((c) => c.id === id),
    scopeOf: () => scope.lock || { state: 'none', entries: [], artifacts: [], paths: [] },
    emitProj: (pid, type, data) => events.push({ type, data }),
    emitConv: (pid, cid, type, data) => events.push({ type, data }),
    hub: { history: () => [{ type: 'card', data: F.flowchartCard() }] },
    convKey: (pid, cid) => `${pid}:${cid}`,
    convsOf: () => [{ id: 'c1' }],
  };
  register(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    const r = routes.find((x) => x.method === method && x.re.test(u.pathname));
    assert.ok(r, `${method} ${path} 没有路由`);
    const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {} });
    return { status: res.status, body: await res.json() };
  };
  return { call, events, scope, cards };
}

const P = `/api/v1/projects/${F.MAIN_PID}`;
const T = 'diagram.flowchart';

test('AR-01 / AR-02 / AR-09：类型、列表（带筛选与 facets）、汇总', async () => {
  const { call } = server();
  const types = await call('GET', `${P}/artifact-types`);
  assert.equal(types.body.items[0].type, T);
  assert.equal(types.body.items[0].studio, true);
  const list = await call('GET', `${P}/artifacts/${T}`);
  assert.equal(list.body.total, 2);
  assert.deepEqual(list.body.items.map((r) => [r.key, r.title, r.fields.summary, r.status.label]), [
    ['overage-flow', '超量处理流程', '8 个节点、1 个判断', '草稿'],
    ['replenish-main-flow', '门店补货主流程', '10 个节点、2 个判断', '已确认'],
  ]);
  assert.deepEqual(list.body.facets.state.map((f) => f.count), [1, 1]);
  const drafts = await call('GET', `${P}/artifacts/${T}?filter[state]=draft`);
  assert.deepEqual(drafts.body.items.map((r) => r.key), ['overage-flow']);
  const sum = await call('GET', `${P}/artifacts/${T}/summary`);
  assert.equal(sum.body.total, 2);
  const unknown = await call('GET', `${P}/artifacts/trace.unknown`);
  assert.equal(unknown.status, 404);
  assert.equal(unknown.body.code, 'artifact_type_unknown');
});

test('AR-03：详情带 FlowchartModel、关联条目与修订号', async () => {
  const { call } = server();
  const a = (await call('GET', `${P}/artifacts/${T}/overage-flow`)).body;
  assert.equal(a.type, T);
  assert.equal(a.content.storage, 'standard');
  assert.equal(a.content.operation.name, '超量与审批提示');
  const pending = a.content.nodes.filter((x) => x.pending);
  assert.deepEqual(pending.map((x) => x.label), ['取标准上限']);
  assert.ok(a.refs.some((r) => r.field === 'operation' && r.key === 'overage_and_hint'));
  assert.ok(a.revision);
  assert.equal((await call('GET', `${P}/artifacts/${T}/nope`)).body.code, 'artifact_not_found');
});

test('AR-05：按 base_revision 保存，版本号 +1，推 workspace.changed，卡片规模跟着变', async () => {
  const { call, events, cards } = server();
  const a = (await call('GET', `${P}/artifacts/${T}/overage-flow`)).body;
  const content = structuredClone(a.content);
  content.nodes.push({ id: 'n11', type: 'step', label: '通知店长', x: 650, y: 650 });
  content.edges.push({ id: 'e11', type: 'seq', from: 'n10', to: 'n11' });
  const r = await call('PUT', `${P}/artifacts/${T}/overage-flow`, { base_revision: a.revision, content });
  assert.equal(r.status, 200);
  assert.equal(r.body.version_label, 'v0.13');
  assert.equal(cards.find((c) => c.id === F.MAIN_PID).version_label, 'v0.13');
  assert.notEqual(r.body.artifact.revision, a.revision);
  assert.equal(r.body.artifact.fields.summary, '9 个节点、1 个判断');
  const ws = events.find((x) => x.type === 'workspace.changed');
  assert.deepEqual(ws.data.artifacts.map((x) => x.key), ['overage-flow']);
  const cardEv = events.find((x) => x.type === 'card.updated');
  assert.equal(cardEv.data.summary, '9 个节点、1 个判断');
  // 旧修订号再存 → 409
  const stale = await call('PUT', `${P}/artifacts/${T}/overage-flow`, { base_revision: a.revision, content });
  assert.equal(stale.status, 409);
  assert.equal(stale.body.code, 'revision_conflict');
  assert.equal(stale.body.details.actual, r.body.artifact.revision);
});

test('AR-05：写入范围锁住时 423；?mock=fc:conflict 第一次保存 409', async () => {
  const lock = { state: 'assistant_running', entries: [], artifacts: [{ type: T, key: 'overage-flow', title: '超量处理流程' }], paths: [] };
  const locked = server({ lock });
  const a = (await locked.call('GET', `${P}/artifacts/${T}/overage-flow`)).body;
  assert.equal(a.locked, true);
  assert.equal(a.lock.state, 'assistant_running');
  const r = await locked.call('PUT', `${P}/artifacts/${T}/overage-flow`, { base_revision: a.revision, content: a.content });
  assert.equal(r.status, 423);
  assert.equal(r.body.code, 'write_scope_locked');
  // 范围外的那张照常保存
  const other = (await locked.call('GET', `${P}/artifacts/${T}/replenish-main-flow`)).body;
  assert.equal((await locked.call('PUT', `${P}/artifacts/${T}/replenish-main-flow`, { base_revision: other.revision, content: other.content })).status, 200);

  const c = server({ sc: { fc: 'conflict' } });
  const b = (await c.call('GET', `${P}/artifacts/${T}/overage-flow`)).body;
  const first = await c.call('PUT', `${P}/artifacts/${T}/overage-flow`, { base_revision: b.revision, content: b.content });
  assert.equal(first.status, 409);
  assert.equal(first.body.details.changed_by, 'assistant');
  const again = await c.call('PUT', `${P}/artifacts/${T}/overage-flow`, { base_revision: first.body.details.actual, content: b.content });
  assert.equal(again.status, 200);
});

test('AR-08 校验、AR-06 新建、AR-07 删除、AR-04 历史；压缩存储的演示参数', async () => {
  const { call } = server();
  const v = await call('POST', `${P}/artifacts/${T}/overage-flow/validate`, {});
  assert.equal(v.body.ok, true);
  assert.equal(v.body.warnings.length, 1);
  const created = await call('POST', `${P}/artifacts/${T}`, { key: 'night-restock', fields: { title: '夜间补货流程' } });
  assert.equal(created.status, 201);
  assert.equal(created.body.artifact.title, '夜间补货流程');
  const h = await call('GET', `${P}/artifacts/${T}/night-restock/history`);
  assert.equal(h.body.items.length, 1);
  const del = await call('DELETE', `${P}/artifacts/${T}/night-restock`, { base_revision: created.body.artifact.revision });
  assert.equal(del.status, 200);
  assert.equal((await call('GET', `${P}/artifacts/${T}`)).body.total, 2);

  const z = server({ sc: { fc: 'compressed' } });
  const a = (await z.call('GET', `${P}/artifacts/${T}/overage-flow`)).body;
  assert.equal(a.content.storage, 'drawio_compressed');
  assert.match(a.content.drawio_xml, /<diagram name="超量处理流程">[A-Za-z0-9+/=]+<\/diagram>/);
});

test('阶段 flowchart：系统行、请求、带流程图卡的回复、两条建议', () => {
  const h = stages.flowchart.history();
  assert.deepEqual(h.map((x) => x.type), ['notice', 'message.user', 'message.assistant', 'card', 'suggestions.updated']);
  assert.match(h[0].data.text, /^\d{2}-\d{2} 15:18 · 会话「超量与审批提示」$/);
  const card = h[3].data;
  assert.equal(card.card_type, 'flowchart');
  assert.equal(card.eyebrow, null);
  assert.deepEqual(card.artifact, { type: T, key: 'overage-flow', title: '超量处理流程' });
  const sug = h[4].data;
  assert.equal(sug.mode, 'normal');
  assert.deepEqual(sug.secondary.map((s) => s.label), ['流程没问题', '我改完了，你看看']);
  assert.equal(stages.flowchart.conversations()[0].title, '超量与审批提示');
});
