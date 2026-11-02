// 场景走查页：页面纯逻辑（scenariosModel）与 mock（trace.scenario、trace.mock 的 AR 路由、页面命令）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { lockScope, register, taskSeeds } from '../src/api/mock/routes-scenarios.js';
import {
  batchCommand, countRows, enumLabel, failDesc, failHint, filterRows, lastRun, lockBarText, mockCommand, mockDesc, passRate,
  refreshCommand, rowLocked, rowLockPop, rowState, skippedNote, sparkPaths, staleDesc, walkCommand, walkTarget,
} from '../src/pages/scenarios/scenariosModel.js';

const P = `/api/v1/projects/${F.MAIN_PID}`;
const SC = 'trace.scenario';
const row = (key, result, extra = {}) => ({ key, title: `场景 ${key}`, fields: { result, slice: 's1', ...extra.fields }, status: { value: result }, stale: result === 'stale', locked: false, ...extra });

function server({ sc = {}, lock = null, scope = null } = {}) {
  const routes = [];
  const events = [];
  const hooks = [];
  const types = [];
  const cards = F.projectCards();
  const resolutions = {};
  const db = { scope: scope || new Map(), changesets: new Map() };
  const lockBox = { lock };
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db,
    sc,
    uid: (() => { let i = 0; return () => (i += 1).toString(16); })(),
    F: { ...F, ANCHOR_RESOLUTIONS: resolutions },
    cardOf: (id) => cards.find((c) => c.id === id),
    scopeOf: () => lockBox.lock || { state: 'none', entries: [], artifacts: [], paths: [] },
    setScope: (pid, l) => { lockBox.lock = l; events.push({ type: 'scope.updated', data: l }); },
    emitProj: (pid, type, data) => events.push({ type, data }),
    emitConv: (pid, cid, type, data) => events.push({ type: `conv:${type}`, data }),
    commandHooks: hooks,
    artifactTypeDefs: types,
  };
  register(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {} });
      if (res !== undefined) return { status: res.status, body: await res.json() };
    }
    return { status: 404, body: null };
  };
  return { call, events, hooks, types, resolutions, db, lockBox };
}

// ---------- 纯逻辑 ----------
test('行状态、筛选、计数、统计卡文字', () => {
  const rows = [row('a', 'pass'), row('b', 'fail', { fields: { expected: '≤120 箱', actual: '140 箱', fail_reason: '没有取严' } }), row('c', 'stale', { fields: { stale_source: '09 修订通知', slice: 's3' } }), row('d', 'not_run')];
  assert.deepEqual(rows.map(rowState), ['pass', 'fail', 'stale', 'none']);
  assert.deepEqual(filterRows(rows, { status: 'stale' }).map((r) => r.key), ['c']);
  assert.deepEqual(filterRows(rows, { slice: 's3' }).map((r) => r.key), ['c']);
  assert.deepEqual(countRows(rows), { total: 4, pass: 1, fail: 1, stale: 1, none: 1 });
  assert.equal(failHint(rows[1]), '预期 ≤120 箱，实得 140 箱：没有取严');
  assert.equal(failDesc(rows), '场景 b');
  assert.equal(failDesc([row('a', 'pass')]), '没有失败的场景');
  assert.equal(staleDesc(rows), '来自 09 修订通知');
  assert.deepEqual(passRate({ total: 23, by: [{ value: 'pass', count: 19 }], delta_vs_previous_pct: 5 }), { pct: 83, desc: '19 / 23 · 比上一版 +5%' });
  assert.deepEqual(passRate(null, { total: 0, pass: 0 }), { pct: 0, desc: '0 / 0' });
  // 生成方式只在表格列里：有示例数据时统计卡不再带说明，还没生成时才给一句
  assert.equal(mockDesc([{ fields: { mode: 'by_scenario' } }]), null);
  assert.equal(mockDesc([]), '还没有生成');
  assert.deepEqual(skippedNote([{ entry: { name: '审批提示' }, reason: '数据来源写的是「无」' }, { entry: { name: '超量清单' }, reason: '数据来源写的是「无」' }]), { labels: '审批提示、超量清单', reason: '数据来源写的是「无」' });
  assert.equal(skippedNote([]), null);
  assert.equal(lastRun(null), '—');
  assert.equal(lastRun(new Date(2026, 9, 31, 15, 21).toISOString()), '10-31 15:21');
  const type = { schema: { fields: [{ name: 'slice', enum: [{ value: 's5', label: '超量与审批提示' }] }] } };
  assert.equal(enumLabel(type, 'slice', 's5'), '超量与审批提示');
  assert.equal(enumLabel(null, 'slice', 's9'), 's9');
  const p = sparkPaths([61, 66, 83], 110, 34);
  assert.match(p.line, /^M2\.0 31\.0 L55\.0 \d/);
  assert.equal(sparkPaths([1]), null);
});

test('页面命令：只发 S-20，文字就是对话里那句', () => {
  assert.deepEqual(batchCommand(23), { command: '/ontology-trace-batch', args: { scope: 'all' }, text: '批量推演 23 个场景' });
  assert.deepEqual(refreshCommand([row('s-03', 'stale'), row('s-04', 'stale'), row('s-06', 'stale')]).text, '补齐 3 个过期场景');
  // 后端命令名（contract-changes B4c 映射表）：补齐过期也是 /ontology-trace-batch，只带 keys
  assert.deepEqual(refreshCommand([row('s-03', 'stale')]).command, '/ontology-trace-batch');
  assert.deepEqual(refreshCommand([row('s-03', 'stale')]).args, { keys: ['s-03'] });
  assert.deepEqual(walkCommand({ key: 's-21', title: '门店申请严于标准，按门店执行' }), { command: '/ontology-trace-interactive', args: { scenario: 's-21' }, text: '交互走查「门店申请严于标准，按门店执行」' });
  assert.equal(mockCommand().text, '按场景补生成示例数据');
  const rows = [row('a', 'pass'), row('b', 'fail')];
  assert.equal(walkTarget(rows, 'a').key, 'a');
  assert.equal(walkTarget(rows, null).key, 'b');
  assert.equal(walkTarget([], null), null);
});

test('写入范围只读：按产物或场景目录判定，两态的只读条文字', () => {
  const type = { path: 'ext/trace/scenarios/{key}.md' };
  const byPath = { state: 'pending_confirm', artifacts: [], entries: [], paths: ['ext/trace/scenarios/**'], node: { id: 'trace.regression' } };
  const byRef = { state: 'assistant_running', artifacts: [{ type: SC, key: 's-21' }], entries: [], paths: [], slice: { name: '超量与审批提示' } };
  assert.equal(rowLocked(byPath, row('s-01', 'pass'), type), true);
  assert.equal(rowLocked(byRef, row('s-01', 'pass'), type), false);
  assert.equal(rowLocked(byRef, row('s-21', 'pass'), type), true);
  assert.equal(rowLocked({ state: 'none' }, row('s-21', 'pass', { locked: true }), type), false);
  const all = [row('a', 'pass'), row('b', 'pass'), row('c', 'fail'), row('d', 'stale')];
  const batch = lockBarText(byPath, all);
  assert.equal(batch.pending[0], '批量推演结果待确认，4 个场景暂时只读');
  assert.equal(batch.pending[1], '通过 2 · 失败 1 · 过期 1');
  assert.equal(batch.running[0], '助手正在推演 4 个场景，暂时只读');
  assert.equal(batch.running[1], undefined);
  const part = lockBarText({ ...byPath, node: { id: 'core.model' } }, all.slice(0, 2));
  assert.equal(part.pending[0], '助手的修改待确认，2 个场景暂时只读');
  assert.equal(part.pending[1], '通过 2 · 失败 0 · 过期 0');
  assert.equal(lockBarText(byRef, all.slice(0, 1)).running[0], '助手正在改「超量与审批提示」，1 个场景暂时只读');
  assert.equal(rowLockPop(byPath), '修改待确认，暂时只读');
  assert.equal(rowLockPop(byRef), '助手在改，暂时只读');
  assert.equal(rowLockPop({ state: 'autobuild_slice', artifacts: [], entries: [], paths: [] }), '自主构建在改，暂时只读');
});

// ---------- mock ----------
test('AR-01 / AR-02 / AR-09：类型登记、23 个场景、筛选与汇总', async () => {
  const { call, types, resolutions } = server();
  assert.deepEqual(types.map((t) => t.type), ['trace.scenario', 'trace.mock']);
  assert.ok(Object.keys(resolutions).length >= 10);
  const list = await call('GET', `${P}/artifacts/${SC}?limit=500`);
  assert.equal(list.body.total, 23);
  assert.deepEqual(countRows(list.body.items), { total: 23, pass: 19, fail: 1, stale: 3, none: 0 });
  assert.deepEqual(list.body.items.slice(0, 3).map((r) => r.key), ['s-21', 's-22', 's-03']);
  const fail = list.body.items.find((r) => rowState(r) === 'fail');
  assert.equal(failHint(fail), '预期 ≤120 箱，实得 140 箱：第 3 节和第 5 节取值没有取严');
  assert.equal(list.body.facets.slice.find((f) => f.value === 's5').count, 5);
  const stale = await call('GET', `${P}/artifacts/${SC}?stale=true`);
  assert.deepEqual(stale.body.items.map((r) => r.key), ['s-03', 's-04', 's-06']);
  const sum = await call('GET', `${P}/artifacts/${SC}/summary?trend=true`);
  assert.equal(sum.body.total, 23);
  assert.equal(sum.body.delta_vs_previous_pct, 5);
  assert.equal(sum.body.trend.at(-1).value_pct, 83);
  const mocks = await call('GET', `${P}/artifacts/trace.mock`);
  assert.equal(mocks.body.total, 6);
  assert.deepEqual(mocks.body.skipped.map((s) => s.entry.name), ['审批提示', '超量清单']);
  const pv = await call('GET', `${P}/artifacts/trace.mock/display_qty_std`);
  assert.deepEqual(pv.body.table.columns, ['goods_name', 'delivery_cycle', 'max_qty', 'std_ref']);
  assert.equal(pv.body.table.total_rows, 8);
  assert.equal((await call('GET', `${P}/artifacts/diagram.flowchart`)).status, 404);
});

test('AR-03 / AR-04 / AR-05 / AR-07：详情、历史、保存改输入后过期、写入范围锁 423', async () => {
  const { call, events } = server();
  const d = await call('GET', `${P}/artifacts/${SC}/s-21`);
  assert.deepEqual(Object.keys(d.body.sections), ['输入', '预期结论', '推演记录']);
  assert.equal(d.body.lock, null);
  const h = await call('GET', `${P}/artifacts/${SC}/s-21/history`);
  assert.equal(h.body.items.length, 1);
  const stale = await call('PUT', `${P}/artifacts/${SC}/s-21`, { base_revision: 'old', fields: { title: 'x' } });
  assert.equal(stale.status, 409);
  const ok = await call('PUT', `${P}/artifacts/${SC}/s-21`, { base_revision: d.body.revision, fields: { title: '门店申请严于标准' }, sections: { 输入: '门店：A 门店；申请 90 箱', 预期结论: '按门店执行' } });
  assert.equal(ok.status, 200);
  assert.equal(ok.body.artifact.title, '门店申请严于标准');
  assert.equal(ok.body.artifact.stale, true);
  assert.ok(events.some((e) => e.type === 'artifacts.updated' && e.data.type === SC && e.data.keys[0] === 's-21'));
  assert.equal((await call('GET', `${P}/artifacts/${SC}/s-21/history`)).body.items.length, 2);
  const del = await call('DELETE', `${P}/artifacts/${SC}/s-23`, {});
  assert.equal(del.status, 200);
  assert.equal((await call('GET', `${P}/artifacts/${SC}`)).body.total, 22);
  assert.equal((await call('PUT', `${P}/artifacts/trace.mock/store`, {})).status, 403);

  const lock = { state: 'pending_confirm', entries: [], artifacts: [], paths: ['ext/trace/scenarios/**'] };
  const locked = server({ lock });
  const rows = (await locked.call('GET', `${P}/artifacts/${SC}`)).body.items;
  assert.ok(rows.every((r) => r.locked));
  assert.equal((await locked.call('GET', `${P}/artifacts/${SC}/s-01`)).body.lock.state, 'pending_confirm');
  assert.equal((await locked.call('PUT', `${P}/artifacts/${SC}/s-01`, { fields: { title: 'x' } })).status, 423);
  assert.equal((await locked.call('DELETE', `${P}/artifacts/${SC}/s-01`, {})).status, 423);
  assert.ok(lockScope.artifacts.every((a) => a.type === SC));
  assert.deepEqual(lockScope.artifacts.map((a) => a.key), ['s-21', 's-22', 's-19', 's-20', 's-23']);
});

test('页面命令：批量验证、补齐过期、交互走查、按场景补生成跑完改结果并发 artifacts.updated', async () => {
  const { call, hooks, events } = server();
  const origin = { page: 'scenarios', label: '场景走查' };
  const find = (command, args) => hooks.find((h) => h.match({ command, origin, args }));
  assert.equal(hooks.find((h) => h.match({ command: '/ontology-trace-batch', origin: { page: 'wb-l2' } })), undefined);
  const batch = find('/ontology-trace-batch');
  assert.equal(batch.title({ text: '批量推演 23 个场景' }), '助手正在批量推演 23 个场景');
  batch.finish(F.MAIN_PID, { args: {} });
  let rows = (await call('GET', `${P}/artifacts/${SC}`)).body.items;
  assert.deepEqual(countRows(rows), { total: 23, pass: 21, fail: 1, stale: 1, none: 0 });
  assert.equal(events.at(-1).type, 'artifacts.updated');
  find('/ontology-trace-batch', { keys: ['s-04'] }).finish(F.MAIN_PID, { args: { keys: ['s-04'] } });
  rows = (await call('GET', `${P}/artifacts/${SC}`)).body.items;
  assert.equal(countRows(rows).stale, 0);
  assert.equal(events.at(-1).data.change, 'fresh');
  find('/ontology-trace-interactive').finish(F.MAIN_PID, { args: { scenario: 's-05' } });
  assert.deepEqual(events.at(-1).data.keys, ['s-05']);
  find('/ontology-mock').finish(F.MAIN_PID, { args: { by: 'scenario' } });
  assert.equal(events.at(-1).data.type, 'trace.mock');
  assert.deepEqual(taskSeeds.scenarios(), { origin, title: '助手正在批量推演 23 个场景', percent: 39, step_label: '已推演 9 / 23' });
});

test('?mock=trace:batch 把开场的锁标成批量推演（只读条用「批量推演跑完了」）', () => {
  const scope = new Map([[F.MAIN_PID, { state: 'pending_confirm', node: { id: 'core.model' }, reason_text: 'x', paths: [] }]]);
  server({ sc: { trace: 'batch' }, scope });
  assert.equal(scope.get(F.MAIN_PID).node.id, 'trace.regression');
  const untouched = new Map([[F.MAIN_PID, { state: 'pending_confirm', node: { id: 'core.model' } }]]);
  server({ sc: {}, scope: untouched });
  assert.equal(untouched.get(F.MAIN_PID).node.id, 'core.model');
});

test('批量验证的两态：开跑进锁（助手运行中）→ 跑完修改待确认、对话里出这一轮卡 → 确认生效解锁；撤回恢复原结果', async () => {
  const origin = { page: 'scenarios', label: '场景走查' };
  const body = { command: '/ontology-trace-batch', origin, text: '批量推演 23 个场景' };
  const s1 = server();
  const hook = s1.hooks.find((h) => h.match(body));
  hook.start(F.MAIN_PID, body, { conversation_id: 'c1' });
  assert.equal(s1.lockBox.lock.state, 'assistant_running');
  assert.equal(s1.lockBox.lock.node.id, 'trace.regression');
  assert.equal(s1.lockBox.lock.artifacts.length, 23);
  assert.ok((await s1.call('GET', `${P}/artifacts/${SC}`)).body.items.every((r) => r.locked));
  assert.match(hook.doneText(body), /确认生效后写进场景走查/);
  hook.finish(F.MAIN_PID, body);
  const lock = s1.lockBox.lock;
  assert.equal(lock.state, 'pending_confirm');
  const card = s1.events.find((e) => e.type === 'conv:card').data;
  assert.equal(card.card_type, 'round_changes');
  assert.equal(card.groups[0].items[0].text, '推演 23 个：通过 21 · 失败 1 · 过期 1');
  const rows = (await s1.call('GET', `${P}/artifacts/${SC}`)).body.items;
  assert.equal(lockBarText(lock, rows.filter((r) => r.locked)).pending[0], '批量推演结果待确认，23 个场景暂时只读');
  const diff = await s1.call('GET', `${P}/changesets/${lock.changeset_id}/diff`);
  assert.deepEqual(diff.body.items.map((x) => x.target.key), ['s-03', 's-06']);
  const merged = await s1.call('POST', `${P}/changesets/${lock.changeset_id}/merge`);
  assert.equal(merged.body.state, 'merged');
  assert.equal(s1.lockBox.lock.state, 'none');
  assert.equal((await s1.call('POST', `${P}/changesets/${lock.changeset_id}/merge`)).status, 409);
  const sum = await s1.call('GET', `${P}/artifacts/${SC}/summary`);
  assert.equal(sum.body.trend.at(-1).value_pct, 91);
  assert.equal(sum.body.delta_vs_previous_pct, 8);

  const s2 = server();
  const h2 = s2.hooks.find((h) => h.match(body));
  h2.start(F.MAIN_PID, body, { conversation_id: 'c1' });
  h2.finish(F.MAIN_PID, body);
  const cs = s2.lockBox.lock.changeset_id;
  assert.equal((await s2.call('POST', `${P}/changesets/${cs}/discard`)).body.state, 'discarded');
  assert.equal(s2.lockBox.lock.state, 'none');
  assert.deepEqual(countRows((await s2.call('GET', `${P}/artifacts/${SC}`)).body.items), { total: 23, pass: 19, fail: 1, stale: 3, none: 0 });

  // 开跑时已经有别的锁：不动锁、不建变更集
  const other = { state: 'pending_confirm', entries: [], artifacts: [], paths: [], node: { id: 'core.model' } };
  const s3 = server({ lock: other });
  const h3 = s3.hooks.find((h) => h.match(body));
  h3.start(F.MAIN_PID, body, { conversation_id: 'c1' });
  h3.finish(F.MAIN_PID, body);
  assert.equal(s3.lockBox.lock, other);
  assert.equal(s3.db.changesets.size, 0);
});
