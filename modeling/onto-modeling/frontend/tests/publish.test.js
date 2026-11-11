// 发布知识包：页面纯逻辑（publishModel）与 mock（PB-01～PB-07；版本号按 Hub 规则，ADR-0091）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { nextReleaseVersion, register as registerPublish } from '../src/api/mock/routes-publish.js';
import { register as registerVersions } from '../src/api/mock/routes-versions.js';
import {
  blockingSummary, degradeNote, fixOptions, formatLabel, gateBlockedText, gateView, issueView, mergeRelease, needsSolverAck,
  publishBlockedTitle, pullView, releaseGate, releasedText, visibleGates,
} from '../src/pages/publish/publishModel.js';

// ---------- 纯逻辑 ----------

test('门禁卡片：不通过 / 还没运行 / 通过（含有提醒）三种样子，合规检查带「去运行」', () => {
  assert.deepEqual(gateView({ id: 'data_binding', status: 'fail', source: { kind: 'platform' } }), {
    cls: 'bad', icon: 'alert', chip: { k: 'fail', label: '不通过' }, action: null,
  });
  const c = gateView({ id: 'retail.compliance', status: 'not_run', source: { kind: 'pack' }, action: { type: 'start_node', node_id: 'retail.compliance' } });
  assert.equal(c.cls, 'warn');
  assert.equal(c.icon, 'gate');
  assert.deepEqual(c.chip, { k: 'blocked', label: '未通过' });
  assert.equal(c.action.label, '去运行');
  assert.equal(gateView({ id: 'rev.anchor_coverage', status: 'warn', source: { kind: 'pack' } }).cls, 'ok');
  assert.equal(gateView({ id: 'rev.anchor_coverage', status: 'warn', source: { kind: 'pack' } }).icon, 'quote');
  assert.equal(gateView({ id: 'solver_contract', status: 'pass' }).icon, 'badge');
  // 求解契约要验证时发布前要确认未验证（ADR-0091）
  assert.equal(needsSolverAck({ gates: [{ id: 'solver_contract', requires_acknowledgement: true }] }), true);
  assert.equal(needsSolverAck({ gates: [{ id: 'solver_contract', requires_acknowledgement: false }] }), false);
  assert.equal(needsSolverAck({ gates: [{ id: 'data_binding' }] }), false);
  assert.equal(gateView({ id: 'retail.compliance', status: 'pass', source: { kind: 'flow' } }).icon, 'check');
  assert.deepEqual(blockingSummary({ blocking_count: 2 }), { text: '2 项挡住发布', tone: 'red' });
  assert.deepEqual(blockingSummary({ blocking_count: 0 }), { text: '全部通过', tone: 'teal' });
  assert.equal(publishBlockedTitle({ blocking_count: 2 }), '2 项未通过');
  assert.equal(publishBlockedTitle({ blocking_count: 0 }), '');
  assert.equal(formatLabel({ format: 'solver' }), '求解知识包');
  assert.equal(formatLabel({ format: 'ontology' }), '普通知识包');
});

test('数据绑定：问题标签、说明与「改为现有表」的选项', () => {
  assert.deepEqual(issueView({ kind: 'declared_missing', message: 'Schema 资料里没有这张表' }).chip, ['声明的表不存在', 'red']);
  assert.equal(issueView({ kind: 'no_columns', message: '', detail: { attr_columns: ['a', 'b'], table_columns: ['c'] } }).note, '属性英文名 a、b；表列 c');
  assert.deepEqual(issueView({ kind: 'declared_unverifiable', message: 'x' }).chip, ['无表可核对', 'amber']);
  const o = fixOptions({ declared: 'replenish_ledger', candidates: [{ table: 'erp.replenish_ledger', matched: 2 }] }, ['erp.store', 'erp.replenish_ledger']);
  assert.equal(o.placeholder, '改为 erp.replenish_ledger');
  assert.deepEqual(o.options.map((x) => [x.value, x.hint]), [['erp.replenish_ledger', '对上 2 列'], ['erp.store', '']]);
  assert.equal(fixOptions({ declared: 'crm.x', candidates: [] }, ['erp.store']).placeholder, '改为现有数据表');
});

test('门禁链：证据源不可用时不出依赖证据源的包带来的门禁', () => {
  const check = { gates: [
    { id: 'data_binding', source: { kind: 'platform' } },
    { id: 'retail.compliance', source: { kind: 'pack', pack: 'industry-retail' } },
    { id: 'rev.anchor_coverage', source: { kind: 'pack', pack: 'rev' } },
  ] };
  assert.deepEqual(visibleGates(check, false).map((g) => g.id), ['data_binding', 'retail.compliance']);
  assert.equal(visibleGates(check, true).length, 3);
  assert.deepEqual(visibleGates(null, false), []);
});

test('能不能降级：其余挡住的门禁照样挡（按钮置灰、悬停写是哪一关）；都过了就能降级发布', () => {
  const gates = [
    { id: 'data_binding', title: '数据绑定检查', status: 'fail', blocking: true, degradable: true },
    { id: 'retail.compliance', title: '合规检查', status: 'not_run', blocking: true, degradable: false },
  ];
  const n = degradeNote({ gates });
  assert.equal(n.blocked, true);
  assert.equal(n.text, '合规检查未通过');
  assert.deepEqual(degradeNote({ gates: gates.slice(0, 1) }), { blocked: false, text: '' });
  assert.equal(gateBlockedText({ gates: [{ title: '合规检查' }] }), '合规检查未通过，仍不能发布');
  assert.equal(gateBlockedText(null), '门禁链有未通过的项，仍不能发布');
});

test('发布记录：门禁一列、评测服务一列、按包版本号合并', () => {
  assert.deepEqual(releaseGate({ gates: { degraded: true, unbound_objects: ['超量清单', '审批提示'] } }), { chip: ['降级发布', 'amber'], note: '2 个对象不绑定数据表' });
  assert.deepEqual(releaseGate({ gates: { degraded: false } }).chip, ['全部通过', 'blue']);
  assert.equal(pullView({ pulls: [] }).text, '还没拉取');
  assert.equal(pullView({ pulls: [{}], feedback_count: 0 }).text, '已拉取');
  assert.equal(pullView({ pulls: [{}, {}], feedback_count: 1 }).text, '已拉取 2 次 · 回传反馈 1');
  const list = [{ version: '0.11.0', at: '2026-10-31T03:20:00Z' }, { version: '0.7.0', at: '2026-10-29T08:30:00Z' }];
  const merged = mergeRelease(list, { version: '0.19.0', at: '2026-11-01T14:44:00Z' });
  assert.deepEqual(merged.map((r) => r.version), ['0.19.0', '0.11.0', '0.7.0']);
  assert.equal(mergeRelease(merged, { version: '0.19.0', at: '2026-11-01T14:44:00Z', pulls: [{}] })[0].pulls.length, 1);
  assert.equal(releasedText({ version: '0.1.3', version_label: 'v0.19', gates: { degraded: true } }), '已发布 v0.19 · 包版本 0.1.3（降级发布）');
  // Hub 已提交、本地标签没写成时只有包版本
  assert.equal(releasedText({ version: '0.1.3', local_record_pending: true }), '已发布 包版本 0.1.3');
});

test('发布版本号按 Hub 的规则：0.1.N 递增，旧版本地发布的 0.{n}.0 不参与编号', () => {
  assert.equal(nextReleaseVersion(['0.11.0', '0.7.0']), '0.1.1');
  assert.equal(nextReleaseVersion(['0.1.57', '0.11.0']), '0.1.58');
  assert.equal(nextReleaseVersion([]), '0.1.1');
});

// ---------- mock ----------

function mockServer({ sc = {}, lock = null } = {}) {
  const routes = [];
  const events = [];
  const buffers = new Map();
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
    stageOf: () => null,
    projKey: (pid) => `proj:${pid}`,
    convKey: (pid, cid) => `conv:${pid}:${cid}`,
    convsOf: () => [{ id: 'c1' }],
    hub: { history: (key) => buffers.get(key) || [] },
    emitProj: (pid, type, data) => {
      const e = { type, data, ts: new Date().toISOString() };
      events.push(e);
      buffers.set(`proj:${pid}`, [...(buffers.get(`proj:${pid}`) || []), e]);
    },
    readFile: async () => null,
  };
  registerVersions(ctx);
  registerPublish(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user: { username: 'zhangsan' }, init: {} });
      if (res !== undefined) {
        const text = res.headers.get('Content-Type') === 'application/zip' ? null : await res.text();
        return { status: res.status, body: text ? JSON.parse(text) : null, headers: res.headers };
      }
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  // 「去运行」：FL-05 在对话里记一条「开始「合规检查」」
  const runCompliance = () => buffers.set(`conv:${F.MAIN_PID}:c1`, [{ type: 'message.user', data: { text: '开始「合规检查」' } }]);
  return { call, events, scope, cards, runCompliance };
}

const P = `/api/v1/projects/${F.MAIN_PID}`;

test('PB-01：门禁链按顺序，数据绑定与合规检查挡住发布，只有数据绑定可降级', async () => {
  const { call } = mockServer();
  const r = await call('GET', `${P}/publish/check`);
  assert.equal(r.status, 200);
  assert.equal(r.body.version_label, 'v0.19');
  assert.equal(r.body.package_name, 'retail-restock');
  assert.equal(r.body.format, 'solver');
  assert.deepEqual(r.body.gates.map((g) => [g.title, g.status, g.blocking]), [
    ['数据绑定检查', 'fail', true], ['求解契约', 'pass', false], ['包结构检查', 'pass', false],
    ['合规检查', 'not_run', true], ['证据出处检查', 'warn', false],
  ]);
  assert.equal(r.body.gates[0].summary, '3 个对象和数据表对不上');
  assert.equal(r.body.gates[1].summary, '求解契约静态检查通过');
  assert.equal(r.body.gates[1].requires_acknowledgement, true);
  assert.deepEqual(r.body.gates[3].action, { type: 'start_node', node_id: 'retail.compliance' });
  assert.equal(r.body.blocking_count, 2);
  assert.equal(r.body.degradable, true);
  assert.equal(r.body.binding.issues.length, 3);
  assert.equal(r.body.binding.bound_objects, 12);
  assert.equal(r.body.last_release.version, '0.11.0');
  // v0.11 之前没启用求解契约：按普通知识包，没有求解契约这一关
  const old = await call('GET', `${P}/publish/check?commit=f07c2d96`);
  assert.equal(old.body.format, 'ontology');
  assert.ok(!old.body.gates.some((g) => g.id === 'solver_contract'));
  assert.equal((await call('GET', `${P}/publish/check?commit=zz`)).body.code, 'invalid_commit');
});

test('PB-03：门禁不过 422；降级只放宽数据绑定；求解包要确认未验证；同一 request_id 重试得到同一个 Hub 版本', async () => {
  const { call, events, runCompliance } = mockServer();
  const r1 = await call('POST', `${P}/releases`, {});
  assert.equal(r1.status, 422);
  assert.equal(r1.body.code, 'gate_blocked');
  assert.deepEqual(r1.body.details.gates.map((g) => g.title), ['数据绑定检查', '合规检查']);
  const r2 = await call('POST', `${P}/releases`, { degrade: true });
  assert.deepEqual(r2.body.details.gates.map((g) => g.title), ['合规检查']);
  runCompliance();
  const check = await call('GET', `${P}/publish/check`);
  assert.equal(check.body.gates[3].status, 'pass');
  assert.equal(check.body.blocking_count, 1);
  const unconfirmed = await call('POST', `${P}/releases`, { degrade: true, request_id: 'r1' });
  assert.equal(unconfirmed.status, 409);
  assert.equal(unconfirmed.body.code, 'solver_validation_acknowledgement_required');
  const r3 = await call('POST', `${P}/releases`, { degrade: true, request_id: 'r1', solver_unverified_acknowledged: true });
  assert.equal(r3.status, 201);
  assert.equal(r3.body.version, '0.1.1');
  assert.equal(r3.body.tag, 'release/0.1.1');
  assert.equal(r3.body.format_label, '求解知识包');
  assert.deepEqual(r3.body.gates, { all_passed: false, degraded: true, unbound_objects: ['补货台账', '门店特殊需求', '超量清单'] });
  assert.ok(events.some((e) => e.type === 'release.updated' && e.data.release.version === '0.1.1'));
  const again = await call('POST', `${P}/releases`, { degrade: true, request_id: 'r1', solver_unverified_acknowledged: true });
  assert.equal(again.body.sha256, r3.body.sha256);
  assert.equal(again.body.version, '0.1.1');
  const list = await call('GET', `${P}/releases`);
  assert.deepEqual(list.body.items.map((x) => x.version), ['0.1.1', '0.11.0', '0.7.0']);
  // 新的请求是 Hub 的下一个版本
  const next = await call('POST', `${P}/releases`, { degrade: true, request_id: 'r2', solver_unverified_acknowledged: true });
  assert.equal(next.body.version, '0.1.2');
  // 版本列表上标出已发布
  const versions = await call('GET', `${P}/versions`);
  assert.equal(versions.body.items[0].release, '0.1.2');
});

test('PB-02：改数据来源提交新版本，问题消失；表名不存在 400、锚点 404、锁住 423', async () => {
  const { call, events } = mockServer();
  const check = await call('GET', `${P}/publish/check`);
  const ledger = check.body.binding.issues.find((x) => x.object.key === 'replenish_ledger');
  assert.equal((await call('PUT', `${P}/objects/replenish_ledger/data-source`, { kind: 'table', value: 'erp.nope', base_revision: ledger.revision })).body.code, 'invalid_argument');
  assert.equal((await call('PUT', `${P}/objects/replenish_ledger/data-source`, { kind: 'anchor', value: 'db:erp-prod/erp.store' })).body.code, 'anchor_not_found');
  assert.equal((await call('PUT', `${P}/objects/replenish_ledger/data-source`, { kind: 'none', base_revision: 'old' })).body.code, 'revision_conflict');
  const ok = await call('PUT', `${P}/objects/replenish_ledger/data-source`, { kind: 'table', value: 'ERP.REPLENISH_LEDGER', base_revision: ledger.revision });
  assert.equal(ok.status, 200);
  assert.equal(ok.body.version_label, 'v0.20');
  assert.equal(ok.body.binding, null);
  const ws = events.filter((e) => e.type === 'workspace.changed').pop();
  assert.equal(ws.data.source, 'web');
  assert.equal(ws.data.entries[0].key, 'replenish_ledger');
  const after = await call('GET', `${P}/publish/check`);
  assert.equal(after.body.version_label, 'v0.20');
  assert.equal(after.body.binding.issues.length, 2);
  assert.equal(after.body.gates[0].summary, '2 个对象和数据表对不上');
  const versions = await call('GET', `${P}/versions`);
  assert.match(versions.body.items[0].summary, /补货台账 的数据来源改为 erp\.replenish_ledger/);

  const locked = mockServer({ lock: { state: 'pending_confirm', entries: [{ kind: 'object', id: 'object:overage_list', key: 'overage_list', name: '超量清单' }], artifacts: [], paths: [] } });
  assert.equal((await locked.call('PUT', `${P}/objects/overage_list/data-source`, { kind: 'none' })).status, 423);
});

test('PB-04～PB-07：记录详情、包清单、下载标准包与图模式变体；mock 参数 publish:ready / publish:empty', async () => {
  const { call } = mockServer();
  assert.equal((await call('GET', `${P}/releases/0.11.0`)).body.version_label, 'v0.11');
  assert.equal((await call('GET', `${P}/releases/9.9.9`)).body.code, 'release_not_found');
  const m = await call('GET', `${P}/releases/0.11.0/manifest`);
  assert.equal(m.body.package_name, 'retail-restock');
  assert.equal(m.body.schema_version, '0.1');
  assert.deepEqual(m.body.degraded, { unbound_objects: ['超量清单', '审批提示'] });
  assert.equal(m.body.solver, null);
  assert.ok(m.body.files.some((f) => f.path === 'ontology/object/陈列量标准.md'));
  assert.ok(!m.body.files.some((f) => f.path === 'ontology/object/临期损耗标准.md'));
  assert.match(m.body.manifest, /^package_name: retail-restock/);
  const g = await call('GET', `${P}/releases/0.11.0/download?variant=graph`);
  assert.equal(g.status, 200);
  assert.equal(g.headers.get('X-Package-Variant'), 'graph');
  assert.equal(g.headers.get('X-Package-Version'), '0.11.0');
  assert.equal((await call('GET', `${P}/releases/0.11.0/download?variant=xml`)).body.code, 'invalid_argument');

  const ready = mockServer({ sc: { publish: 'ready' } });
  const c = await ready.call('GET', `${P}/publish/check`);
  assert.equal(c.body.blocking_count, 0);
  assert.equal(c.body.degradable, false);
  const rec = await ready.call('POST', `${P}/releases`, { solver_unverified_acknowledged: true });
  assert.equal(rec.status, 201);
  assert.equal(rec.body.gates.all_passed, true);
  const empty = mockServer({ sc: { publish: 'empty' } });
  assert.deepEqual((await empty.call('GET', `${P}/releases`)).body.items, []);
});

test('consumer 导入规则不通过（知识包结构）挡发布且不能降级（从 develop 的 packagePublish 测试移植）', () => {
  const check = {
    gates: [
      { id: 'data_binding', title: '数据绑定', blocking: true, degradable: true },
      { id: 'package_structure', title: '知识包结构', blocking: true, degradable: false },
    ],
  };
  assert.deepEqual(degradeNote(check), { blocked: true, text: '知识包结构未通过' });
  assert.equal(degradeNote({ gates: [check.gates[0]] }).blocked, false);
});
