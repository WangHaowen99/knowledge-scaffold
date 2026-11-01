// 系统逆向建模的三个资料子页（sources、snapshots、schema）：页面纯逻辑与 mock 的证据源接口（EV-01～EV-30）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register, taskSeeds } from '../src/api/mock/routes-evidence.js';
import { SNAP } from '../src/api/mock/evidence-data.js';
import {
  checkDetail, collectBody, connText, defaultDiffPair, diffPair, domainIndex, emptyForm, fieldAnchor, fmtBudget, fmtDuration, fmtEgress,
  fmtRate, fmtWhen, formFromSource, hubTable, inWindow, jobActions, jobDuration, jobFilterQuery, jobMatches, jobOpText, jobPhase,
  layoutEr, mergeJob, monthText, parseAnchor, parseBudget, parseDbAnchor, parseEgress, parsePortDb, parseRate, parseWindow,
  pickSnapshot, previewPairs, registerBody, scopeTag, scopeWindowText, shortSnap, snapRecords, toggleSnapPick, validateForm, windowText,
} from '../src/pages/evidence/evidenceModel.js';

// ---------- 纯逻辑 ----------

test('采集任务：阶段 / 结果列、按钮、筛选、合并推送', () => {
  const run = { id: 'job-1', state: 'running', phase: '采集字段与约束', progress: { done: 28, total: 42, unit: '表', pct: 66 }, actions: ['cancel'] };
  assert.deepEqual(jobPhase(run), { text: '采集字段与约束 · 28 / 42 表', pct: 66 });
  assert.deepEqual(jobPhase({ state: 'queued', queue_reason: '等待访问窗口 22:00' }), { text: '等待访问窗口 22:00', muted: true });
  assert.equal(jobPhase({ state: 'failed', error: { message: '401：凭据已过期' } }).tone, 'red');
  assert.equal(jobPhase({ state: 'succeeded', result: { snapshot_id: SNAP.s5e } }).snapshot, SNAP.s5e);
  assert.deepEqual(jobActions({ actions: ['resume', 'bogus', 'notify_admin'] }).map((a) => [a.label, a.kind]), [['续采', 'soft'], ['联系管理员', undefined]]);
  assert.equal(jobOpText({ operation: 'snapshot', scope_text: '42 表（增量）' }), 'snapshot · 42 表（增量）');
  assert.equal(jobOpText({ operation: 'discover', scope_text: '' }), 'discover');
  assert.equal(jobFilterQuery('run'), 'active');
  assert.equal(jobFilterQuery('done'), 'finished');
  assert.equal(jobFilterQuery('all'), undefined);
  assert.ok(jobMatches(run, 'run') && !jobMatches(run, 'done') && jobMatches({ state: 'partial' }, 'done'));
  const list = [run, { id: 'job-0', state: 'succeeded' }];
  assert.equal(mergeJob(list, { id: 'job-2', state: 'queued' })[0].id, 'job-2');
  const merged = mergeJob(list, { id: 'job-1', state: 'succeeded', progress: null });
  assert.equal(merged.length, 2);
  assert.equal(merged[0].state, 'succeeded');
  assert.equal(merged[0].phase, '采集字段与约束');
});

test('时间与时长：今天只写时分，运行中的按现在算', () => {
  const now = new Date(2026, 10, 1, 22, 41).getTime();
  assert.equal(fmtWhen(new Date(2026, 10, 1, 22, 30).toISOString(), now), '22:30');
  assert.equal(fmtWhen(new Date(2026, 9, 30, 22, 15).toISOString(), now), '10-30 22:15');
  assert.equal(fmtWhen(null, now), '—');
  assert.equal(fmtDuration(4000), '4 秒');
  assert.equal(fmtDuration(310), '310 毫秒');
  assert.equal(fmtDuration(843000), '14 分 03 秒');
  assert.equal(fmtDuration(1800000), '30 分 00 秒');
  assert.equal(fmtDuration(2 * 3600000 + 5 * 60000), '2 小时 5 分');
  assert.equal(jobDuration({ state: 'running', started_at: new Date(now - 372000).toISOString() }, now), 372000);
  assert.equal(jobDuration({ state: 'queued' }, now), null);
  assert.equal(jobDuration({ state: 'succeeded', duration_ms: 5 }, now), 5);
});

test('快照：短写、记录条数、两份选择与默认对比、证据浏览器选哪份', () => {
  assert.equal(shortSnap(SNAP.s9c), '9c1d');
  assert.deepEqual(snapRecords({ sample: 1, entity: 2, rule: 0 }), [['entity', 2], ['sample', 1]]);
  assert.deepEqual(toggleSnapPick(['a', 'b'], 'c'), ['b', 'c']);
  assert.deepEqual(toggleSnapPick(['a', 'b'], 'a'), ['b']);
  const snaps = [
    { id: 'n', source: { id: 'x' }, complete: true, started_at: '2026-10-31' },
    { id: 'm', source: { id: 'x' }, complete: true, started_at: '2026-10-30' },
    { id: 'p', source: { id: 'x' }, complete: false, started_at: '2026-11-01' },
    { id: 'o', source: { id: 'y' }, complete: true, started_at: '2026-10-29' },
  ];
  assert.deepEqual(diffPair(['n', 'm'], snaps), { from: 'm', to: 'n', sameSource: true });
  assert.equal(diffPair(['n', 'o'], snaps).sameSource, false);
  assert.equal(diffPair(['n'], snaps), null);
  assert.deepEqual(defaultDiffPair(snaps), { from: 'm', to: 'n' });
  assert.equal(pickSnapshot(snaps, {}), 'n');
  assert.equal(pickSnapshot(snaps, { wanted: 'm' }), 'm');
  assert.equal(pickSnapshot(snaps, { wanted: 'zz', sourceId: 'y' }), 'o');
  assert.equal(monthText({ total: 4, succeeded: 1, partial: 1, failed: 0, cancelled: 2 }), '成功 1 · 部分 1 · 取消 2');
});

test('数据源：连接参数、范围与窗口、窗口跨午夜', () => {
  const s = { connection: { host: '192.0.2.14', port: 3306, database: 'erp', username: 'ro_onto' }, scope_summary: 'erp.* 42 表', access_window: { days: 'daily', start: '22:00', end: '06:00' } };
  assert.equal(connText(s), '192.0.2.14:3306 / erp · ro_onto');
  assert.equal(connText({ connection: { repo_url: 'git.example.com/restock.git', ref: '3f9a1c2' } }), 'git.example.com/restock.git @ 3f9a1c2');
  assert.equal(scopeWindowText(s), 'erp.* 42 表 · 22:00–06:00');
  assert.equal(scopeWindowText({ scope_summary: 'crm.*', access_window: null }), 'crm.* · 全天');
  assert.equal(windowText(s.access_window, { long: true }), '每天 22:00 – 06:00');
  assert.equal(scopeTag('erp.* 42 表'), 'erp.*（42 表）');
  assert.equal(scopeTag('crm.*'), 'crm.*');
  const at = (h, m) => new Date(2026, 10, 1, h, m).getTime();
  assert.ok(inWindow(s.access_window, at(22, 41)));
  assert.ok(inWindow(s.access_window, at(3, 0)));
  assert.ok(!inWindow(s.access_window, at(14, 0)));
  assert.ok(inWindow(null, at(14, 0)));
});

test('登记表单：一行输入的格式化与解析能来回', () => {
  const budget = { max_rows: 2000000, max_bytes: 2 * 1024 ** 3, max_duration_s: 1800 };
  assert.equal(fmtBudget(budget), '≤ 200 万行 · 2 GB · 30 分钟');
  assert.deepEqual(parseBudget(fmtBudget(budget)), budget);
  assert.equal(parseBudget('两百万行'), null);
  const rate = { concurrency: 2, qps: 20, query_timeout_s: 30 };
  assert.deepEqual(parseRate(fmtRate(rate)), rate);
  assert.equal(parseRate('并发 2'), null);
  assert.deepEqual(parseWindow('每天 22:00 – 06:00'), { ok: true, value: { days: 'daily', start: '22:00', end: '06:00', tz: 'Asia/Shanghai' } });
  assert.deepEqual(parseWindow('全天'), { ok: true, value: null });
  assert.equal(parseWindow('晚上').ok, false);
  assert.equal(parseWindow('25:00 - 06:00').ok, false);
  assert.deepEqual(parseEgress('192.0.2.14:3306, 192.0.2.15:3306'), [{ host: '192.0.2.14', port: 3306 }, { host: '192.0.2.15', port: 3306 }]);
  assert.equal(fmtEgress(parseEgress('192.0.2.14:3306')), '192.0.2.14:3306');
  assert.equal(parseEgress('192.0.2.14'), null);
  assert.deepEqual(parsePortDb('3306 / erp'), { port: 3306, database: 'erp' });
  assert.equal(parsePortDb('erp'), null);
});

const MYSQL = {
  id: 'db-mysql', kind: 'db', cred_fields: [
    { name: 'host', label: '主机' }, { name: 'port', label: '端口' }, { name: 'database', label: '库名' }, { name: 'username', label: '用户名' }, { name: 'password', label: '密码', secret: true },
  ],
};

test('登记表单校验：逐步完成；管理员要凭据和测试，申请不带密钥', () => {
  const user = { username: 'zhangsan' };
  const empty = validateForm(emptyForm(user), { provider: null });
  assert.equal(empty.current, 0);
  assert.equal(empty.done[2], false);
  const form = {
    ...emptyForm(user), provider: 'db-mysql', display_name: 'ERP 生产库', id: 'erp-prod', conn: { host: '192.0.2.14', username: 'ro_onto' },
    port_db: '3306 / erp', scope: ['erp.*'], egress: '192.0.2.14:3306',
  };
  const admin = validateForm(form, { provider: MYSQL, admin: true });
  assert.deepEqual(admin.errors, {});
  assert.deepEqual(admin.done, [true, true, false, true, true, false]);
  assert.equal(admin.current, 2);
  assert.deepEqual(validateForm({ ...form, password: 'x' }, { provider: MYSQL, admin: true, tested: true }).done, [true, true, true, true, true, true]);
  assert.deepEqual(validateForm(form, { provider: MYSQL, admin: false }).done, [true, true, true, true, true, true]);
  const bad = validateForm({ ...form, id: 'ERP', port_db: 'x', window: '晚上' }, { provider: MYSQL });
  assert.deepEqual(Object.keys(bad.errors).sort(), ['id', 'port_db', 'window']);
  assert.ok(validateForm({ ...form, note: '' }, { provider: { ...MYSQL, kind: 'rea' } }).errors.note);

  const body = registerBody({ ...form, password: 'pw' }, MYSQL, { grantProjectId: F.MAIN_PID });
  assert.deepEqual(body.connection, { host: '192.0.2.14', username: 'ro_onto', port: 3306, database: 'erp' });
  assert.deepEqual(body.secret, { password: 'pw' });
  assert.equal(body.grant_project_id, F.MAIN_PID);
  assert.deepEqual(body.access_window, { days: 'daily', start: '22:00', end: '06:00', tz: 'Asia/Shanghai' });
  assert.equal(registerBody({ ...form, password: 'pw' }, MYSQL, { withSecret: false }).secret, undefined);
});

test('编辑登记：从数据源回填，回填的能原样提交', () => {
  const s = {
    id: 'erp-prod', display_name: 'ERP 生产库', environment: 'production', provider: { id: 'db-mysql' },
    connection: { host: '192.0.2.14', port: 3306, database: 'erp', username: 'ro_onto' }, scope_allowlist: ['erp.*'], egress: [{ host: '192.0.2.14', port: 3306 }],
    access_window: { days: 'daily', start: '22:00', end: '06:00' }, budget: { max_rows: 2000000, max_bytes: 2 * 1024 ** 3, max_duration_s: 1800 },
    rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 }, masking_policy: 'mask-v3', authorization_note: '只读账号', owner: 'zhangsan',
  };
  const form = formFromSource(s);
  assert.equal(form.port_db, '3306 / erp');
  assert.equal(form.window, '每天 22:00 – 06:00');
  const v = validateForm(form, { provider: MYSQL, admin: true, editing: true, hasCred: true });
  assert.deepEqual(v.errors, {});
  assert.equal(v.current, 5);
  const body = registerBody(form, MYSQL);
  assert.deepEqual(body.connection, s.connection);
  assert.deepEqual(body.budget, s.budget);
});

test('采集确认：summary 展开成键值；发起采集的请求体', () => {
  const now = new Date(2026, 10, 1, 22, 41).getTime();
  const pairs = previewPairs({
    scope_text: 'erp.*，42 张表（增量，基于 5e2b）', window: '22:00 – 06:00', in_window_now: true, rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 },
    estimate: { rows: 860000, bytes: 1.2 * 1024 ** 3 }, readonly_methods: ['只读账号 ro_onto', '只读会话', '语句白名单'], masking: { policy: 'mask-v3', sample_rows: 20 }, ticket: { ttl_s: 600, single_use: true },
  }, now);
  assert.deepEqual(pairs.map(([k]) => k), ['范围', '访问时间窗口', '限流', '预计规模', '只读方式', '脱敏', '票据']);
  assert.equal(pairs[1][1], '22:00 – 06:00（现在 22:41，在窗口内）');
  assert.equal(pairs[3][1], '约 86 万行 · 1.2 GB（按 discover 估算）');
  assert.equal(pairs[6][1], '一次性，10 分钟过期，用过作废');
  assert.match(previewPairs({ window: '22:00 – 06:00', in_window_now: false }, now)[1][1], /不在窗口内，同意后排队到 22:00 开始/);
  assert.deepEqual(collectBody({ sourceId: 'erp-prod', operation: 'snapshot', incremental: true, base: SNAP.s5e, budget: '≤ 200 万行 · 2 GB · 30 分钟' }), {
    source_id: 'erp-prod', operation: 'snapshot', incremental_base: SNAP.s5e, budget: { max_rows: 2000000, max_bytes: 2 * 1024 ** 3, max_duration_s: 1800 },
  });
  assert.deepEqual(collectBody({ sourceId: 'erp-prod', operation: 'sample', sampleTable: ' store ', incremental: true, base: 'x' }), { source_id: 'erp-prod', operation: 'sample', sample: { selector: 'store', limit: 20 } });
  assert.equal(checkDetail({ name: 'connectivity', ok: true, detail: '192.0.2.14:3306', latency_ms: 32 }), '192.0.2.14:3306 · 32 ms');
});

test('锚点：拆出数据源、表、字段；约束不算字段', () => {
  assert.deepEqual(parseAnchor('file:resource/others/02.docx#p:9.2'), { scheme: 'file', source: 'resource', path: 'others/02.docx', fragment: 'p:9.2' });
  const a = parseDbAnchor('db:erp-prod/erp.replenish_std.max_qty');
  assert.deepEqual(a, { source: 'erp-prod', schema: 'erp', table: 'replenish_std', field: 'max_qty', tableAnchor: 'db:erp-prod/erp.replenish_std' });
  assert.equal(parseDbAnchor('db:erp-prod/erp.replenish_std.check:chk_qty_range').field, null);
  assert.equal(parseDbAnchor('db:erp-prod/erp.replenish_std').field, null);
  assert.equal(parseDbAnchor('file:x/y'), null);
  assert.equal(parseDbAnchor('db:erp-prod/erp.store%2Ex.code').table, 'store.x');
  assert.equal(fieldAnchor('db:erp-prod/erp.replenish_std', 'max_qty'), 'db:erp-prod/erp.replenish_std.max_qty');
});

const T = (name, fields) => ({ anchor: `db:s/erp.${name}`, name, fields: fields.map((n) => ({ name: n })) });
const ER = {
  tables: [T('a', ['id', 'b_id']), T('b', ['id']), T('c', ['id', 'x', 'y', 'z']), T('d', ['id']), T('e', ['id'])],
  relations: [
    { from_anchor: 'db:s/erp.c.x', to_anchor: 'db:s/erp.a.id', confidence: 'declared' },
    { from_anchor: 'db:s/erp.c.y', to_anchor: 'db:s/erp.d.id', confidence: 'observed' },
    { from_anchor: 'db:s/erp.a.b_id', to_anchor: 'db:s/erp.b.id', confidence: 'inferred' },
    { from_anchor: 'db:s/erp.e.id', to_anchor: 'db:s/erp.d.id', confidence: 'declared' },
  ],
};
const DOM = { 'db:s/erp.a': '基础', 'db:s/erp.b': '基础', 'db:s/erp.c': '基础', 'db:s/erp.d': '单据', 'db:s/erp.e': '单据' };

test('ER 排版：同域放左栏、直接相连的放中栏、其余右栏；蓝色是选中表的关系', () => {
  const lay = layoutEr(ER, 'db:s/erp.c', { domainOf: (a) => DOM[a] });
  const col = (n) => lay.boxes[`db:s/erp.${n}`].col;
  assert.deepEqual(['a', 'b', 'c', 'd', 'e'].map(col), [0, 0, 1, 1, 2]);
  assert.equal(lay.width, 676);
  assert.ok(lay.height >= 545);
  assert.equal(lay.edges.length, 4);
  assert.deepEqual(lay.edges.map((e) => e.on), [true, true, false, false]);
  // 中栏 c.x（第 2 行）→ 左栏 a.id（第 1 行）：从 c 的左边连到 a 的右边，起止点落在字段那一行
  const c = lay.boxes['db:s/erp.c'];
  const a = lay.boxes['db:s/erp.a'];
  assert.equal(lay.edges[0].d, `M245 ${c.y + 26 + 18 + 9} C 212 ${c.y + 26 + 18 + 9}, 212 ${a.y + 26 + 9}, 180 ${a.y + 26 + 9}`);
  // 左栏相邻两张表之间绕左边
  assert.match(lay.edges[2].d, /^M20 /);
  // 选中别的表时只换蓝色，不重排
  const again = layoutEr(ER, 'db:s/erp.c', { domainOf: (x) => DOM[x], selected: 'db:s/erp.e' });
  assert.deepEqual(again.edges.map((e) => e.on), [false, false, false, true]);
  assert.deepEqual(again.boxes['db:s/erp.e'], lay.boxes['db:s/erp.e']);
  assert.equal(hubTable(ER), 'db:s/erp.c');
  assert.deepEqual([...domainIndex({ domains: [{ name: '基础', entities: [{ anchor: 'x' }] }] })], [['x', '基础']]);
});

// ---------- mock ----------

function server({ role = 'admin', now = new Date(2026, 10, 1, 22, 41).getTime() } = {}) {
  const routes = [];
  const events = [];
  const timers = [];
  const hooks = [];
  let seq = 0;
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    wait: () => Promise.resolve(),
    uid: () => (seq += 1).toString(16).padStart(4, '0'),
    F,
    emitProj: (pid, type, data) => events.push({ type, data: JSON.parse(JSON.stringify(data)) }),
    commandHooks: hooks,
    setTimer: (fn) => { timers.push(fn); return timers.length; },
    now: () => now,
  };
  register(ctx);
  const user = { username: 'zhangsan', role };
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user });
      if (res !== undefined) return { status: res.status, body: res.status === 204 ? null : await res.json() };
    }
    assert.fail(`${method} ${path} 没有路由`);
  };
  const tick = () => { const fns = timers.splice(0); fns.forEach((fn) => fn()); return fns.length; };
  return { call, events, tick, hooks };
}
const P = `/api/v1/projects/${F.MAIN_PID}`;

test('EV-01：新建向导、资料分析用的 total / authorized_count 形状不变；带项目时标授权', async () => {
  const { call } = server();
  const plain = await call('GET', '/api/v1/evidence/sources');
  assert.equal(plain.body.total, 2);
  assert.equal(plain.body.authorized_count, 0);
  assert.deepEqual(plain.body.items.map((s) => s.id), ['erp-prod', 'crm-test']);
  const mine = await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}`);
  assert.equal(mine.body.authorized_count, 1);
  assert.deepEqual(mine.body.items.map((s) => s.authorized_for_project), [true, false]);
  assert.deepEqual(mine.body.items[0].last_job, { at: '2026-10-31T22:18:00+08:00', state: 'succeeded' });
  assert.deepEqual((await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}&filter=production`)).body.items.map((s) => s.id), ['erp-prod']);
  assert.deepEqual((await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}&filter=authorized`)).body.items.map((s) => s.id), ['erp-prod']);
});

test('EV-02 / EV-03 / EV-08：管理员登记并授权，普通用户只能申请、只能测已授权的源', async () => {
  const body = {
    id: 'pos-test', display_name: 'POS 测试库', provider: 'db-mysql', environment: 'test', connection: { host: '192.0.2.30', port: 3306, database: 'pos', username: 'ro' },
    secret: { password: 'pw' }, scope_allowlist: ['pos.*'], egress: [{ host: '192.0.2.30', port: 3306 }], budget: {}, rate_limit: {}, masking_policy: 'mask-v3', owner: 'zhangsan', grant_project_id: F.MAIN_PID,
  };
  const admin = server();
  const made = await admin.call('POST', '/api/v1/admin/evidence/sources', body);
  assert.equal(made.status, 201);
  assert.equal(made.body.cred_ref, 'cred://pos-test/1');
  assert.equal(made.body.authorized_for_project, true);
  assert.equal((await admin.call('POST', '/api/v1/admin/evidence/sources', body)).body.code, 'source_id_taken');
  assert.equal((await admin.call('POST', '/api/v1/admin/evidence/sources', { ...body, id: 'rea-x', provider: 'rea-binary' })).body.code, 'provider_not_installed');
  const rot = await admin.call('PUT', '/api/v1/admin/evidence/sources/erp-prod/secret', { fields: { password: 'new' } });
  assert.equal(rot.body.cred_ref, 'cred://erp-prod/4');
  const t = await admin.call('POST', '/api/v1/evidence/sources/crm-test/test', {});
  assert.equal(t.body.ok, true);
  assert.deepEqual(t.body.checks.map((c) => c.name), ['connectivity', 'readonly_account', 'statement_allowlist']);

  const user = server({ role: 'user' });
  assert.equal((await user.call('POST', '/api/v1/admin/evidence/sources', body)).status, 403);
  assert.equal((await user.call('POST', '/api/v1/evidence/source-requests', body)).body.code, 'invalid_argument');
  const { secret, ...noSecret } = body;
  assert.equal((await user.call('POST', '/api/v1/evidence/source-requests', noSecret)).status, 202);
  assert.equal((await user.call('POST', '/api/v1/evidence/sources/crm-test/test', { project_id: F.MAIN_PID })).body.code, 'source_not_authorized');
  assert.equal((await user.call('POST', '/api/v1/evidence/sources/erp-prod/test', { project_id: F.MAIN_PID })).status, 200);
  const log = await user.call('GET', `/api/v1/evidence/sources/erp-prod/access-log?project_id=${F.MAIN_PID}`);
  assert.ok(log.body.items.every((x) => x.project_id === F.MAIN_PID));
});

test('EV-13 / EV-14：生产库先预览拿 token，缺 token 返回 428（details 带新 token），token 只能用一次；进度经 evidence.job.updated 推到成功并生成快照', async () => {
  const { call, events, tick } = server();
  const req = { source_id: 'erp-prod', operation: 'snapshot', incremental_base: SNAP.s5e };
  const pv = await call('POST', `${P}/evidence/jobs/preview`, req);
  assert.equal(pv.body.requires_confirmation, true);
  assert.equal(pv.body.summary.in_window_now, true);
  assert.match(pv.body.summary.scope_text, /增量，基于 5e2b/);
  const no = await call('POST', `${P}/evidence/jobs`, req);
  assert.equal(no.status, 428);
  assert.equal(no.body.code, 'confirmation_required');
  assert.ok(no.body.details.confirmation_token);
  const ok = await call('POST', `${P}/evidence/jobs`, { ...req, confirmation_token: pv.body.confirmation_token });
  assert.equal(ok.status, 202);
  assert.equal(ok.body.id, 'job-0133');
  assert.equal(ok.body.state, 'running');
  assert.equal((await call('POST', `${P}/evidence/jobs`, { ...req, confirmation_token: pv.body.confirmation_token })).status, 428);
  let n = 0;
  while (tick()) n += 1;
  assert.ok(n >= 5);
  const last = events.filter((e) => e.type === 'evidence.job.updated' && e.data.id === 'job-0133').at(-1).data;
  assert.equal(last.state, 'succeeded');
  assert.match(last.result.snapshot_id, /^20261101-224100-[0-9a-f]{4}$/);
  const snaps = await call('GET', `${P}/evidence/snapshots`);
  assert.equal(snaps.body.items[0].id, last.result.snapshot_id);
  assert.equal(snaps.body.items.length, 5);
  assert.equal((await call('POST', `${P}/evidence/jobs/preview`, { source_id: 'crm-test', operation: 'discover' })).body.code, 'source_not_authorized');
});

test('EV-13 / EV-14：窗口外的任务排队，不推进', async () => {
  const { call, tick } = server({ now: new Date(2026, 10, 1, 14, 0).getTime() });
  const req = { source_id: 'erp-prod', operation: 'discover' };
  const pv = await call('POST', `${P}/evidence/jobs/preview`, req);
  assert.equal(pv.body.summary.in_window_now, false);
  const j = await call('POST', `${P}/evidence/jobs`, { ...req, confirmation_token: pv.body.confirmation_token });
  assert.equal(j.body.state, 'queued');
  assert.equal(j.body.queue_reason, '等待访问窗口 22:00');
  assert.equal(tick(), 0);
});

test('EV-15～EV-17：列表筛选、取消、续采、生产库重试要重新确认', async () => {
  const { call, tick } = server();
  const all = await call('GET', `${P}/evidence/jobs`);
  assert.deepEqual(all.body.items.map((j) => j.state), ['running', 'queued', 'succeeded', 'partial', 'failed', 'cancelled']);
  assert.deepEqual((await call('GET', `${P}/evidence/jobs?state=active`)).body.items.map((j) => j.id), ['job-0131', 'job-0132']);
  assert.equal((await call('GET', `${P}/evidence/jobs?state=finished`)).body.items.length, 4);
  const c = await call('POST', `${P}/evidence/jobs/job-0131/cancel`);
  assert.equal(c.body.state, 'cancelled');
  assert.deepEqual(c.body.actions, ['retry']);
  assert.equal((await call('POST', `${P}/evidence/jobs/job-0131/cancel`)).body.code, 'job_state_invalid');
  const r = await call('POST', `${P}/evidence/jobs/job-0127/resume`);
  assert.equal(r.body.resumed_from, 'job-0127');
  assert.equal(r.body.progress.done, 33);
  while (tick());
  assert.equal((await call('GET', `${P}/evidence/jobs/job-0127`)).body.actions.length, 0);
  const again = await call('POST', `${P}/evidence/jobs/job-0122/retry`);
  assert.equal(again.status, 428);
  const ok = await call('POST', `${P}/evidence/jobs/job-0122/retry`, { confirmation_token: again.body.details.confirmation_token });
  assert.equal(ok.body.operation, 'sample');
  assert.equal(ok.body.scope_text, 'store 20 行');
  assert.equal((await call('POST', '/api/v1/evidence/sources/erp-prod/notify-admin', { project_id: F.MAIN_PID, job_id: 'job-0125', reason: 'credential_expired' })).status, 202);
  assert.equal((await call('POST', '/api/v1/evidence/sources/crm-test/notify-admin', { project_id: F.MAIN_PID, reason: 'other' })).body.code, 'source_not_authorized');
});

test('EV-12 / EV-18～EV-22：统计、被引用的快照不能清理、对比 9c1d → 5e2b（乳制品区域配额 140 → 150）', async () => {
  const { call } = server();
  const sum = (await call('GET', `${P}/evidence/summary`)).body;
  assert.deepEqual(sum.snapshots, { count: 4, sources: 1, referenced: 2, last_at: '2026-10-31T22:32:03+08:00' });
  assert.equal(sum.referenced_anchors, 23);
  assert.deepEqual(sum.jobs_this_month, { total: 4, succeeded: 1, partial: 1, failed: 1, cancelled: 1 });
  assert.equal(sum.running_jobs, 1);
  assert.equal(sum.queued_jobs, 1);
  assert.equal((await call('DELETE', `${P}/evidence/snapshots/${SNAP.s9c}`)).body.code, 'snapshot_referenced');
  assert.deepEqual((await call('DELETE', `${P}/evidence/snapshots/${SNAP.s77}`)).body, { removed: true, blob_retention_days: 90 });
  const diff = (await call('GET', `${P}/evidence/snapshots/diff?from=${SNAP.s9c}&to=${SNAP.s5e}`)).body;
  assert.deepEqual(diff.counts, { added: 2, removed: 1, modified: 3, touching_confirmed: 5 });
  assert.ok(diff.items.some((x) => x.summary === '乳制品区域配额上限 140 → 150' && x.anchor === 'db:erp-prod/erp.replenish_std.region_quota_max'));
  assert.equal((await call('GET', `${P}/evidence/snapshots/diff?from=${SNAP.s9c}&to=${SNAP.s5e}&change=removed`)).body.items.length, 1);
  assert.equal((await call('GET', `${P}/evidence/snapshots/diff?from=${SNAP.s41}&to=${SNAP.s5e}`)).body.items.length, 0);
  assert.equal((await call('POST', `${P}/evidence/snapshots/diff/suggestions`, { from: SNAP.s9c, to: SNAP.s5e })).body.created, 3);
});

test('EV-23～EV-28：分层摘要 42 实体、检索、记录详情按快照变化、相邻记录、ER 那一组 9 张表', async () => {
  const { call, hooks } = server();
  const tree = (await call('GET', `${P}/evidence/summary-tree?snapshot=${SNAP.s9c}`)).body;
  assert.equal(tree.domains.reduce((n, d) => n + d.count, 0), 42);
  const std = tree.domains[0].entities.find((e) => e.name === 'erp.replenish_std');
  assert.deepEqual(std.mapped_to, { kind: 'object', id: 'object:display_qty_std', key: 'display_qty_std', name: '陈列量标准' });
  const hits = (await call('GET', `${P}/evidence/search?q=max_qty&snapshot=${SNAP.s9c}`)).body;
  assert.equal(hits.items[0].anchor, 'db:erp-prod/erp.replenish_std.max_qty');
  assert.equal(hits.items[0].parent_anchor, 'db:erp-prod/erp.replenish_std');
  assert.ok((await call('GET', `${P}/evidence/search?q=补货&type=entity&snapshot=${SNAP.s9c}`)).body.items.every((x) => x.type === 'entity'));
  const old = (await call('GET', `${P}/evidence/record?anchor=db%3Aerp-prod%2Ferp.replenish_std&snapshot=${SNAP.s9c}`)).body;
  const neu = (await call('GET', `${P}/evidence/record?anchor=db%3Aerp-prod%2Ferp.replenish_std&snapshot=${SNAP.s5e}`)).body;
  assert.equal(old.fields.find((f) => f.name === 'max_qty').note, '注释「标准上限」');
  assert.equal(neu.fields.find((f) => f.name === 'max_qty').note, '注释「标准上限（含区域配额）」');
  assert.equal(old.profile.find((p) => p.column === 'region_quota_max').stats.max, 140);
  assert.equal(neu.profile.find((p) => p.column === 'region_quota_max').stats.max, 150);
  assert.ok(old.samples.rows.length <= old.samples.max);
  assert.ok((await call('GET', `${P}/evidence/record?anchor=db%3Aerp-prod%2Ferp.goods_category&snapshot=${SNAP.s9c}`)).body.fields.some((f) => f.name === 'old_code'));
  assert.ok(!(await call('GET', `${P}/evidence/record?anchor=db%3Aerp-prod%2Ferp.goods_category&snapshot=${SNAP.s5e}`)).body.fields.some((f) => f.name === 'old_code'));
  assert.equal((await call('GET', `${P}/evidence/record?anchor=db%3Aerp-prod%2Ferp.nope`)).body.code, 'anchor_not_found');
  assert.equal((await call('GET', `${P}/evidence/record?anchor=nope`)).body.code, 'anchor_invalid');
  const lin = (await call('GET', `${P}/evidence/lineage?anchor=db%3Aerp-prod%2Ferp.replenish_std`)).body.items;
  assert.deepEqual(lin.map((x) => [x.label, x.confidence]), [['erp.goods_category', 'declared'], ['erp.std_catalog', 'inferred'], ['erp.overage_record', 'observed']]);
  const er = (await call('GET', `${P}/evidence/er?snapshot=${SNAP.s9c}&focus=db%3Aerp-prod%2Ferp.replenish_std&depth=2`)).body;
  assert.equal(er.tables.length, 9);
  assert.ok(er.relations.every((r) => r.from_anchor.startsWith('db:erp-prod/erp.')));
  assert.equal(hubTable(er), 'db:erp-prod/erp.replenish_std');
  const other = (await call('GET', `${P}/evidence/er?snapshot=${SNAP.s9c}&focus=db%3Aerp-prod%2Ferp.expiry_check&depth=1`)).body;
  assert.deepEqual(other.tables.map((t) => t.name), ['expiry_check', 'arrival_record']);
  assert.ok(hooks[0].match({ command: '/ontology-evidence-impact' }));
  assert.equal(taskSeeds.snapshots().origin.page, 'snapshots');
});
