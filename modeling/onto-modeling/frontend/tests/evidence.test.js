// 系统逆向建模的三个资料子页（sources、snapshots、schema）：页面纯逻辑与 mock 的证据源接口（EV-01～EV-30）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register, taskSeeds } from '../src/api/mock/routes-evidence.js';
import { CODE, CODE_REPO, REA_ARTIFACT, SNAP, codeRecords } from '../src/api/mock/evidence-data.js';
import { createArtifactUpload } from '../src/pages/evidence/artifactUpload.js';
import {
  checkDetail, collectBody, connText, defaultDiffPair, diffPair, domainIndex, emptyForm, fieldAnchor, fmtBudget, fmtDuration, fmtEgress,
  fmtRate, fmtWhen, formFromSource, hubTable, inWindow, jobActions, jobDuration, jobFilterQuery, jobMatches, jobOpText, jobPhase,
  layoutEr, mergeJob, monthText, parseAnchor, parseBudget, parseDbAnchor, parseEgress, parsePortDb, parseRate, parseWindow,
  pickSnapshot, previewPairs, registerBody, artifactPatch, wizardSteps, artifactText, isArtifactSource, canManageSource, collectOps, canIncremental,
  snapshotPairs, snapshotLimits, snapshotKind, isDbKind, browseTarget, binPathType, recordIcon, recordKindText, recordPairs, firstTreeAnchor, scopeTag, scopeWindowText, shortSnap, snapRecords, toggleSnapPick, validateForm, windowText,
  credState, credText, defaultEgress, repoHost, secretInputLabel, portDbPlaceholder, scopePlaceholder, snippetLines, validationText, recordValidations, splitMembers,
  withKeys, maskedColumns, profileColumns, erLegend, dbRecordIcon, untrustedText, MASKING,
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

const REA_PROVIDER = { id: 'rea-static', kind: 'rea', cred_fields: [], self_service: true, input: 'artifact', capabilities: ['snapshot'] };
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
  // 票据是内部机制，确认弹窗不列（copy-audit snapshots-18）
  assert.deepEqual(pairs.map(([k]) => k), ['范围', '访问时间窗口', '限流', '预计规模', '只读方式', '脱敏']);
  assert.equal(pairs[1][1], '22:00 – 06:00（现在 22:41，在窗口内）');
  assert.equal(pairs[3][1], '约 86 万行 · 1.2 GB（按 discover 估算）');
  assert.equal(pairs[5][1], 'mask-v3 · 样本每表最多 20 行');
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
  const call = async (method, path, body, as = user) => {
    const u = new URL(path, 'http://mock.local');
    const bin = body instanceof Uint8Array;
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: bin ? {} : body || {}, init: bin ? { body } : {}, user: as });
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
  assert.equal(plain.body.total, 4);
  assert.equal(plain.body.authorized_count, 0);
  assert.deepEqual(plain.body.items.map((s) => s.id), ['erp-prod', 'crm-test', 'restock-svc', 'sample-orders']);
  const mine = await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}`);
  assert.equal(mine.body.authorized_count, 3);
  assert.deepEqual(mine.body.items.map((s) => s.authorized_for_project), [true, false, true, true]);
  assert.deepEqual(mine.body.items[0].last_job, { at: '2026-10-31T22:18:00+08:00', state: 'succeeded' });
  assert.deepEqual((await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}&filter=production`)).body.items.map((s) => s.id), ['erp-prod']);
  assert.deepEqual((await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}&filter=authorized`)).body.items.map((s) => s.id), ['erp-prod', 'restock-svc', 'sample-orders']);
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
  assert.equal((await admin.call('POST', '/api/v1/admin/evidence/sources', { ...body, id: 'log-x', provider: 'log-file' })).body.code, 'provider_not_installed');
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
  assert.equal(snaps.body.items.length, 7);
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
  assert.deepEqual(all.body.items.map((j) => j.state), ['running', 'queued', 'succeeded', 'partial', 'failed', 'cancelled', 'succeeded', 'succeeded']);
  assert.deepEqual((await call('GET', `${P}/evidence/jobs?state=active`)).body.items.map((j) => j.id), ['job-0131', 'job-0132']);
  assert.equal((await call('GET', `${P}/evidence/jobs?state=finished`)).body.items.length, 6);
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
  assert.deepEqual(sum.snapshots, { count: 6, sources: 3, referenced: 2, last_at: '2026-10-31T22:32:03+08:00' });
  assert.equal(sum.referenced_anchors, 23);
  assert.deepEqual(sum.jobs_this_month, { total: 6, succeeded: 3, partial: 1, failed: 1, cancelled: 1 });
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
  assert.deepEqual(lin.map((x) => [x.label, x.confidence]), [['erp.goods_category', 'declared'], ['erp.std_catalog', 'inferred'], ['erp.overage_record', 'inferred']]);
  const er = (await call('GET', `${P}/evidence/er?snapshot=${SNAP.s9c}&focus=db%3Aerp-prod%2Ferp.replenish_std&depth=2`)).body;
  assert.equal(er.tables.length, 9);
  assert.ok(er.relations.every((r) => r.from_anchor.startsWith('db:erp-prod/erp.')));
  assert.equal(hubTable(er), 'db:erp-prod/erp.replenish_std');
  const other = (await call('GET', `${P}/evidence/er?snapshot=${SNAP.s9c}&focus=db%3Aerp-prod%2Ferp.expiry_check&depth=1`)).body;
  assert.deepEqual(other.tables.map((t) => t.name), ['expiry_check', 'arrival_record']);
  assert.ok(hooks[0].match({ command: '/ontology-evidence-impact' }));
  assert.equal(taskSeeds.snapshots().origin.page, 'snapshots');
});

// ---------- F13：程序逆向（自助登记、分块上传、采集、证据浏览器） ----------

test('EV-10：rea-static 已装、自助、要上传程序；其他提供方按连接参数', async () => {
  const { call } = server({ role: 'user' });
  const items = (await call('GET', '/api/v1/evidence/providers')).body.items;
  const rea = items.find((p) => p.id === 'rea-static');
  assert.deepEqual([rea.kind, rea.installed, rea.self_service, rea.input, rea.capabilities, rea.cred_fields], ['rea', true, true, 'artifact', ['snapshot'], []]);
  assert.equal(rea.max_artifact_bytes, 2048 * 1024 * 1024);
  assert.ok(items.filter((p) => p.id !== 'rea-static').every((p) => !p.self_service && p.input === 'connection'));
  assert.ok(!items.some((p) => p.id === 'rea-binary'));
});

// 把 mock 包成 EV-31～EV-33 的客户端，让真实的上传状态机去跑
const uploadApi = (call, as) => {
  const unwrap = (r) => { if (r.status >= 400) throw Object.assign(new Error(r.body.code), { status: r.status, code: r.body.code, details: r.body.details }); return r.body; };
  return {
    startEvidenceUpload: async (b) => unwrap(await call('POST', '/api/v1/evidence/uploads', b, as)),
    putEvidenceUploadChunk: async (id, i, bytes) => unwrap(await call('PUT', `/api/v1/evidence/uploads/${id}/chunks/${i}`, bytes, as)),
    completeEvidenceUpload: async (id, b) => unwrap(await call('POST', `/api/v1/evidence/uploads/${id}/complete`, b, as)),
  };
};
const MiB = 1024 * 1024;
async function uploadVia(call, as, size = 2.5 * MiB, name = 'my-client.zip') {
  const file = { name, size, bytes: new Uint8Array(size).fill(7) };
  const up = createArtifactUpload({ api: uploadApi(call, as), file, read: async (f, s, e) => f.bytes.slice(s, e), sleep: async () => {} });
  await up.start();
  return up.state;
}

test('EV-31～EV-33：分块上传，块大小要对、缺块不能完成；上限与未完成数配额', async () => {
  const { call } = server({ role: 'user' });
  const me = { username: 'modeler01', role: 'user' };
  const st = await uploadVia(call, me);
  assert.equal(st.phase, 'done');
  assert.deepEqual([st.artifact.size, st.artifact.filename], [2.5 * MiB, 'my-client.zip']);
  assert.match(st.artifact.sha256, /^[0-9a-f]{64}$/);

  const begin = await call('POST', '/api/v1/evidence/uploads', { filename: 'x.zip', size: MiB + 10 }, me);
  assert.equal(begin.status, 201);
  assert.equal(begin.body.chunk_bytes, MiB);
  const id = begin.body.upload_id;
  assert.equal((await call('PUT', `/api/v1/evidence/uploads/${id}/chunks/1`, new Uint8Array(9), me)).body.code, 'invalid_argument');
  assert.equal((await call('PUT', `/api/v1/evidence/uploads/${id}/chunks/2`, new Uint8Array(10), me)).body.code, 'invalid_argument');
  assert.equal((await call('PUT', `/api/v1/evidence/uploads/${id}/chunks/1`, new Uint8Array(10), me)).status, 200);
  const inc = await call('POST', `/api/v1/evidence/uploads/${id}/complete`, { sha256: 'a'.repeat(64) }, me);
  assert.deepEqual([inc.status, inc.body.code, inc.body.details.missing], [409, 'upload_incomplete', [0]]);
  assert.equal((await call('PUT', `/api/v1/evidence/uploads/${id}/chunks/0`, new Uint8Array(10), { username: 'someone', role: 'user' })).body.code, 'upload_not_found');

  assert.equal((await call('POST', '/api/v1/evidence/uploads', { filename: 'y.zip', size: 10 }, me)).status, 201);
  assert.equal((await call('POST', '/api/v1/evidence/uploads', { filename: 'z.zip', size: 10 }, me)).body.code, 'quota_exceeded');
  const big = await call('POST', '/api/v1/evidence/uploads', { filename: 'huge.zip', size: 3 * 1024 * MiB }, { username: 'other', role: 'user' });
  assert.deepEqual([big.status, big.body.code, big.body.details.max_bytes], [413, 'upload_too_large', 2048 * MiB]);
});

test('自助登记（EV-02 / EV-04 / EV-05）：登录用户直接登记，只有本人和管理员看得到、改得了', async () => {
  const { call } = server({ role: 'user' });
  const me = { username: 'modeler01', role: 'user' };
  const other = { username: 'modeler02', role: 'user' };
  const admin = { username: 'zhangsan', role: 'admin' };
  const v1 = (await uploadVia(call, me)).artifact;
  const user = { username: 'modeler01', display_name: '建模者' };
  const form = { ...emptyForm(user), provider: 'rea-static', display_name: '我的客户端', id: 'my-client', note: '许可允许静态分析', artifact: v1 };
  const body = registerBody(form, REA_PROVIDER, { grantProjectId: F.MAIN_PID });
  assert.equal((await call('POST', '/api/v1/admin/evidence/sources', { ...body, connection: { artifact_sha256: 'f'.repeat(64) } }, me)).body.code, 'artifact_not_found');
  assert.equal((await call('POST', '/api/v1/admin/evidence/sources', { ...body, authorization_note: null }, me)).body.code, 'invalid_argument');
  const made = await call('POST', '/api/v1/admin/evidence/sources', body, me);
  assert.equal(made.status, 201);
  assert.deepEqual([made.body.owner, made.body.environment, made.body.authorized_for_project, made.body.cred_ref], ['modeler01', 'test', true, null]);
  assert.deepEqual(made.body.connection, { artifact_sha256: v1.sha256, artifact_filename: 'my-client.zip', artifact_size: 2.5 * MiB });
  assert.equal((await call('POST', '/api/v1/admin/evidence/sources', { ...body, id: 'db-x', provider: 'db-mysql' }, me)).status, 403);

  const ids = async (as) => (await call('GET', '/api/v1/evidence/sources', undefined, as)).body.items.map((s) => s.id);
  assert.deepEqual(await ids(me), ['erp-prod', 'crm-test', 'restock-svc', 'my-client']);
  assert.deepEqual(await ids(other), ['erp-prod', 'crm-test', 'restock-svc']);
  assert.deepEqual(await ids(admin), ['erp-prod', 'crm-test', 'restock-svc', 'sample-orders', 'my-client']);
  const mine = (await call('GET', '/api/v1/evidence/sources', undefined, me)).body.items.find((s) => s.id === 'my-client');
  assert.equal(mine.authorization_note, '许可允许静态分析');

  // 上传新版本 = 上传完后 PATCH connection
  const v2 = (await uploadVia(call, me, 3 * MiB, 'my-client-2.zip')).artifact;
  const patch = await call('PATCH', '/api/v1/admin/evidence/sources/my-client', artifactPatch({ ...form, artifact: v2 }), me);
  assert.equal(patch.status, 200);
  assert.equal(patch.body.connection.artifact_filename, 'my-client-2.zip');
  assert.equal(patch.body.owner, 'modeler01');
  assert.equal((await call('PATCH', '/api/v1/admin/evidence/sources/my-client', { display_name: 'x' }, other)).status, 403);
  assert.equal((await call('PATCH', '/api/v1/admin/evidence/sources/erp-prod', { display_name: 'x' }, me)).status, 403);
  assert.equal((await call('DELETE', '/api/v1/admin/evidence/sources/my-client', undefined, other)).status, 403);
  assert.equal((await call('DELETE', '/api/v1/admin/evidence/sources/my-client', undefined, me)).status, 204);
});

test('采集 REA 数据源：只能快照，不用确认，进度按步推进，生成的快照带 manifest 和不可信标记', async () => {
  const { call, tick, events } = server();
  assert.equal((await call('POST', `${P}/evidence/jobs/preview`, { source_id: 'sample-orders', operation: 'discover' })).body.code, 'invalid_argument');
  const pv = (await call('POST', `${P}/evidence/jobs/preview`, { source_id: 'sample-orders', operation: 'snapshot' })).body;
  assert.deepEqual([pv.requires_confirmation, pv.confirmation_token, pv.summary.scope_text], [false, null, 'sample-orders-1.4.2.zip']);
  const j = (await call('POST', `${P}/evidence/jobs`, { source_id: 'sample-orders', operation: 'snapshot' })).body;
  assert.deepEqual([j.state, j.phase, j.progress.unit, j.progress.total, j.consent, j.scope_text], ['running', '判定类型', '步', 4, null, 'sample-orders-1.4.2.zip']);
  while (tick());
  const phases = events.filter((e) => e.data.id === j.id).map((e) => e.data.phase);
  assert.deepEqual(phases, ['判定类型', '解析程序', '转换记录', '组装快照', null]);
  const last = events.filter((e) => e.data.id === j.id).at(-1).data;
  assert.equal(last.state, 'succeeded');
  const detail = (await call('GET', `${P}/evidence/snapshots/${last.result.snapshot_id}`)).body;
  assert.equal(detail.provider, 'rea-static@0.1.0');
  assert.equal(detail.previous, SNAP.rea);
  assert.equal(detail.manifest.untrusted_content, true);
  assert.deepEqual(detail.manifest.artifact, { sha256: REA_ARTIFACT.sha256, size: REA_ARTIFACT.size, filename: 'sample-orders-1.4.2.zip', detected: 'electron' });
  assert.ok(detail.manifest.limitations.length);
});

test('证据浏览器读 REA 快照：按模块分组、检索、记录详情带位置和成员、相邻记录、ER 为空', async () => {
  const { call } = server();
  const q = `snapshot=${SNAP.rea}`;
  const tree = (await call('GET', `${P}/evidence/summary-tree?${q}`)).body;
  assert.deepEqual(tree.domains.map((d) => d.name), ['main.js', 'preload.js', 'orders.js', 'customers.js', 'erp-client.js']);
  const orders = tree.domains.find((d) => d.name === 'orders.js').entities;
  assert.deepEqual(orders.map((e) => [e.name, e.type]), [['Order', 'entity'], ['Order.create', 'operation'], ['Order.cancel', 'operation']]);
  assert.equal(orders[0].anchor, 'bin:sample-orders/sym:orders%2Ejs::Order');
  const hits = (await call('GET', `${P}/evidence/search?${q}&q=order&type=operation`)).body;
  assert.ok(hits.items.some((h) => h.anchor === 'bin:sample-orders/ipc:order:create'));
  assert.ok(hits.items.every((h) => h.type === 'operation'));
  const rec = (await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent('bin:sample-orders/sym:orders%2Ejs::Order')}&${q}`)).body;
  assert.deepEqual(rec.attrs.loc, { file: 'orders.js', line: 8 });
  assert.equal(rec.untrusted_content, true);
  assert.deepEqual(rec.fields.map((f) => f.name), ['orderNo', 'qty', 'Order.create', 'Order.cancel']);
  const lin = (await call('GET', `${P}/evidence/lineage?anchor=${encodeURIComponent('bin:sample-orders/ipc:order:create')}&${q}`)).body.items;
  assert.deepEqual(lin.map((x) => [x.label, x.basis]), [['api.createOrder', '被调用'], ['Order.create', '由它处理']]);
  const ext = 'bin:sample-orders/ext:POST%20https:%2F%2Ferp%2Eexample%2Ecom%2Fapi%2Forders';
  assert.equal((await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent(ext)}&${q}`)).body.attrs.url, 'https://erp.example.com/api/orders');
  assert.deepEqual((await call('GET', `${P}/evidence/er?${q}`)).body, { tables: [], relations: [] });
  assert.equal((await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent('bin:sample-orders/sym:nope')}&${q}`)).body.code, 'anchor_not_found');
});

test('上传程序的提供方：三步校验、自助登记请求体、修改请求体，不带出口、窗口、限流、预算', () => {
  const user = { username: 'modeler01' };
  const blank = { ...emptyForm(user), provider: 'rea-static' };
  assert.deepEqual(wizardSteps(REA_PROVIDER), ['选择类型', '上传程序', '授权说明']);
  assert.equal(wizardSteps(MYSQL).length, 6);
  const v0 = validateForm(blank, { provider: REA_PROVIDER, admin: false });
  assert.deepEqual(Object.keys(v0.errors).sort(), ['artifact', 'display_name', 'id', 'note']);
  assert.deepEqual([v0.done, v0.current], [[true, false, false], 1]);
  const art = { sha256: 'a'.repeat(64), size: 48 * 1024 * 1024, filename: 'sample-orders.zip' };
  const form = { ...blank, display_name: '订货客户端', id: 'sample-orders', artifact: art, note: ' 许可允许静态分析 ' };
  const v = validateForm(form, { provider: REA_PROVIDER, admin: false });
  assert.deepEqual([v.errors, v.done, v.current], [{}, [true, true, true], -1]);
  assert.deepEqual(validateForm({ ...form, note: '' }, { provider: REA_PROVIDER }).done, [true, true, false]);

  const body = registerBody(form, REA_PROVIDER, { grantProjectId: 'p1' });
  assert.deepEqual(body, {
    id: 'sample-orders', display_name: '订货客户端', provider: 'rea-static', environment: 'test',
    connection: { artifact_sha256: 'a'.repeat(64), artifact_filename: 'sample-orders.zip', artifact_size: 48 * 1024 * 1024 },
    authorization_note: '许可允许静态分析', owner: 'modeler01', grant_project_id: 'p1',
  });
  const src = { provider: { id: 'rea-static' }, connection: body.connection, owner: 'modeler01', display_name: '订货客户端', id: 'sample-orders', authorization_note: 'x', environment: 'test' };
  assert.deepEqual(formFromSource(src).artifact, art);
  assert.deepEqual(Object.keys(artifactPatch(form)).sort(), ['authorization_note', 'connection', 'display_name']);
});

test('自助类数据源的展示：程序文件名和大小、范围为空、谁能管理、按提供方或连接参数判断', () => {
  const s = { provider: { id: 'rea-static' }, owner: 'modeler01', connection: { artifact_sha256: 'b'.repeat(64), artifact_filename: 'sample-orders.zip', artifact_size: 48 * 1024 * 1024 + 5 } };
  assert.equal(connText(s), 'sample-orders.zip · 48 MB');
  assert.equal(artifactText({ filename: 'x.exe', size: 3 * 1024 ** 3 }), 'x.exe · 3 GB');
  assert.equal(scopeWindowText(s), '—');
  assert.ok(isArtifactSource(s, [REA_PROVIDER]));
  assert.ok(isArtifactSource(s, []));
  assert.ok(!isArtifactSource({ provider: { id: 'db-mysql' }, connection: { host: 'h' } }, [MYSQL]));
  assert.ok(canManageSource(s, { username: 'modeler01' }, false));
  assert.ok(!canManageSource(s, { username: 'modeler02' }, false));
  assert.ok(canManageSource(s, { username: 'zhangsan' }, true));
});

test('发起采集只列提供方支持的操作；程序逆向没有增量', () => {
  assert.deepEqual(collectOps(REA_PROVIDER).map(([k]) => k), ['snapshot']);
  assert.deepEqual(collectOps({ capabilities: ['discover', 'snapshot', 'sample', 'incremental'] }).map(([k]) => k), ['discover', 'snapshot', 'sample']);
  assert.equal(collectOps(null).length, 3);
  assert.equal(canIncremental(REA_PROVIDER), false);
  assert.equal(canIncremental(MYSQL), true);
});

test('快照详情：程序逆向的 manifest 展开成程序、类型、哈希、工具，局限原样加未识别数；数据库快照照旧', () => {
  const rea = {
    provider: 'rea-static@0.1.0', coverage_pct: 100,
    manifest: { tool: { name: 'rea', version: '6.8.0' }, masking_policy: 'none', untrusted_content: true, limitations: ['动态通道没有展开'], unknowns: 7, artifact: { sha256: 'c'.repeat(64), size: 2 * 1024 * 1024, filename: 'a.zip', detected: 'electron' } },
    inventory: { skipped: [{ object: 'node_modules/', reason: '第三方依赖' }] },
  };
  assert.deepEqual(snapshotPairs(rea), [
    ['程序', 'a.zip · 2 MB'], ['类型', 'Electron'], ['SHA-256', `${'c'.repeat(12)}…`], ['工具', 'rea 6.8.0'], ['覆盖率', '100%'], ['跳过', 'node_modules/（第三方依赖）'],
  ]);
  assert.deepEqual(snapshotLimits(rea), ['动态通道没有展开', '未识别 7 处']);
  const db = { provider: 'db-mysql@1.0.0', coverage_pct: 79, manifest: { provider: 'db-mysql@1.0.0', masking_policy: 'mask-v3', scope: ['erp.*'] }, inventory: { skipped: [] } };
  assert.deepEqual(snapshotPairs(db), [['连接器', 'db-mysql@1.0.0'], ['范围', 'erp.*'], ['覆盖率', '79%'], ['脱敏', 'mask-v3']]);
  assert.deepEqual(snapshotLimits(db), []);
});

test('证据浏览器：快照类别、能打开的锚点、记录图标与键值、默认选中', () => {
  assert.equal(snapshotKind({ provider: 'rea-static@0.1.0' }, [REA_PROVIDER]), 'rea');
  assert.equal(snapshotKind({ provider: 'db-mysql@1.0.0' }, []), 'db');
  assert.equal(snapshotKind({ provider: 'rea-static@0.1.0' }, []), 'rea');
  assert.ok(isDbKind('db') && !isDbKind('rea'));
  assert.deepEqual(browseTarget('db:erp-prod/erp.replenish_std.max_qty'), { source: 'erp-prod', table: 'db:erp-prod/erp.replenish_std', field: 'max_qty' });
  const cls = 'bin:sample-orders/sym:orders%2Ejs::Order';
  assert.deepEqual(browseTarget(cls), { source: 'sample-orders', table: cls, field: null });
  assert.equal(browseTarget('file:resource/text/a.md#L3'), null);
  assert.equal(binPathType('bin:sample-orders/ipc:order:create'), 'ipc');
  assert.equal(recordIcon({ anchor: cls, type: 'entity' }), 'cube');
  assert.equal(recordIcon({ anchor: `${cls}::create`, type: 'operation' }), 'code');
  assert.equal(recordIcon({ anchor: 'bin:x/ext:GET%20https:%2F%2Ferp%2Eexample%2Ecom', type: 'entity' }), 'cloud');
  assert.equal(recordIcon({ anchor: 'bin:x/ipc:a', type: 'operation' }), 'send');
  const rec = { type: 'operation', attrs: { kind: 'method', module: 'orders.js', loc: { file: 'orders.js', line: 21 }, signature: 'create(draft)' }, evidence: { method: 'rea analyze-javascript-application' } };
  assert.equal(recordKindText(rec), '方法');
  assert.deepEqual(recordPairs(rec), [['位置', 'orders.js:21'], ['签名', 'create(draft)'], ['来源', 'rea analyze-javascript-application']]);
  assert.deepEqual(recordPairs({ attrs: { kind: 'endpoint', module: 'erp-client.js', method: 'POST', url: 'https://erp.example.com/api/orders' } }), [['模块', 'erp-client.js'], ['地址', 'POST https://erp.example.com/api/orders']]);
  assert.equal(firstTreeAnchor({ domains: [{ entities: [] }, { entities: [{ anchor: 'a' }, { anchor: 'b' }] }] }), 'a');
  assert.equal(firstTreeAnchor(null), null);
});

// ---------- F14：数据库、代码仓库连接器对接真实接口的形状 ----------

const CODE_PROVIDER = {
  id: 'code-repo', kind: 'code', capabilities: ['snapshot'],
  cred_fields: [{ name: 'repo_url', label: '仓库地址' }, { name: 'ref', label: '分支或提交' }, { name: 'token', label: '访问令牌', secret: true }],
};

test('凭据只看写没写过：引用给不给都能判断，从不回显；输入框按提供方的密钥字段命名', () => {
  const at = '2026-10-28T10:20:00+08:00';
  const now = new Date(2026, 10, 1, 22, 41).getTime();
  assert.deepEqual(credState({ cred_ref: 'cred://erp-prod/3', secret_fields: [{ name: 'password', set: true, written_at: at }] }), { needed: true, set: true, ref: 'cred://erp-prod/3', at });
  // 普通用户看别人登记的：cred_ref 为空，secret_fields 照给
  assert.deepEqual(credState({ cred_ref: null, secret_fields: [{ name: 'password', set: true, written_at: at }] }), { needed: true, set: true, ref: null, at });
  assert.deepEqual(credState({ cred_ref: null, secret_fields: [{ name: 'token', set: false, written_at: null }] }), { needed: true, set: false, ref: null, at: null });
  assert.equal(credState({ secret_fields: [] }).needed, false);
  assert.equal(credText(credState({ secret_fields: [{ name: 'password', set: true, written_at: at }] }), now), `已设置 · ${fmtWhen(at, now)}`);
  assert.equal(credText({ set: true, at: null }), '已设置');
  assert.equal(secretInputLabel({ label: '访问令牌' }), '写入新访问令牌');
  assert.equal(secretInputLabel(null), '写入新密码');
  assert.equal(portDbPlaceholder({ id: 'db-postgres' }), '5432 / crm');
  assert.equal(portDbPlaceholder({ id: 'db-mysql' }), '3306 / erp');
  assert.equal(scopePlaceholder(CODE_PROVIDER), '如 src/main/java/*');
  assert.deepEqual(MASKING.map(([k]) => k), ['mask-v1']);
  assert.equal(emptyForm({}).masking, 'mask-v1');
});

test('代码仓库登记：只认 https，出口缺省是仓库主机的 443，范围可以不写，令牌走 secret', () => {
  assert.equal(repoHost('https://git.example.com/demo/restock-service.git'), 'git.example.com');
  assert.equal(repoHost('https://ci@Git.Example.com:8443/x.git'), 'git.example.com');
  assert.equal(repoHost('http://git.example.com/x.git'), null);
  assert.equal(repoHost('git@git.example.com:x.git'), null);
  assert.equal(defaultEgress(CODE_PROVIDER, { conn: { repo_url: 'https://git.example.com/demo/x.git' } }), 'git.example.com:443');
  assert.equal(defaultEgress(CODE_PROVIDER, { conn: { repo_url: 'git.example.com/x' } }), '');
  assert.equal(defaultEgress(MYSQL, { conn: { host: '192.0.2.30' }, port_db: '3306 / pos' }), '192.0.2.30:3306');
  const form = {
    ...emptyForm({ username: 'zhangsan' }), provider: 'code-repo', display_name: '补货单服务代码', id: 'restock-svc',
    conn: { repo_url: 'https://git.example.com/demo/restock-service.git', ref: 'main' }, scope: [], egress: 'git.example.com:443', password: 'tok',
  };
  const v = validateForm(form, { provider: CODE_PROVIDER, admin: true, tested: true });
  assert.deepEqual(v.errors, {});
  assert.deepEqual(v.done, [true, true, true, true, true, true]);
  assert.equal(validateForm({ ...form, conn: { ...form.conn, repo_url: 'http://git.example.com/x.git' } }, { provider: CODE_PROVIDER }).errors['conn.repo_url'], '只支持 https 地址');
  assert.ok(validateForm({ ...form, scope: [] }, { provider: MYSQL }).errors.scope);
  const body = registerBody(form, CODE_PROVIDER, { grantProjectId: 'p1' });
  assert.deepEqual(body.connection, { repo_url: 'https://git.example.com/demo/restock-service.git', ref: 'main' });
  assert.deepEqual(body.secret, { token: 'tok' });
  assert.deepEqual(body.egress, [{ host: 'git.example.com', port: 443 }]);
  assert.deepEqual(body.scope_allowlist, []);
  assert.equal(body.masking_policy, 'mask-v1');
});

test('采集确认：内核给的窗口是对象，样本行数为 0 时不写；代码仓库快照详情展开成仓库和提交', () => {
  const now = new Date(2026, 10, 1, 14, 0).getTime();
  const pairs = previewPairs({
    scope_text: 'erp.goods_category、erp.replenish_std', window: { days: 'daily', start: '22:00', end: '06:00', tz: 'Asia/Shanghai' }, in_window_now: false,
    rate_limit: { concurrency: 2, qps: 20, query_timeout_s: 30 }, estimate: null, readonly_methods: ['只读账号 · 只读会话 · 语句白名单'], masking: { policy: 'mask-v1', sample_rows: 0 },
  }, now);
  assert.equal(pairs[1][1], '22:00 – 06:00（现在 14:00，不在窗口内，同意后排队到 22:00 开始）');
  assert.equal(pairs[4][1], '只读账号 · 只读会话 · 语句白名单');
  assert.equal(pairs[5][1], 'mask-v1');
  assert.equal(previewPairs({ window: null, masking: { policy: 'none', sample_rows: 0 } }, now)[5][1], '—');
  const detail = { provider: 'code-repo@0.1.0', coverage_pct: 100, manifest: { artifact: { ...CODE_REPO }, scope: ['src/main/java/*'], masking_policy: 'none' }, inventory: { skipped: [] } };
  assert.deepEqual(snapshotPairs(detail), [
    ['仓库', CODE_REPO.repo_url], ['提交', `main · ${CODE_REPO.commit.slice(0, 12)}`], ['连接器', 'code-repo@0.1.0'], ['范围', 'src/main/java/*'], ['覆盖率', '100%'],
  ]);
});

test('数据库记录详情：主键外键从 ER 补、被遮蔽的列、画像按列、图例只列出现的关系、ER 连线落在列对上', () => {
  const fields = [{ name: 'id', type: 'bigint', masked: false }, { name: 'store_code', type: 'varchar(16)' }, { name: 'phone', type: 'varchar(20)', masked: true }];
  const er = { fields: [{ name: 'id', pk: true, fk: false }, { name: 'store_code', pk: false, fk: true }, { name: 'phone', masked: true }] };
  assert.deepEqual(withKeys(fields, er).map((f) => [f.name, f.pk, f.fk, f.masked]), [['id', true, false, false], ['store_code', false, true, false], ['phone', false, false, true]]);
  // EV-25 自己带了就用自己的
  assert.equal(withKeys([{ name: 'id', pk: false }], er)[0].pk, false);
  assert.deepEqual(withKeys(fields, null).map((f) => f.pk), [false, false, false]);
  assert.deepEqual([...maskedColumns({ fields: withKeys(fields), samples: { masked_columns: ['contact_name'] } })].sort(), ['contact_name', 'phone']);
  assert.deepEqual(profileColumns([{ column: 'a', top_k: [{ value: 'x', pct: 50 }] }, { column: 'b' }, { column: 'c', stats: { min: 1, max: 2, null_pct: 0 } }]).map((p) => p.column), ['a', 'c']);
  assert.deepEqual(erLegend([{ confidence: 'declared' }, { confidence: 'inferred' }, { confidence: 'declared' }]), [['declared', '外键'], ['inferred', '推断']]);
  assert.equal(dbRecordIcon('db:erp-prod/erp.view:v_orders'), 'layers');
  assert.equal(dbRecordIcon('db:erp-prod/erp.proc:calc_quota'), 'zap');
  assert.equal(dbRecordIcon('db:erp-prod/erp.store'), 'table');
  // 外键的 from / to 是表锚点，列对在 columns：连线落在 c.x 和 a.id 那两行，和用列锚点时一样
  const byTable = { ...ER, relations: ER.relations.map((r) => ({ ...r, from_anchor: parseDbAnchor(r.from_anchor).tableAnchor, to_anchor: parseDbAnchor(r.to_anchor).tableAnchor, columns: [[parseDbAnchor(r.from_anchor).field, parseDbAnchor(r.to_anchor).field]] })) };
  const a = layoutEr(ER, 'db:s/erp.c', { domainOf: (x) => DOM[x] });
  const b = layoutEr(byTable, 'db:s/erp.c', { domainOf: (x) => DOM[x] });
  assert.deepEqual(b.edges.map((e) => e.d), a.edges.map((e) => e.d));
  // 没有列对就落在表头
  const head = layoutEr({ ...byTable, relations: byTable.relations.map(({ columns, ...r }) => r) }, 'db:s/erp.c', { domainOf: (x) => DOM[x] });
  assert.match(head.edges[0].d, new RegExp(`^M245 ${head.boxes['db:s/erp.c'].y + 12} `));
});

test('代码记录：锚点能打开、图标、键值、片段行号、校验、成员里的规则单列、来源标签', () => {
  const recs = codeRecords();
  const byName = (n) => recs.find((r) => r.name === n);
  const order = byName('Order');
  assert.equal(order.id, 'code:restock-svc/sym:com::example::restock::order::Order');
  assert.deepEqual(browseTarget(order.id), { source: 'restock-svc', table: order.id, field: null });
  assert.deepEqual(browseTarget('code:restock-svc/file:src%2Fa%2Ejava#L3-L9'), { source: 'restock-svc', table: 'code:restock-svc/file:src%2Fa%2Ejava#L3-L9', field: null });
  const rule = recs.find((r) => r.type === 'rule' && r.name === 'Max');
  assert.equal(rule.id, 'code:restock-svc/rule:com::example::restock::order::OrderItem::requestQty::Max');
  assert.equal(recordIcon({ anchor: rule.id, type: 'rule' }), 'scale');
  const route = recs.find((r) => r.attrs.kind === 'route' && r.attrs.path === '/api/orders');
  assert.equal(route.id, 'code:restock-svc/route:POST%20%2Fapi%2Forders');
  assert.equal(route.attrs.handler, byName('OrderController.create').id);
  assert.equal(recordIcon({ anchor: route.id, type: route.type }), 'branch');
  assert.deepEqual(recordPairs(route), [['位置', 'src/main/java/com/example/restock/order/web/OrderController.java:31'], ['模块', 'com.example.restock.order.web'], ['路由', 'POST /api/orders'], ['来源', 'javalang']]);
  assert.deepEqual(recordPairs(byName('requestQty')).filter(([k]) => ['类型', '列'].includes(k)), [['类型', 'Integer'], ['列', 'request_qty']]);
  assert.deepEqual(recordPairs(order).find(([k]) => k === '表'), ['表', 'replenish_order']);
  assert.deepEqual(recordPairs(byName('OrderRepository.findPendingByStore')).find(([k]) => k === '涉及表'), ['涉及表', 'replenish_order']);
  assert.deepEqual(recordPairs(byName('OrderStatus')).find(([k]) => k === '取值'), ['取值', 'DRAFT、SUBMITTED、APPROVED、REJECTED']);
  assert.equal(recordKindText(byName('OrderStatus')), '枚举');
  const lines = snippetLines(byName('OrderService.submit'));
  assert.deepEqual(lines[0], { n: 27, text: '@Transactional' });
  assert.equal(lines.at(-1).n, 27 + lines.length - 1);
  assert.equal(snippetLines({ attrs: { snippet: Array.from({ length: 40 }, (_, i) => `l${i}`).join('\n') } }).length, 30);
  assert.equal(snippetLines({ attrs: {} }), null);
  assert.deepEqual(recordValidations(byName('requestQty')), ['@Min(1)', '@Max(120)']);
  assert.equal(validationText({ name: '@Size', args: { max: 20 } }), '@Size(max=20)');
  assert.equal(validationText({ annotation: '@NotNull' }), '@NotNull');
  const split = splitMembers([{ anchor: byName('orderNo').id }, { anchor: rule.id }]);
  assert.deepEqual([split.members.length, split.rules.length], [1, 1]);
  assert.equal(untrustedText('code'), '来自代码仓库');
  assert.equal(untrustedText('rea'), '来自目标程序');
  assert.equal(snapshotKind({ provider: 'code-repo@0.1.0' }, []), 'code');
});

test('mock 对齐真实形状：提供方清单同网关，测试连接按类型回 checks，访问审计只记语句摘要', async () => {
  const { call } = server();
  const items = (await call('GET', '/api/v1/evidence/providers')).body.items;
  assert.deepEqual(items.map((p) => [p.id, p.version, p.kind]), [['db-mysql', '0.1.0', 'db'], ['db-postgres', '0.1.0', 'db'], ['code-repo', '0.1.0', 'code'], ['rea-static', '0.1.0', 'rea']]);
  assert.deepEqual(items.find((p) => p.id === 'db-mysql').capabilities, ['discover', 'snapshot', 'sample']);
  assert.deepEqual(items.find((p) => p.id === 'code-repo').cred_fields.filter((f) => f.secret).map((f) => f.name), ['token']);
  const db = (await call('POST', '/api/v1/evidence/sources/erp-prod/test', { project_id: F.MAIN_PID })).body;
  assert.deepEqual(db.checks.map((c) => [c.name, c.ok]), [['connectivity', true], ['readonly_account', true], ['statement_allowlist', true]]);
  assert.ok(db.ticket.id_prefix);
  const code = (await call('POST', `/api/v1/evidence/sources/${CODE.id}/test`, { project_id: F.MAIN_PID })).body;
  assert.deepEqual(code.checks.map((c) => [c.name, c.ok, c.detail]), [['connectivity', true, 'git.example.com:443 · main']]);
  const rea = (await call('POST', '/api/v1/evidence/sources/sample-orders/test', { project_id: F.MAIN_PID })).body;
  assert.equal(rea.ticket, null);
  await call('DELETE', `/api/v1/admin/evidence/sources/${CODE.id}/secret`);
  const off = (await call('POST', `/api/v1/evidence/sources/${CODE.id}/test`, { project_id: F.MAIN_PID })).body;
  assert.equal(off.ok, false);
  const src = (await call('GET', `/api/v1/evidence/sources?project_id=${F.MAIN_PID}`)).body.items.find((s) => s.id === CODE.id);
  assert.deepEqual([src.cred_ref, src.secret_fields], [null, [{ name: 'token', set: false, written_at: null }]]);
  const rot = (await call('PUT', `/api/v1/admin/evidence/sources/${CODE.id}/secret`, { fields: { token: 'new' } })).body;
  assert.equal(rot.cred_ref, 'cred://restock-svc/1');
  const log = (await call('GET', `/api/v1/evidence/sources/erp-prod/access-log?project_id=${F.MAIN_PID}`)).body;
  assert.ok(log.items.every((x) => /^(select|show):[0-9a-f]{16}$/.test(x.statement_digest)));
  assert.ok(log.items.every((x) => x.job_id));
  const pv = (await call('POST', `${P}/evidence/jobs/preview`, { source_id: 'erp-prod', operation: 'discover' })).body;
  assert.deepEqual(pv.summary.window, { days: 'daily', start: '22:00', end: '06:00', tz: 'Asia/Shanghai' });
  assert.deepEqual(pv.summary.readonly_methods, ['只读账号 · 只读会话 · 语句白名单']);
  assert.equal((await call('POST', `${P}/evidence/jobs/preview`, { source_id: CODE.id, operation: 'discover' })).body.code, 'invalid_argument');
});

test('证据浏览器读代码仓库快照：按包分组、记录详情带片段和成员、字段的成员是校验规则、路由指向处理方法、ER 为空；库表的相邻记录有代码调用方', async () => {
  const { call, tick, events } = server();
  const q = `snapshot=${SNAP.code}`;
  const tree = (await call('GET', `${P}/evidence/summary-tree?${q}`)).body;
  assert.equal(tree.untrusted_content, true);
  assert.deepEqual(tree.domains.map((d) => d.name), ['com.example.restock.order', 'com.example.restock.order.web', 'com.example.restock.order.service', 'com.example.restock.order.repo']);
  const order = tree.domains[0].entities.find((e) => e.name === 'Order');
  assert.equal(order.mapped_to.key, 'replenish_order');
  const rec = (await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent(order.anchor)}&${q}`)).body;
  assert.equal(rec.attrs.snippet.split('\n')[0], '@Entity');
  assert.deepEqual(rec.attrs.loc, { file: 'src/main/java/com/example/restock/order/Order.java', line: 12, end_line: 40 });
  assert.deepEqual(rec.fields.map((f) => [f.name, f.type]), [['orderNo', 'String'], ['storeCode', 'String'], ['categoryCode', 'String'], ['status', 'OrderStatus']]);
  const field = (await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent(rec.fields[0].anchor)}&${q}`)).body;
  assert.deepEqual(field.attrs.validations, ['@NotNull', '@Size(max = 20)']);
  assert.deepEqual(splitMembers(field.fields).rules.map((f) => [f.name, f.type]), [['NotNull', '@NotNull'], ['Size', '@Size']]);
  const hits = (await call('GET', `${P}/evidence/search?${q}&q=orders&type=operation`)).body;
  const route = hits.items.find((h) => h.anchor === 'code:restock-svc/route:POST%20%2Fapi%2Forders');
  const routeRec = (await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent(route.anchor)}&${q}`)).body;
  const handler = (await call('GET', `${P}/evidence/record?anchor=${encodeURIComponent(routeRec.attrs.handler)}&${q}`)).body;
  assert.equal(handler.name, 'OrderController.create');
  const lin = (await call('GET', `${P}/evidence/lineage?anchor=${encodeURIComponent(handler.anchor)}&${q}`)).body.items;
  assert.deepEqual(lin.map((x) => [x.label, x.basis, x.confidence]), [['OrderService.submit', '调用', 'inferred']]);
  assert.deepEqual((await call('GET', `${P}/evidence/er?${q}`)).body, { tables: [], relations: [] });
  const dbLin = (await call('GET', `${P}/evidence/lineage?anchor=${encodeURIComponent('db:erp-prod/erp.replenish_order')}&snapshot=${SNAP.s9c}`)).body.items;
  const callers = dbLin.filter((x) => x.anchor.startsWith('code:'));
  assert.deepEqual(callers.map((x) => x.basis), ['代码实体', '代码调用方']);
  assert.ok(callers.every((x) => browseTarget(x.anchor)?.source === CODE.id));
  const er = (await call('GET', `${P}/evidence/er?snapshot=${SNAP.s9c}&focus=db%3Aerp-prod%2Ferp.replenish_std`)).body;
  assert.ok(er.relations.every((r) => !parseDbAnchor(r.from_anchor).field && r.columns[0].length === 2));
  // 再采一次：代码仓库只能快照，测试环境不用确认，按步推进
  const j = (await call('POST', `${P}/evidence/jobs`, { source_id: CODE.id, operation: 'snapshot' })).body;
  assert.deepEqual([j.state, j.phase, j.progress.unit, j.scope_text], ['running', '浅克隆', '步', 'src/main/java/*']);
  while (tick());
  const last = events.filter((e) => e.data.id === j.id).at(-1).data;
  const detail = (await call('GET', `${P}/evidence/snapshots/${last.result.snapshot_id}`)).body;
  assert.deepEqual([detail.provider, detail.previous, detail.manifest.artifact.commit], ['code-repo@0.1.0', SNAP.code, CODE_REPO.commit]);
});
