// 用户、审计与回收站（ops-users）：页面纯逻辑（usersModel）与 mock（AD-01～AD-07、O-48～O-54）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register } from '../src/api/mock/routes-admin.js';
import {
  AUDIT_FILTERS, actorLabel, auditContent, auditQuery, clockText, dateTimeText, daysLeft, defaultTransferTarget, deleteDoneText,
  deleteRequest, deleteSub, errorProject, errorSub, execText, isTrashUnsupported, lastSeenText, mergePage, ownerRequest, packNode,
  pagerText, passwordProblems, purgeText, resetRequest, restoreDoneText, roleMenu, ruleTexts, traceTarget, transferCandidates,
  trashOption, trashView, userStats,
} from '../src/pages/ops/usersModel.js';

const NOW = new Date(2026, 10, 1, 15, 50, 0).getTime();
const at = (d, h, m, s = 0) => new Date(2026, 10, d, h, m, s).toISOString();

// ---------- 纯逻辑 ----------

test('最近活跃与时间列：刚刚、分钟前、当天时分、昨天、更早只写月日；没有记录给「—」', () => {
  assert.equal(lastSeenText(at(1, 15, 49, 30), NOW), '刚刚');
  assert.equal(lastSeenText(at(1, 15, 38), NOW), '12 分钟前');
  assert.equal(lastSeenText(at(1, 13, 20), NOW), '13:20');
  assert.equal(lastSeenText(at(1, 0, 0), NOW), '00:00');
  assert.equal(lastSeenText(new Date(2026, 9, 31, 18, 40).toISOString(), NOW), '昨天 18:40');
  assert.equal(lastSeenText(new Date(2026, 9, 28, 9, 0).toISOString(), NOW), '10-28');
  assert.equal(lastSeenText(null, NOW), '—');
  assert.equal(clockText(at(1, 15, 44, 2), NOW), '15:44:02');
  assert.equal(clockText(new Date(2026, 9, 31, 9, 5, 7).toISOString(), NOW), '10-31 09:05');
  assert.equal(dateTimeText(new Date(2026, 9, 28, 17, 20).toISOString()), '10-28 17:20');
});

test('改角色菜单：三种角色，当前角色标「当前」', () => {
  const m = roleMenu('user');
  assert.deepEqual(m.map((x) => x.label), ['建模者', '管理员', '运维']);
  assert.deepEqual(m.map((x) => x.hint), ['当前', '', '']);
  assert.equal(roleMenu('ops')[2].hint, '当前');
});

test('统计卡：B2 第 13 条 new_7d、locked 为 null 时显示「—」并标出读不到；有值时不出复述标签的说明', () => {
  const full = userStats({ accounts: 23, by_role: { user: 19, admin: 2, ops: 2 }, new_7d: 2, locked: 1, registration_enabled: false, auth: { alg: 'RS256', issuer_checked: true } });
  assert.deepEqual(full.map((s) => s.value), [23, 2, 1, 'RS256']);
  assert.equal(full[0].desc, '建模者 19 · 管理员 2 · 运维 2');
  assert.equal(full[1].desc, '注册开关：关闭');
  assert.equal(full[2].desc, null);
  assert.equal(full[3].desc, 'JWKS 验签');
  const b2 = userStats({ accounts: 5, by_role: { user: 4, admin: 1 }, new_7d: null, locked: null, registration_enabled: null, auth: { alg: 'RS256', issuer_checked: true } });
  assert.equal(b2[1].value, '—');
  assert.equal(b2[2].value, '—');
  assert.equal(b2[1].desc, '注册开关：读不到');
  assert.equal(b2[2].desc, '认证服务未提供');
  assert.equal(b2[0].desc, '建模者 4 · 管理员 1 · 运维 0');
  // 没有 stats 时用 total 兜底
  assert.equal(userStats(null, 7)[0].value, 7);
});

test('重置密码：规则同 A-03（至少 8 位、三类字符、不含用户名、不是常见密码、首尾无空格）', () => {
  assert.deepEqual(passwordProblems('wanggong', ''), []);
  assert.deepEqual(passwordProblems('wanggong', 'abc'), ['len', 'classes']);
  assert.deepEqual(passwordProblems('wanggong', 'Wanggong#2026'), ['contains_username']);
  assert.deepEqual(passwordProblems('wanggong', 'Passw0rd!'), ['common']);
  assert.deepEqual(passwordProblems('wanggong', ' Spring#2026'), ['edge_space']);
  assert.deepEqual(passwordProblems('wanggong', 'Spring#2026'), []);
  assert.deepEqual(ruleTexts(['len', 'classes']), ['至少 8 位', '大写、小写、数字、符号中至少三类']);
  assert.deepEqual(ruleTexts(['same_as_old']), ['不能与旧密码相同']);

  assert.equal(resetRequest({ username: 'wanggong', password: '' }).ok, false);
  const weak = resetRequest({ username: 'wanggong', password: 'abc' });
  assert.equal(weak.ok, false);
  assert.match(weak.error, /^不满足：至少 8 位、/);
  // B2 第 14 条：require_change 不支持，总是 false
  assert.deepEqual(resetRequest({ username: 'wanggong', password: 'Spring#2026' }).body, { password: 'Spring#2026', require_change: false });
  assert.deepEqual(resetRequest({ username: 'wanggong', generate: true }).body, { generate: true, require_change: false });
});

const USERS = [
  { username: 'zhangsan', display_name: '张工', role: 'admin', online: true },
  { username: 'wanggong', display_name: '王工', role: 'user', online: true },
  { username: 'liming', display_name: '李明', role: 'user', online: false },
  { username: 'ops01', display_name: '周运维', role: 'ops', online: true },
  { username: 'sunyu', display_name: '孙宇', role: 'user', online: true },
];

test('删除用户：只能移交给另一位建模者；名下没有项目不用选去向', () => {
  assert.deepEqual(transferCandidates(USERS, 'wanggong').map((u) => u.username), ['liming', 'sunyu']);
  assert.equal(defaultTransferTarget(USERS, 'wanggong'), 'sunyu'); // 优先在线的
  assert.equal(defaultTransferTarget(USERS, 'sunyu'), 'wanggong');
  assert.equal(defaultTransferTarget([USERS[0]], 'zhangsan'), '');

  assert.deepEqual(deleteRequest({ projectCount: 0, users: USERS, username: 'liming' }), { ok: true, body: {} });
  assert.deepEqual(deleteRequest({ projectCount: 2, mode: 'transfer', target: 'sunyu', users: USERS, username: 'wanggong' }).body, { projects: 'transfer', transfer_to: 'sunyu' });
  assert.equal(deleteRequest({ projectCount: 2, mode: 'transfer', target: '', users: USERS, username: 'wanggong' }).error, '请选择接手这些项目的建模者');
  assert.equal(deleteRequest({ projectCount: 2, mode: 'transfer', target: 'wanggong', users: USERS, username: 'wanggong' }).error, '不能移交给被删除的用户自己');
  assert.equal(deleteRequest({ projectCount: 2, mode: 'transfer', target: 'ops01', users: USERS, username: 'wanggong' }).error, '移交对象不是建模者或不存在');
  assert.deepEqual(deleteRequest({ projectCount: 2, mode: 'trash', users: USERS, username: 'wanggong', trashEnabled: true }).body, { projects: 'trash' });
  assert.equal(deleteRequest({ projectCount: 2, mode: 'trash', users: USERS, username: 'wanggong', trashEnabled: false }).ok, false);

  assert.equal(deleteSub(USERS[1], 3), 'wanggong · 建模者 · 名下有 3 个项目');
  assert.equal(deleteSub(USERS[1], 0), 'wanggong · 建模者 · 名下没有项目');
  assert.equal(deleteSub(USERS[1], null), 'wanggong · 建模者');
  const nameOf = (u) => USERS.find((x) => x.username === u)?.display_name || u;
  assert.equal(deleteDoneText({ transferred: 3, transfer_to: 'sunyu' }, nameOf), '已删除用户，3 个项目已移交给孙宇');
  assert.equal(deleteDoneText({ trashed: 2 }), '已删除用户，2 个项目已移入回收站');
  assert.equal(deleteDoneText({ transferred: 0, trashed: 0 }), '已删除用户');
});

test('「移入回收站」与回收站页签按 retention_days：有值照契约，null（B2 第 15、24 条）只能移交、存量清空后不出页签（R20、R21）', () => {
  assert.deepEqual(trashOption(30), { enabled: true, text: '30 天内可恢复' });
  assert.deepEqual(trashOption(null), { enabled: false, text: null });
  assert.equal(trashOption(undefined).enabled, false);
  // 有保留期：页签照常、带「自动清除」列，空了也出
  assert.deepEqual(trashView({ retention_days: 30, items: [] }), { show: true, autoPurge: true });
  // 只剩旧版存量：有数据照常列、不出「自动清除」列；清空后整个页签不出；读失败时照常出（页签里报错）
  assert.deepEqual(trashView({ retention_days: null, items: [{ entry: 'x' }] }), { show: true, autoPurge: false });
  assert.deepEqual(trashView({ retention_days: null, items: [] }), { show: false, autoPurge: false });
  assert.equal(trashView({ retention_days: null, items: [] }, { loadError: true }).show, true);
  assert.deepEqual(trashView(null), { show: true, autoPurge: true });
  assert.equal(deleteRequest({ projectCount: 2, mode: 'trash', users: USERS, username: 'wanggong', trashEnabled: false }).ok, false);
  assert.equal(isTrashUnsupported({ code: 'invalid_argument', details: { fields: { projects: 'trash_unsupported' } } }), true);
  assert.equal(isTrashUnsupported({ code: 'invalid_argument', details: { fields: { projects: 'required' } } }), false);
});

test('回收站剩余天数：向上取整，过期算「今天」，purge_at 为 null 不自动清除', () => {
  const day = 24 * 3600 * 1000;
  assert.equal(daysLeft(new Date(NOW + 25.2 * day).toISOString(), NOW), 26);
  assert.equal(daysLeft(new Date(NOW - day).toISOString(), NOW), 0);
  assert.equal(daysLeft(null, NOW), null);
  assert.equal(purgeText(new Date(NOW + 5 * day).toISOString(), NOW), '5 天 后');
  assert.equal(purgeText(new Date(NOW + 3600 * 1000 * -2).toISOString(), NOW), '今天');
  assert.equal(purgeText(null, NOW), '不自动清除');
  assert.equal(restoreDoneText({ project: { name: '测试项目 0919' } }, { name: '测试项目 0919' }), '已恢复');
  assert.equal(restoreDoneText({ project: { name: '测试项目 0919' } }, { name: '测试项目 0919（恢复）' }), '已恢复为「测试项目 0919（恢复）」');
});

test('审计筛选：设计稿的分段去掉证据采集（证据源本期不开放），「全部」不带 type；分页合并按 id 去重，第一页整体替换', () => {
  assert.deepEqual(AUDIT_FILTERS.map(([k]) => k), ['all', 'write', 'mcp', 'commit', 'plugin', 'ops']);
  assert.equal(AUDIT_FILTERS.find(([k]) => k === 'plugin')[1], '插件');
  assert.deepEqual(auditQuery('all', '', 50), { limit: 50 });
  assert.deepEqual(auditQuery('plugin', '  王工 ', 50), { type: 'plugin', q: '王工', limit: 50 });

  const p1 = { items: [{ id: 'a' }, { id: 'b' }], next_cursor: '2|', total: 4 };
  const p2 = { items: [{ id: 'b' }, { id: 'c' }], next_cursor: null, total: 4 };
  const merged = mergePage(mergePage(null, p1, { append: false }), p2, { append: true });
  assert.deepEqual(merged.items.map((x) => x.id), ['a', 'b', 'c']);
  assert.equal(merged.next_cursor, null);
  assert.equal(merged.total, 4);
  assert.deepEqual(mergePage(merged, p1, { append: false }).items.map((x) => x.id), ['a', 'b']);
  // 回收站条目用 entry，用户用 username
  assert.deepEqual(mergePage({ items: [{ entry: 'x' }] }, { items: [{ entry: 'x' }, { entry: 'y' }] }, { append: true }).items.length, 2);
  assert.deepEqual(mergePage({ items: [{ username: 'u1' }] }, { items: [{ username: 'u1' }, { username: 'u2' }] }, { append: true }).items.length, 2);
  assert.equal(pagerText(5, 100), '显示最近 5 条，共 100 条');
  assert.equal(pagerText(5, null), '显示最近 5 条');
});

test('审计显示：操作者换显示名，内容只有摘要与 trailer（没有摘要时用方法和路径）', () => {
  const nameOf = (u) => ({ ops01: '周运维' }[u]);
  assert.equal(actorLabel({ actor: 'ops01', actor_kind: 'user' }, nameOf), '周运维');
  assert.equal(actorLabel({ actor: 'system', actor_kind: 'system' }, nameOf), '系统');
  assert.equal(actorLabel({ actor: 'agent', actor_kind: 'agent' }, nameOf), 'agent');
  assert.equal(actorLabel({ actor: 'mcp:业务 Agent 联调', actor_kind: 'mcp' }, nameOf), 'mcp:业务 Agent 联调');
  assert.equal(actorLabel({ actor: 'nobody', actor_kind: 'user' }, nameOf), 'nobody');
  assert.equal(auditContent({ summary: '门店补货单编制', trailer: { 'Onto-Source': 'agent', 'Onto-Changeset': 'cs-31' } }), '门店补货单编制 · Onto-Source: agent · Onto-Changeset: cs-31');
  assert.equal(auditContent({ summary: '', method: 'PUT', path: '/api/v1/projects/p1/file' }), 'PUT /api/v1/projects/p1/file');
  assert.equal(auditContent({ summary: '', action: 'http.post' }), 'http.post');
});

test('错误留档：B2 第 23 条 pack、node、project 为 null 时的显示；抽屉标题与执行行；链路跳转', () => {
  const full = { at: at(1, 15, 41, 7), project: { name: '共享单车故障诊断' }, pack: 'industry-retail@1.0.3', node: 'retail.compliance' };
  assert.equal(packNode(full), 'industry-retail@1.0.3 · retail.compliance');
  assert.equal(errorSub(full, NOW), '15:41:07 · 共享单车故障诊断 · industry-retail@1.0.3 · retail.compliance');
  const b2 = { at: at(1, 9, 31, 40), project: null, project_id: 'p-legacy', pack: null, node: null };
  assert.equal(packNode(b2), '—');
  assert.equal(errorProject(b2), 'p-legacy');
  assert.equal(errorProject({ project: null }), '—');
  assert.equal(errorSub({ id: 'err-x' }, NOW), '');
  assert.equal(errorSub(b2, NOW), '09:31:40 · p-legacy');
  assert.equal(execText({ exec_id: 'ex-0533', session_id: 'd2f8', changeset_id: null }), 'ex-0533 · 会话 d2f8');
  assert.equal(execText({ exec_id: null, session_id: null }), '—');
  assert.deepEqual(traceTarget('a71e03c9'), { path: '/ops-obs', query: { trace: 'a71e03c9' } });
  assert.equal(traceTarget(null), null);
});

test('指定所有者：用户名格式同契约 AD-07，必须是已有账号', () => {
  assert.equal(ownerRequest('', USERS).error, '请选择新的所有者');
  assert.equal(ownerRequest('bad name', USERS).error, '所有者用户名不合法');
  assert.equal(ownerRequest('x'.repeat(65), USERS).error, '所有者用户名不合法');
  assert.equal(ownerRequest('ghost', USERS).error, '这个用户不存在');
  assert.deepEqual(ownerRequest(' liming ', USERS), { ok: true, body: { owner: 'liming' } });
  assert.equal(ownerRequest('anyone.ok', null).ok, true);
});

// ---------- mock ----------

function server({ role = 'admin', sc = {} } = {}) {
  const routes = [];
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    forbid: (user, roles) => (roles.includes(user.role) ? undefined : new Response(JSON.stringify({ code: 'forbidden', message: '没有权限' }), { status: 403 })),
    sc,
    F,
    now: () => NOW,
  };
  register(ctx);
  const user = { username: 'zhangsan', role };
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user });
      if (res !== undefined) {
        const text = await res.text();
        let data = text;
        try { data = JSON.parse(text); } catch { /* 导出的 CSV / JSONL */ }
        return { status: res.status, body: data, headers: res.headers };
      }
    }
    assert.fail(`${method} ${path} 没有路由`);
  };
  return { call };
}

test('AD-01：23 个账号、按角色计数；ops 能读，user 不能；users:unavailable 时 available=false', async () => {
  const { call } = server();
  const r = await call('GET', '/api/v1/admin/users');
  assert.equal(r.status, 200);
  assert.equal(r.body.available, true);
  assert.equal(r.body.total, 23);
  assert.equal(r.body.items.length, 23);
  assert.deepEqual(r.body.stats.by_role, { user: 19, admin: 2, ops: 2 });
  assert.equal(r.body.items.find((u) => u.username === 'zhangsan').project_count, 6);
  assert.equal(r.body.items.find((u) => u.username === 'wanggong').project_count, 1);
  // 在线的排在前面
  assert.ok(r.body.items.slice(0, 9).every((u) => u.online));
  const paged = await call('GET', '/api/v1/admin/users?limit=10');
  assert.equal(paged.body.items.length, 10);
  assert.equal(paged.body.next_cursor, 'o:10');
  assert.equal((await call('GET', `/api/v1/admin/users?limit=10&cursor=${paged.body.next_cursor}`)).body.items[0].username, r.body.items[10].username);

  assert.equal((await server({ role: 'ops' }).call('GET', '/api/v1/admin/users')).status, 200);
  assert.equal((await server({ role: 'user' }).call('GET', '/api/v1/admin/users')).status, 403);

  const na = server({ sc: { users: 'unavailable' } });
  const nr = await na.call('GET', '/api/v1/admin/users');
  assert.equal(nr.body.available, false);
  assert.deepEqual(nr.body.items, []);
  assert.equal((await na.call('PATCH', '/api/v1/admin/users/wanggong', { role: 'ops' })).body.code, 'user_admin_unavailable');
});

test('AD-02：只有 admin 能改；可以设 ops；不能降级最后一个管理员；写一条审计', async () => {
  const { call } = server();
  assert.equal((await server({ role: 'ops' }).call('PATCH', '/api/v1/admin/users/wanggong', { role: 'ops' })).status, 403);
  const r = await call('PATCH', '/api/v1/admin/users/wanggong', { role: 'ops' });
  assert.equal(r.status, 200);
  assert.equal(r.body.role, 'ops');
  assert.equal((await call('PATCH', '/api/v1/admin/users/wanggong', { role: 'root' })).status, 400);
  assert.equal((await call('PATCH', '/api/v1/admin/users/nobody', { role: 'user' })).body.code, 'user_not_found');
  await call('PATCH', '/api/v1/admin/users/admin02', { role: 'user' });
  const last = await call('PATCH', '/api/v1/admin/users/zhangsan', { role: 'user' });
  assert.equal(last.status, 403);
  assert.equal(last.body.details.reason, 'last_admin');
  const audit = await call('GET', '/api/v1/ops/audit?type=user&limit=5');
  assert.equal(audit.body.items[0].action, 'user.role');
  assert.match(audit.body.items[1].summary, /wanggong：user → ops/);
});

test('AD-03：弱密码返回 weak_password 与 failed_rules；generate 返回一次生成的密码且满足规则', async () => {
  const { call } = server();
  const weak = await call('POST', '/api/v1/admin/users/wanggong/reset-password', { password: 'abc', require_change: false });
  assert.equal(weak.status, 400);
  assert.equal(weak.body.code, 'weak_password');
  assert.deepEqual(weak.body.details.failed_rules, ['len', 'classes']);
  assert.deepEqual((await call('POST', '/api/v1/admin/users/wanggong/reset-password', { password: 'Spring#2026' })).body, {});
  const gen = await call('POST', '/api/v1/admin/users/wanggong/reset-password', { generate: true });
  assert.equal(passwordProblems('wanggong', gen.body.generated_password).length, 0);
  assert.equal((await call('POST', '/api/v1/admin/users/wanggong/reset-password', {})).status, 400);
});

test('AD-04 / AD-05：移交给建模者后项目归新所有者；移入回收站进 O-52；trash:legacy 返回 trash_unsupported', async () => {
  const { call } = server();
  const before = await call('GET', '/api/v1/admin/users/wanggong/projects');
  assert.deepEqual(before.body.items.map((p) => p.name), ['快递分拣异常处理']);
  assert.ok(before.body.items[0].stage_label);
  assert.equal((await call('DELETE', '/api/v1/admin/users/wanggong', {})).body.details.fields.projects, 'required');
  assert.equal((await call('DELETE', '/api/v1/admin/users/wanggong', { projects: 'transfer', transfer_to: 'ops01' })).body.code, 'transfer_target_invalid');
  const done = await call('DELETE', '/api/v1/admin/users/wanggong', { projects: 'transfer', transfer_to: 'liming' });
  assert.deepEqual(done.body, { deleted: 'wanggong', transferred: 1, trashed: 0 });
  assert.equal((await call('GET', '/api/v1/admin/users/liming/projects')).body.items.length, 2);
  assert.equal((await call('GET', '/api/v1/admin/users')).body.total, 22);
  // 名下没有项目：不用带 projects
  assert.equal((await call('DELETE', '/api/v1/admin/users/fengxue')).status, 200);

  const trashed = await call('DELETE', '/api/v1/admin/users/sunyu', { projects: 'trash' });
  assert.equal(trashed.body.trashed, 1);
  const bin = await call('GET', '/api/v1/ops/trash');
  assert.equal(bin.body.items[0].project.name, '食堂菜单与备餐');
  assert.equal(bin.body.items.length, 5);

  const legacy = server({ sc: { trash: 'legacy' } });
  const no = await legacy.call('DELETE', '/api/v1/admin/users/sunyu', { projects: 'trash' });
  assert.equal(no.status, 400);
  assert.equal(isTrashUnsupported(no.body), true);
  const lt = await legacy.call('GET', '/api/v1/ops/trash');
  assert.equal(lt.body.retention_days, null);
  assert.ok(lt.body.items.every((t) => t.purge_at === null));
  // trash:legacy-empty：旧数据已清空，页面据此不出回收站页签（R21）
  const le = await server({ sc: { trash: 'legacy-empty' } }).call('GET', '/api/v1/ops/trash');
  assert.deepEqual([le.body.retention_days, le.body.items.length, trashView(le.body).show], [null, 0, false]);
  assert.equal((await server({ role: 'ops' }).call('GET', '/api/v1/admin/users/wanggong/projects')).status, 403);
});

test('AD-06 / AD-07：两个无主项目，只有 admin；用户名不合法 invalid_owner；指定后从列表里去掉并写审计', async () => {
  const { call } = server();
  assert.equal((await server({ role: 'ops' }).call('GET', '/api/v1/admin/projects/unowned')).status, 403);
  const list = await call('GET', '/api/v1/admin/projects/unowned');
  assert.equal(list.body.items.length, 2);
  assert.ok(list.body.items.every((p) => p.owner === null));
  const pid = list.body.items[0].id;
  assert.equal((await call('PUT', `/api/v1/admin/projects/${pid}/owner`, { owner: 'bad name' })).body.code, 'invalid_owner');
  assert.equal((await call('PUT', '/api/v1/admin/projects/nope/owner', { owner: 'liming' })).body.code, 'project_not_found');
  const set = await call('PUT', `/api/v1/admin/projects/${pid}/owner`, { owner: 'liming' });
  assert.equal(set.body.owner, 'liming');
  assert.equal((await call('GET', '/api/v1/admin/projects/unowned')).body.items.length, 1);
  const audit = await call('GET', '/api/v1/ops/audit?type=user&limit=1');
  assert.equal(audit.body.items[0].action, 'project.set_owner');
});

test('O-48 / O-49：按类型、关键词筛选，游标绑定筛选条件；导出带附件名并写审计；审计里没有项目正文', async () => {
  const { call } = server({ role: 'ops' });
  const all = await call('GET', '/api/v1/ops/audit?limit=20');
  assert.equal(all.body.items.length, 20);
  assert.ok(all.body.total > 50);
  assert.equal(all.body.items[0].summary, '导出诊断包 diag-20261101-1544.zip');
  const next = await call('GET', `/api/v1/ops/audit?limit=20&cursor=${encodeURIComponent(all.body.next_cursor)}`);
  assert.equal(next.body.items.length, 20);
  assert.notEqual(next.body.items[0].id, all.body.items[0].id);
  // 换了筛选条件还拿旧游标
  assert.equal((await call('GET', `/api/v1/ops/audit?type=plugin&cursor=${encodeURIComponent(all.body.next_cursor)}`)).body.code, 'invalid_cursor');
  const plugin = await call('GET', '/api/v1/ops/audit?type=plugin');
  assert.ok(plugin.body.items.length >= 4 && plugin.body.items.every((e) => e.type === 'plugin'));
  const q = await call('GET', `/api/v1/ops/audit?q=${encodeURIComponent('王工')}`);
  assert.ok(q.body.items.length && q.body.items.every((e) => e.actor === 'wanggong'));
  const commit = (await call('GET', '/api/v1/ops/audit?type=commit')).body.items[0];
  assert.equal(commit.trailer['Onto-Changeset'], 'cs-31');
  assert.equal(commit.actor_kind, 'agent');

  const csv = await call('GET', '/api/v1/ops/audit/export?type=ops&format=csv');
  assert.equal(csv.status, 200);
  assert.match(csv.headers.get('Content-Disposition'), /filename="audit-export\.csv"/);
  assert.match(csv.body.split('\n')[0], /^id,ts,actor,/);
  assert.equal((await call('GET', '/api/v1/ops/audit?type=ops&limit=1')).body.items[0].action, 'ops.audit_export');
  assert.equal((await call('GET', '/api/v1/ops/audit/export?format=xml')).status, 400);
  assert.equal((await server({ role: 'user' }).call('GET', '/api/v1/ops/audit')).status, 403);
});

test('O-50 / O-51：共 100 条、分页；详情给脱敏堆栈与上下文；找不到 error_record_not_found', async () => {
  const { call } = server({ role: 'ops' });
  const p1 = await call('GET', '/api/v1/ops/errors?limit=5');
  assert.equal(p1.body.total, 100);
  assert.equal(p1.body.items.length, 5);
  assert.equal(p1.body.items[0].friendly_message, '合规检查暂时不可用，已通知管理员');
  assert.equal(p1.body.items[0].project.name, '共享单车故障诊断');
  assert.equal(p1.body.items[0]._detail, undefined);
  const p2 = await call('GET', `/api/v1/ops/errors?limit=5&cursor=${p1.body.next_cursor}`);
  assert.equal(new Set([...p1.body.items, ...p2.body.items].map((e) => e.id)).size, 10);
  const d = await call('GET', `/api/v1/ops/errors/${p1.body.items[0].id}`);
  assert.match(d.body.stack, /^RuleSchemaError/);
  assert.equal(d.body.context.exec_id, 'ex-0533');
  assert.equal(d.body.auto_action, '包已熔断，节点暂时跳过，发布关卡解除');
  assert.equal((await call('GET', '/api/v1/ops/errors/err-none')).body.code, 'error_record_not_found');
  // 有的错误没有链路 ID（按钮停用）
  const all = (await call('GET', '/api/v1/ops/errors?limit=100')).body.items;
  assert.ok(all.some((e) => !e.trace_id) && all.some((e) => e.trace_id));
});

test('O-52～O-54：ops 和 admin 都能恢复、永久删除（202 {purged}）；trash:empty 为空', async () => {
  for (const role of ['ops', 'admin']) {
    const { call } = server({ role });
    const bin = await call('GET', '/api/v1/ops/trash');
    assert.equal(bin.body.retention_days, 30);
    assert.equal(bin.body.items.length, 4);
    assert.equal(daysLeft(bin.body.items[0].purge_at, NOW), 26);
    const restored = await call('POST', `/api/v1/ops/trash/${bin.body.items[0].entry}/restore`);
    assert.equal(restored.status, 200);
    assert.equal(restored.body.name, bin.body.items[0].project.name);
    const purged = await call('DELETE', `/api/v1/ops/trash/${bin.body.items[1].entry}`);
    assert.equal(purged.status, 202);
    assert.deepEqual(purged.body, { purged: bin.body.items[1].entry });
    assert.equal((await call('GET', '/api/v1/ops/trash')).body.items.length, 2);
    assert.equal((await call('DELETE', '/api/v1/ops/trash/nope')).body.code, 'trash_entry_not_found');
  }
  assert.equal((await server({ role: 'user' }).call('GET', '/api/v1/ops/trash')).status, 403);
  assert.deepEqual((await server({ sc: { trash: 'empty' } }).call('GET', '/api/v1/ops/trash')).body.items, []);
});
