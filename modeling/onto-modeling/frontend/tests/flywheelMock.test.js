import assert from 'node:assert/strict';
import test from 'node:test';
import { register } from '../src/api/mock/routes-flywheel.js';

const PID = 'retail-restock-3f9a1c2d';

function server(sc = {}) {
  const routes = [];
  let t = 1_900_000_000;
  register({
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(body == null ? null : JSON.stringify(body), { status }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {}, sc, cardOf: () => ({ name: '门店补货单编制' }), clock: () => t,
  });
  const call = async (method, path, body, headers = {}) => {
    const u = new URL(`http://localhost/api/v1/projects/${PID}/flywheel${path}`);
    const r = routes.find((x) => x.method === method && x.re.test(u.pathname));
    assert.ok(r, `没有路由 ${method} ${path}`);
    const res = await r.handler({
      m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, init: { headers }, user: { username: 'zhangsan' },
    });
    return { status: res.status, body: await res.json() };
  };
  return { call, tick: (seconds) => { t += seconds; } };
}
const runOf = (view, id) => view.runs.find((run) => run.id === id);

test('mock 飞轮：初始一条待确认优化，记录按 20 条分页，筛选与后端分组一致', async () => {
  const { call } = server();
  const { body: view } = await call('GET', '');
  assert.equal(view.available, true);
  assert.equal(view.package.version, '0.11.0');
  assert.equal(view.pending, 1);
  assert.equal(view.total, 24);
  assert.equal(view.runs[0].id, 'FW-024');
  assert.equal(view.runs[0].status, 'waiting_optimize');
  assert.equal(runOf(view, 'FW-023').published_release_id, view.package.release_id);
  assert.ok(view.runs.every((run) => !Object.keys(run).some((key) => key.startsWith('_'))));

  const first = await call('GET', '/runs');
  assert.deepEqual([first.body.items.length, first.body.next_offset, first.body.total], [20, 20, 24]);
  const rest = await call('GET', '/runs?offset=20');
  assert.deepEqual([rest.body.items.length, rest.body.next_offset], [4, null]);
  assert.equal((await call('GET', '/runs?limit=99')).body.items.length, 24);
  assert.equal((await call('GET', '/runs?filter=waiting')).body.total, 1);
  const blocked = await call('GET', '/runs?filter=blocked');
  assert.ok(blocked.body.items.some((run) => run.status === 'interrupted'));
  assert.equal((await call('GET', '/runs?filter=nope')).body.code, 'flywheel_invalid');
  assert.equal((await call('GET', '/audit')).body.items[0].action, '自动化确认发布与发起评测');
});

test('mock 飞轮：人工逐步确认，优化 → 待发布 → 发布 → 待评测 → 评测通过', async () => {
  const { call, tick } = server();
  const started = await call('POST', '/runs/FW-024/optimize');
  assert.equal(started.status, 200);
  assert.equal(started.body.active_id, 'FW-024');
  assert.equal(runOf(started.body, 'FW-024').status, 'optimizing');
  const again = await call('POST', '/runs/FW-024/optimize');
  assert.deepEqual([again.status, again.body.code], [409, 'flywheel_blocked']);

  tick(7);
  let view = (await call('GET', '')).body;
  assert.equal(runOf(view, 'FW-024').status, 'waiting_publish');
  assert.equal(view.candidate_id, 'FW-024');
  assert.equal((await call('GET', '/runs/FW-024/changes')).body.files.length, 3);

  await call('POST', '/runs/FW-024/publish');
  tick(5);
  view = (await call('GET', '')).body;
  assert.equal(runOf(view, 'FW-024').status, 'waiting_evaluation');
  assert.equal(runOf(view, 'FW-024').target_version, '0.11.1');
  assert.equal(view.package.version, '0.11.1');

  await call('POST', '/runs/FW-024/evaluate', { replace_graph: false, graph_confirmation_code: '' });
  tick(9);
  const done = runOf((await call('GET', '')).body, 'FW-024');
  assert.equal(done.status, 'published');
  assert.equal(done.evaluation.passed, true);
  assert.equal(done.after_score, 100);
  const detail = (await call('GET', '/runs/FW-024')).body;
  assert.ok(detail.history.length >= 5);
  assert.equal(new Set(detail.history.map((item) => item.title)).size, detail.history.length);
  assert.equal(detail.checks.items.length, 3);
});

test('mock 飞轮：开启自动发布并转交 refresh token 后，一次读取追上整轮', async () => {
  const { call, tick } = server();
  const saved = await call('PUT', '/settings', { publish: true }, { 'X-Refresh-Token': 'synthetic-refresh' });
  assert.equal(saved.body.delegation.login, 'active');
  assert.equal(saved.body.audit[0].description, '已开启');
  const missing = await server().call('PUT', '/settings', { publish: true });
  assert.equal(missing.body.delegation.login, 'missing');

  await call('POST', '/runs/FW-024/optimize');
  tick(19);
  const run = runOf((await call('GET', '')).body, 'FW-024');
  assert.equal(run.status, 'published');
  assert.equal(run.evaluation.mode, 'auto');
  assert.equal(run.evaluation.passed, true);
});

test('mock 飞轮：回退校验范围；仅恢复建模内容时新增回退记录并暂停开关', async () => {
  const { call } = server();
  await call('PUT', '/settings', { optimize: true });
  const bad = await call('POST', '/runs/FW-023/rollback', { scope: 'all' });
  assert.deepEqual([bad.status, bad.body.code], [422, 'flywheel_invalid']);
  const noSnapshot = await call('POST', '/runs/FW-024/rollback', { scope: 'workspace' });
  assert.equal(noSnapshot.status, 409);

  const { body: view } = await call('POST', '/runs/FW-023/rollback', { scope: 'workspace' });
  assert.equal(view.runs[0].kind, 'rollback');
  assert.equal(view.runs[0].status, 'restored');
  assert.equal(view.runs[0].source_id, 'FW-023');
  assert.equal(runOf(view, 'FW-023').status, 'rolledback');
  assert.equal(view.settings.optimize, false);
  assert.equal(view.audit[0].description, '已关闭');
  assert.equal(view.total, 25);
  const swapped = (await call('GET', `/runs/${view.runs[0].id}/changes`)).body.files[0];
  assert.equal(swapped.lines.find((line) => line.text.startsWith('| 审核人')).kind, 'removed');
});

test('mock 飞轮：设置校验、未接统一账号、只读角色与不存在的运行', async () => {
  const { call } = server();
  for (const body of [{ max_rounds: 2 }, { delegation_days: 2 }, { optimize: 'yes' }]) {
    const res = await call('PUT', '/settings', body);
    assert.deepEqual([res.status, res.body.code], [422, 'flywheel_invalid']);
  }
  assert.equal((await call('GET', '/runs/FW-999')).status, 404);
  assert.equal((await call('POST', '/runs/FW-024/explode')).status, 404);
  assert.deepEqual((await call('GET', '/runs/FW-024/changes')).body, { total: 0, truncated: false, files: [] });

  const off = server({ flywheel: 'off' });
  const { body: unavailable } = await off.call('GET', '');
  assert.equal(unavailable.available, false);
  assert.match(unavailable.message, /统一账号/);
  assert.equal((await off.call('POST', '/runs/FW-024/ignore')).body.code, 'flywheel_unavailable');

  const readonly = server({ flywheel: 'readonly' });
  assert.equal((await readonly.call('GET', '')).body.can_write, false);
  assert.equal((await readonly.call('PUT', '/settings', { optimize: true })).status, 403);
  assert.equal((await readonly.call('POST', '/runs/FW-024/ignore')).status, 403);
});
