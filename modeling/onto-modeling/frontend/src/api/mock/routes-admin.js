// mock：用户、审计与回收站（AD-01～AD-05、O-48～O-54），以及没挂界面的无主项目与指定所有者（AD-06、AD-07）。页面：ops-users
// 运维面路径登记成 '/ops/...'（客户端按 ops_base_url 拼，mock 只看路径）；角色用 ctx.forbid 把关。
// 地址参数（?mock=，逗号分隔）：
// - users:unavailable  没配认证服务管理令牌：AD-01 available=false、列表为空，AD-02～AD-05 返回 503 user_admin_unavailable；
// - trash:empty        回收站为空；
// - trash:legacy       B2 后端口径：O-52 retention_days、purge_at 为 null，AD-04 选「移入回收站」返回 trash_unsupported；
// - trash:legacy-empty 同上，旧数据已清空（回收站页签不出，R21）。
// 页面上的写操作会追加一条审计（新的在最前），审计页能看到刚才的改角色、重置密码、删除用户、恢复与永久删除。
import { passwordRules } from '../../pages/login/passwordRules.js';
import { metaView, platformMetaViews, seedAudit, seedErrors, seedTrash, seedUnowned, seedUsers } from './admin-data.js';

const DAY = 24 * 3600 * 1000;
const ROLES = ['user', 'admin', 'ops'];
const OWNER_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const COMMON_FAIL = ['len', 'max', 'classes', 'contains_username', 'common', 'edge_space'];

export function register(ctx) {
  const { on, json, err, forbid, sc = {} } = ctx;
  const now = () => (ctx.now ? ctx.now() : Date.now());
  const unavailable = sc.users === 'unavailable';
  const legacyTrash = sc.trash === 'legacy' || sc.trash === 'legacy-empty';

  const st = {
    users: seedUsers(now(), sc.role),
    owners: {}, // 项目 ID → 移交后的所有者
    removed: new Set(), // 进了回收站的项目
    unowned: seedUnowned(now()),
    audit: seedAudit(now()),
    errors: seedErrors(now()),
    trash: sc.trash === 'empty' || sc.trash === 'legacy-empty' ? [] : seedTrash(now(), { legacy: legacyTrash }),
    seq: 0,
  };

  const userOf = (name) => st.users.find((u) => u.username === name);
  const nameOf = (name) => userOf(name)?.display_name || name;
  const projects = () => platformMetaViews(now())
    .filter((p) => !st.removed.has(p.id))
    .map((p) => (st.owners[p.id] ? { ...p, owner: st.owners[p.id] } : p));
  const projectsOf = (username) => projects().filter((p) => p.owner === username);
  const countOf = (username) => projectsOf(username).length;
  const view = (u) => ({ ...u, project_count: countOf(u.username) });
  const unavailableErr = () => err(503, 'user_admin_unavailable', '未配置认证服务管理令牌，用户管理不可用');

  function record(user, e) {
    st.seq += 1;
    st.audit.unshift({
      id: `aud-new${String(st.seq).padStart(5, '0')}`, ts: new Date(now()).toISOString(), actor: user?.username || 'local', actor_kind: 'user',
      type: e.type, action: e.action, method: e.method, path: e.path, status: e.status || 200, project_id: e.project_id || null,
      summary: e.summary, trailer: null, ip: '192.0.2.21', trace_id: null,
    });
  }

  // ---------- AD-01～AD-05 用户 ----------

  on('GET', '/admin/users', ({ user, query }) => forbid(user, ['admin', 'ops']) || (() => {
    const stats = { accounts: 0, by_role: { user: 0, admin: 0, ops: 0 }, new_7d: 2, locked: 1, registration_enabled: false, auth: { alg: 'RS256', issuer_checked: true } };
    if (unavailable) return json(200, { items: [], next_cursor: null, total: 0, stats: { ...stats, new_7d: null, locked: null }, available: false });
    stats.accounts = st.users.length;
    st.users.forEach((u) => { stats.by_role[u.role] = (stats.by_role[u.role] || 0) + 1; });
    const q = (query.get('q') || '').trim().toLowerCase();
    const list = st.users
      .filter((u) => !q || u.username.toLowerCase().includes(q) || (u.display_name || '').includes(q))
      .slice()
      .sort((a, b) => (b.online - a.online) || (Date.parse(b.last_seen_at || 0) - Date.parse(a.last_seen_at || 0)))
      .map(view);
    const limit = Math.min(Number(query.get('limit')) || 50, 200);
    const offset = Number((query.get('cursor') || '').replace(/^o:/, '')) || 0;
    const page = list.slice(offset, offset + limit);
    return json(200, { items: page, next_cursor: offset + limit < list.length ? `o:${offset + limit}` : null, total: list.length, stats, available: true });
  })());

  on('PATCH', '/admin/users/([^/]+)', ({ user, m, body }) => forbid(user, ['admin']) || (() => {
    if (unavailable) return unavailableErr();
    const name = decodeURIComponent(m[1]);
    const target = userOf(name);
    if (!target) return err(404, 'user_not_found', `用户 ${name} 不存在`);
    if (!ROLES.includes(body.role)) return err(400, 'invalid_argument', '角色只能是 user、admin 或 ops', { fields: { role: 'enum' } });
    if (target.role === 'admin' && body.role !== 'admin' && st.users.filter((u) => u.role === 'admin').length <= 1) {
      return err(403, 'forbidden', '不能降级最后一个管理员', { reason: 'last_admin' });
    }
    const from = target.role;
    target.role = body.role;
    record(user, { type: 'user', action: 'user.role', method: 'PATCH', path: `/api/v1/admin/users/${name}`, summary: `改角色 ${name}：${from} → ${body.role}` });
    return json(200, view(target));
  })());

  on('POST', '/admin/users/([^/]+)/reset-password', ({ user, m, body }) => forbid(user, ['admin']) || (() => {
    if (unavailable) return unavailableErr();
    const name = decodeURIComponent(m[1]);
    if (!userOf(name)) return err(404, 'user_not_found', `用户 ${name} 不存在`);
    let generated = null;
    if (body.generate) {
      st.seq += 1;
      generated = `Tmp-${(0x5a3c91 + st.seq * 7331).toString(36)}#Rq${st.seq}`;
    } else {
      if (!body.password) return err(400, 'invalid_argument', '请填写新密码或选择自动生成', { fields: { password: 'required' } });
      const r = passwordRules(name, body.password);
      const failed = COMMON_FAIL.filter((k) => !r[k]);
      if (failed.length) return err(400, 'weak_password', '密码不满足规则', { failed_rules: failed });
    }
    record(user, { type: 'user', action: 'user.password_reset', method: 'POST', path: `/api/v1/admin/users/${name}/reset-password`, summary: `重置 ${name} 的密码${generated ? '（服务端生成）' : ''}` });
    return json(200, generated ? { generated_password: generated } : {});
  })());

  on('DELETE', '/admin/users/([^/]+)', ({ user, m, body }) => forbid(user, ['admin']) || (() => {
    if (unavailable) return unavailableErr();
    const name = decodeURIComponent(m[1]);
    const target = userOf(name);
    if (!target) return err(404, 'user_not_found', `用户 ${name} 不存在`);
    const owned = projectsOf(name);
    let transferred = 0;
    let trashed = 0;
    if (owned.length || body?.projects) {
      if (body?.projects === 'trash') {
        if (legacyTrash) return err(400, 'invalid_argument', '暂不支持把项目移入回收站，请移交给其他用户', { fields: { projects: 'trash_unsupported' } });
        owned.forEach((p) => {
          st.removed.add(p.id);
          const at = now();
          st.trash.unshift({ entry: `trash-${p.id}`, project: { ...p, disk_bytes: undefined }, deleted_at: new Date(at).toISOString(), size: p.disk_bytes, purge_at: new Date(at + 30 * DAY).toISOString() });
          trashed += 1;
        });
      } else if (body?.projects === 'transfer') {
        const to = (body.transfer_to || '').trim();
        const dest = userOf(to);
        if (!to || to === name || !dest || dest.role !== 'user') return err(400, 'transfer_target_invalid', '移交对象不是建模者或不存在');
        owned.forEach((p) => { st.owners[p.id] = to; transferred += 1; });
      } else {
        return err(400, 'invalid_argument', '请选择名下项目的去向', { fields: { projects: 'required' } });
      }
    }
    st.users = st.users.filter((u) => u.username !== name);
    record(user, { type: 'user', action: 'user.delete', method: 'DELETE', path: `/api/v1/admin/users/${name}`, summary: `删除用户 ${name}${transferred ? ` · ${transferred} 个项目移交给 ${body.transfer_to}` : ''}${trashed ? ` · ${trashed} 个项目移入回收站` : ''}` });
    return json(200, { deleted: name, transferred, trashed });
  })());

  on('GET', '/admin/users/([^/]+)/projects', ({ user, m }) => forbid(user, ['admin']) || (() => {
    if (unavailable) return unavailableErr();
    const name = decodeURIComponent(m[1]);
    if (!userOf(name)) return err(404, 'user_not_found', `用户 ${name} 不存在`);
    return json(200, { items: projectsOf(name) });
  })());

  // ---------- AD-06、AD-07 无主项目（设计稿没画入口，页面不挂） ----------

  on('GET', '/admin/projects/unowned', ({ user }) => forbid(user, ['admin']) || json(200, { items: st.unowned }));

  on('PUT', '/admin/projects/([^/]+)/owner', ({ user, m, body }) => forbid(user, ['admin']) || (() => {
    const pid = decodeURIComponent(m[1]);
    const owner = String(body.owner || '').trim();
    if (!OWNER_RE.test(owner)) return err(400, 'invalid_owner', '所有者用户名不合法');
    const i = st.unowned.findIndex((p) => p.id === pid);
    const known = projects().find((p) => p.id === pid);
    if (i < 0 && !known) return err(404, 'project_not_found', '项目不存在');
    const before = i >= 0 ? st.unowned[i] : known;
    const after = metaView({ ...before, owner });
    if (i >= 0) st.unowned.splice(i, 1);
    else st.owners[pid] = owner;
    record(user, { type: 'user', action: 'project.set_owner', method: 'PUT', path: `/api/v1/admin/projects/${pid}/owner`, project_id: pid, summary: `指定项目所有者 ${pid}：${before.owner || '无'} → ${owner}` });
    return json(200, after);
  })());

  // ---------- O-48、O-49 审计 ----------

  function filteredAudit(query) {
    const types = (query.get('type') || '').split(',').filter(Boolean);
    const q = (query.get('q') || '').trim().toLowerCase();
    const from = query.get('from') ? Date.parse(query.get('from')) : null;
    const to = query.get('to') ? Date.parse(query.get('to')) : null;
    const matched = st.audit.filter((e) => {
      const t = Date.parse(e.ts);
      if ((from != null && t < from) || (to != null && t > to)) return false;
      if (types.length && !types.includes(e.type)) return false;
      if (q && ![e.actor, nameOf(e.actor), e.project_id, e.path, e.summary, e.action].some((v) => String(v || '').toLowerCase().includes(q))) return false;
      return true;
    });
    return { matched, bind: `${types.sort().join('+')}~${q}` };
  }

  on('GET', '/ops/audit', ({ user, query }) => forbid(user, ['ops', 'admin']) || (() => {
    const { matched, bind } = filteredAudit(query);
    const limit = Math.min(Number(query.get('limit')) || 100, 200);
    const cursor = query.get('cursor') || '';
    let offset = 0;
    if (cursor) {
      const [o, b] = cursor.split('|');
      if (b !== bind) return err(400, 'invalid_cursor', '列表条件变了，请刷新后重试');
      offset = Number(o) || 0;
    }
    const page = matched.slice(offset, offset + limit);
    return json(200, { items: page, next_cursor: offset + limit < matched.length ? `${offset + limit}|${bind}` : null, total: matched.length });
  })());

  on('GET', '/ops/audit/export', ({ user, query }) => forbid(user, ['ops', 'admin']) || (() => {
    const format = query.get('format') || 'jsonl';
    if (!['jsonl', 'csv'].includes(format)) return err(400, 'invalid_argument', 'format 只能是 jsonl 或 csv', { fields: { format: 'enum' } });
    const items = filteredAudit(query).matched.slice(0, 100000);
    const cols = ['id', 'ts', 'actor', 'actor_kind', 'type', 'action', 'method', 'path', 'status', 'project_id', 'summary', 'ip', 'trace_id'];
    const cell = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
    const text = format === 'jsonl'
      ? items.map((e) => JSON.stringify(e)).join('\n') + (items.length ? '\n' : '')
      : [cols.join(','), ...items.map((e) => cols.map((c) => cell(e[c])).join(','))].join('\n') + '\n';
    record(user, { type: 'ops', action: 'ops.audit_export', method: 'GET', path: '/api/v1/ops/audit/export', summary: `导出审计 ${items.length} 条（${format}）` });
    return new Response(text, {
      status: 200,
      headers: { 'Content-Type': format === 'csv' ? 'text/csv' : 'application/x-ndjson', 'Content-Disposition': `attachment; filename="audit-export.${format}"` },
    });
  })());

  // ---------- O-50、O-51 错误留档 ----------

  const publicError = ({ _detail, ...rest }) => rest;

  on('GET', '/ops/errors', ({ user, query }) => forbid(user, ['ops', 'admin']) || (() => {
    const limit = Math.min(Number(query.get('limit')) || 20, 200);
    const offset = Number(query.get('cursor') || 0) || 0;
    const page = st.errors.slice(offset, offset + limit).map(publicError);
    return json(200, { items: page, next_cursor: offset + limit < st.errors.length ? String(offset + limit) : null, total: st.errors.length });
  })());

  on('GET', '/ops/errors/([^/]+)', ({ user, m }) => forbid(user, ['ops', 'admin']) || (() => {
    const e = st.errors.find((x) => x.id === decodeURIComponent(m[1]));
    if (!e) return err(404, 'error_record_not_found', '错误留档不存在');
    return json(200, { ...publicError(e), stack: e._detail.stack, context: e._detail.context, auto_action: e._detail.auto_action });
  })());

  // ---------- O-52～O-54 回收站（ops、admin 都能操作） ----------

  on('GET', '/ops/trash', ({ user }) => forbid(user, ['ops', 'admin']) || json(200, { retention_days: legacyTrash ? null : 30, items: st.trash }));

  on('POST', '/ops/trash/([^/]+)/restore', ({ user, m }) => forbid(user, ['ops', 'admin']) || (() => {
    const entry = decodeURIComponent(m[1]);
    const i = st.trash.findIndex((t) => t.entry === entry);
    if (i < 0) return err(404, 'trash_entry_not_found', '回收站条目不存在');
    const [item] = st.trash.splice(i, 1);
    const taken = projects().some((p) => p.owner === item.project.owner && p.name === item.project.name);
    const restored = { ...item.project, name: taken ? `${item.project.name}（恢复）` : item.project.name };
    st.removed.delete(restored.id);
    record(user, { type: 'ops', action: 'ops.trash_restore', method: 'POST', path: `/api/v1/ops/trash/${entry}/restore`, project_id: restored.id, summary: `恢复回收站项目 ${entry}` });
    return json(200, restored);
  })());

  on('DELETE', '/ops/trash/([^/]+)', ({ user, m }) => forbid(user, ['ops', 'admin']) || (() => {
    const entry = decodeURIComponent(m[1]);
    const i = st.trash.findIndex((t) => t.entry === entry);
    if (i < 0) return err(404, 'trash_entry_not_found', '回收站条目不存在');
    st.trash.splice(i, 1);
    record(user, { type: 'ops', action: 'ops.trash_purge', method: 'DELETE', path: `/api/v1/ops/trash/${entry}`, summary: `永久删除回收站项目 ${entry}` });
    return json(202, { purged: entry });
  })());
}
