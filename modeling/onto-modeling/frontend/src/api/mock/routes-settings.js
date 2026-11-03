// mock · 项目设置：P-12 引擎、P-14 助手可用的 MCP、PL-05～PL-11 本项目的能力与装配状态、M-01～M-04 对外 MCP；
// PL-12 扩展项由 index.js 那条路由调这里登记的 db.extensionsOf（启用、停用后左栏跟着变）。
// 自主构建策略（AB-01～AB-04、AB-18）在 routes-autobuild.js。
// ?mock=assembly:failed：装配失败（PL-11 state=failed，带 last_error）
// ?mock=off:reverse、off:trace+knowledge：一开始就停用这些能力（左栏、资料子页跟着消失，直接打开地址看到「本项目没有启用」）
import {
  DATA_CATEGORIES, OFFLINE_PACKS, SCOPES, clientConfigs, mcpOverview, mcpServers, mcpTokens, projectPacks, remainingText,
} from './settings-data.js';

// 带左栏页面、资料子页或产物类型的包，决定扩展项
const EXT_PACKS = ['trace', 'knowledge', 'codegen', 'reverse', 'diagram'];
const PAGE_OF = { trace: ['scenarios', '场景走查'], knowledge: ['knowledge', '知识沉淀'], solver: ['scripts', '代码生成'] };

export function actionsOf(p) {
  if (p.status === 'required') return [];
  if (p.status === 'pending') return ['cancel_pending'];
  if (p.status === 'disabled') return ['enable'];
  return [
    ...(p.latest_available && p.latest_available !== p.locked_version ? ['upgrade'] : []),
    ...(p.rollback_to ? ['rollback'] : []),
    'disable',
  ];
}

export function pendingChanges(items) {
  return items.filter((p) => p.pending).map((p) => ({ pack: p.name, from: p.pending.from, to: p.pending.to, kind: p.pending.kind }));
}

// 左栏按「生效中 + 待生效的启用」算：停用立即从左栏消失（契约 PL-09），新启用的页面带 pending 标记
export function extensionState(items) {
  const on = (p) => p.status === 'enabled' || p.status === 'required' || (p.status === 'pending' && p.pending?.kind !== 'disable');
  return {
    active: items.filter(on).map((p) => p.name),
    pendingEnable: items.filter((p) => p.pending?.kind === 'enable').map((p) => p.name),
    disabled: items.filter((p) => !on(p)).map((p) => p.name),
  };
}

const withActions = (p) => ({ ...p, actions: actionsOf(p) });

export function register(ctx) {
  const { on, json, err, db, sc, F, cardOf, emitProj, uid } = ctx;
  db.settings = new Map();
  const stOf = (pid) => {
    if (!db.settings.has(pid)) {
      // ?mock=off:<包名>[+<包名>]：一开始就停用这些能力（插件页的「本项目没有启用」空状态用）
      const off = (sc.off || '').split(/[+\s]+/).filter(Boolean); // 地址里的 + 会解码成空格
      if (sc.evidence === 'off') off.push('reverse'); // ?mock=evidence:off：系统逆向建模包不在（本期后端没有这个包）
      const packs = projectPacks().map((p) => (off.includes(p.name) && p.status !== 'required' ? { ...p, status: 'disabled', locked_version: p.locked_version } : p));
      db.seedProjectPacks?.(pid, packs); // 插件中心的 mock 状态（routes-plugins.js 的 ?mock=plugins:…）在这里改初始能力表
      db.settings.set(pid, { packs, servers: mcpServers(), tokens: mcpTokens(), rev: 1 });
    }
    return db.settings.get(pid);
  };
  const running = (pid) => !!db.autobuildActiveOf?.(pid);

  function extensionsOf(pid) {
    const s = stOf(pid);
    const x = extensionState(s.packs);
    const ext = F.extensions(x.active.filter((n) => EXT_PACKS.includes(n)));
    const pending = pendingChanges(s.packs).length > 0;
    return {
      ...ext,
      active_hash: `a${s.rev.toString(16).padStart(7, '0')}`,
      pending_hash: pending ? `p${s.rev.toString(16).padStart(7, '0')}` : null,
      pages: ext.pages.map((p) => ({ ...p, pending: x.pendingEnable.includes(p.pack) })),
      disabled_pages: x.disabled.filter((n) => PAGE_OF[n]).map((n) => ({ id: PAGE_OF[n][0], pack: n, title: PAGE_OF[n][1] })),
    };
  }
  db.extensionsOf = extensionsOf;

  function assemblyOf(pid) {
    const s = stOf(pid);
    const changes = pendingChanges(s.packs);
    const failed = sc.assembly === 'failed' && pid === F.MAIN_PID;
    return {
      active_hash: `a${s.rev.toString(16).padStart(7, '0')}`,
      pending_hash: changes.length ? `p${s.rev.toString(16).padStart(7, '0')}` : null,
      lock_hash: `l${s.rev.toString(16).padStart(7, '0')}`,
      flow_hash: 'b41e09c2a7',
      state: failed ? 'failed' : changes.length ? 'pending' : 'applied',
      pending_changes: changes,
      effective: 'next_message',
      last_error: failed ? { error_id: 'err-7c1e40', message: '图示 0.6.0 的流程挂载点在基础建模 1.4.2 里不存在', at: new Date(Date.now() - 6 * 60000).toISOString() } : null,
    };
  }

  // 切换之后：锁文件变了 → 装配状态、扩展项（左栏）都要通知外壳
  function changed(pid, extChanged) {
    const s = stOf(pid);
    s.rev += 1;
    const a = assemblyOf(pid);
    emitProj(pid, 'assembly.updated', { state: a.state, pending_changes: a.pending_changes, effective: a.effective, error_id: a.last_error?.error_id || null });
    if (extChanged) {
      const e = extensionsOf(pid);
      emitProj(pid, 'extensions.updated', { active_hash: e.active_hash, pending_hash: e.pending_hash, changed: ['nav', 'pages', 'subpages', 'artifact_types'] });
    }
  }
  const switched = (pid, p, effective, status = 200) => json(status, {
    pack: withActions(p), commit: `c${uid().padStart(7, '0')}`, version_label: cardOf(pid)?.version_label || null, effective,
    extensions_pending_hash: extensionsOf(pid).pending_hash,
  });
  const guard = (pid) => {
    const c = cardOf(pid);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    if (c.archived) return err(409, 'project_archived', '项目已归档，只能查看');
    return null;
  };

  // ---------- P-12 引擎 ----------
  on('GET', '/projects/([^/]+)/engine', ({ m }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    return json(200, { engine: c.engine, model_source: (ctx.db.me?.modelSettings || F.modelSettings()).engines[c.engine]?.source || 'deployment' });
  });
  on('PUT', '/projects/([^/]+)/engine', ({ m, body }) => {
    const pid = m[1];
    const g = guard(pid);
    if (g) return g;
    const eng = F.engines({ betaInstalled: sc.engine !== 'missing' }).items.find((x) => x.id === body.engine);
    if (!eng) return err(400, 'invalid_argument', '没有这个引擎', { fields: { engine: 'unknown' } });
    if (!eng.installed) return err(409, 'engine_unavailable', eng.unavailable_reason || '当前部署未安装');
    if (running(pid)) return err(409, 'autobuild_running', '自主构建运行中', { run_id: db.autobuildActiveOf(pid).run_id });
    const c = cardOf(pid);
    Object.assign(c, { engine: eng.id, engine_label: eng.label });
    emitProj(pid, 'project.updated', { fields: ['engine'], meta: F.projectMeta(c) });
    return json(200, { engine: eng.id });
  });

  // ---------- P-14 助手可用的 MCP 服务器 ----------
  on('GET', '/projects/([^/]+)/mcp-servers', ({ m }) => json(200, { items: stOf(m[1]).servers }));
  on('PUT', '/projects/([^/]+)/mcp-servers', ({ m, body }) => {
    const g = guard(m[1]);
    if (g) return g;
    const off = body.disabled || [];
    if (!Array.isArray(off) || off.length > 64) return err(400, 'invalid_argument', '最多停用 64 个', { fields: { disabled: 'too_many' } });
    const s = stOf(m[1]);
    s.servers = s.servers.map((x) => ({ ...x, enabled: x.enabled_globally && !off.includes(x.name) }));
    return json(200, { items: s.servers });
  });

  // ---------- PL-05～PL-11 本项目的能力 ----------
  on('GET', '/projects/([^/]+)/packs', ({ m }) => {
    const s = stOf(m[1]);
    const a = assemblyOf(m[1]);
    return json(200, { items: s.packs.map(withActions), pending_switches: a.pending_changes.length, lock_hash: a.lock_hash, flow_hash: a.flow_hash });
  });
  on('GET', '/projects/([^/]+)/assembly', ({ m }) => json(200, assemblyOf(m[1])));
  on('POST', '/projects/([^/]+)/packs', ({ m, body }) => {
    const pid = m[1];
    const g = guard(pid);
    if (g) return g;
    const s = stOf(pid);
    let p = s.packs.find((x) => x.name === body.name);
    if (p && p.status !== 'disabled') return err(409, 'pack_already_enabled', '已经启用了');
    const card = (db.packCatalog?.() || F.packs()).find((x) => x.name === body.name); // 插件目录（routes-plugins.js）比 packs() 多租户包
    if (!p && !card) return err(404, 'pack_not_found', '找不到这个能力包');
    if (OFFLINE_PACKS.includes(body.name)) return err(409, 'pack_unavailable', '离线部署不可用', { reason: 'offline' });
    const version = body.version || p?.locked_version || card.latest;
    if (!p) {
      p = { name: card.name, label: card.label, category: card.category, ui: card.ui, locked_version: version, latest_available: card.latest, channel: card.channel, status: 'disabled', pending: null, enabled_at: null, rollback_to: null, added: true };
      s.packs.push(p);
    }
    const immediate = DATA_CATEGORIES.includes(p.category);
    if (immediate) Object.assign(p, { status: 'enabled', locked_version: version, pending: null, enabled_at: new Date().toISOString() });
    else Object.assign(p, { status: 'pending', pending: { from: null, to: version, kind: 'enable', effective: 'next_message' } });
    changed(pid, EXT_PACKS.includes(p.name));
    return switched(pid, p, immediate ? 'immediate' : 'next_message', 201);
  });
  on('POST', '/projects/([^/]+)/packs/([^/]+)/(upgrade|rollback|disable|cancel-pending)', ({ m, body }) => {
    const [pid, name, op] = [m[1], decodeURIComponent(m[2]), m[3]];
    const g = guard(pid);
    if (g) return g;
    const s = stOf(pid);
    const p = s.packs.find((x) => x.name === name);
    if (!p) return err(404, 'pack_not_found', '找不到这个能力包');
    const immediate = DATA_CATEGORIES.includes(p.category);
    if (op === 'cancel-pending') {
      if (!p.pending) return err(409, 'no_pending_switch', '没有待生效的切换');
      const kind = p.pending.kind;
      if (kind === 'enable' && p.added) s.packs.splice(s.packs.indexOf(p), 1);
      Object.assign(p, { status: kind === 'enable' ? 'disabled' : 'enabled', pending: null });
      changed(pid, EXT_PACKS.includes(p.name) && (kind === 'enable' || kind === 'disable'));
      return json(200, { pack: withActions(p) });
    }
    if (op === 'disable') {
      if (p.status === 'required') return err(409, 'pack_required', '必装的能力不能停用');
      if (p.status === 'disabled' || p.pending?.kind === 'disable') return err(409, 'pack_not_enabled', '本项目没有启用这个能力');
      if (immediate) Object.assign(p, { status: 'disabled', pending: null });
      else Object.assign(p, { status: 'pending', pending: { from: p.locked_version, to: null, kind: 'disable', effective: 'next_message' } });
      changed(pid, EXT_PACKS.includes(p.name));
      return switched(pid, p, immediate ? 'immediate' : 'next_message');
    }
    if (p.status !== 'enabled') return err(409, 'pack_not_enabled', p.status === 'pending' ? '已有一个待生效的切换，先撤销再改' : '本项目没有启用这个能力');
    const to = body.to_version;
    const allowed = op === 'upgrade' ? p.latest_available : p.rollback_to;
    if (!to || to !== allowed) return err(404, 'pack_version_not_found', '找不到这个版本', { version: to || null });
    if (immediate) Object.assign(p, { locked_version: to, rollback_to: op === 'upgrade' ? p.locked_version : null });
    else Object.assign(p, { status: 'pending', pending: { from: p.locked_version, to, kind: op, effective: body.effective === 'next_autobuild' ? 'next_autobuild' : 'next_message' } });
    changed(pid, false);
    return switched(pid, p, immediate ? 'immediate' : 'next_message');
  });

  // ---------- M-01～M-04 对外 MCP ----------
  on('GET', '/projects/([^/]+)/mcp', ({ m }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    return json(200, mcpOverview(m[1], { archived: c.archived }));
  });
  on('GET', '/projects/([^/]+)/mcp/tokens', ({ m }) => {
    const s = stOf(m[1]);
    s.tokens.forEach((t) => { t.remaining_text = remainingText(t.created_at, t.expires_at); });
    return json(200, { items: s.tokens });
  });
  on('POST', '/projects/([^/]+)/mcp/tokens', ({ m, body, user }) => {
    const pid = m[1];
    const name = String(body.name || '').trim();
    const scopes = Array.isArray(body.scopes) ? body.scopes : [];
    const ttl = body.expires_in_s;
    const fields = {};
    if (!name || name.length > 120) fields.name = name ? 'too_long' : 'required';
    if (!scopes.length || scopes.some((x) => ![...SCOPES, 'evidence.read'].includes(x))) fields.scopes = scopes.length ? 'unknown_scope' : 'required';
    if (ttl !== null && !(Number.isInteger(ttl) && ttl >= 300 && ttl <= 2592000)) fields.expires_in_s = 'out_of_range';
    if (Object.keys(fields).length) return err(400, 'invalid_argument', '参数不合法', { fields });
    const s = stOf(pid);
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    const token = `onto_pat_${Array.from({ length: 28 }, () => alphabet[Math.floor(Math.random() * alphabet.length)]).join('')}`;
    const now = new Date().toISOString();
    const expires = ttl ? new Date(Date.now() + ttl * 1000).toISOString() : null;
    const summary = { id: `tok-${uid()}`, name, scopes, subject: user?.username || 'zhangsan', issued_by: user?.display_name || '张工', created_at: now, expires_at: expires, last_used_at: null, remaining_text: remainingText(now, expires) };
    s.tokens.unshift(summary);
    return json(201, { token, summary, client_config: clientConfigs(pid, token, scopes) });
  });
  on('DELETE', '/projects/([^/]+)/mcp/tokens/([^/]+)', ({ m }) => {
    const s = stOf(m[1]);
    const i = s.tokens.findIndex((t) => t.id === m[2]);
    if (i < 0) return err(404, 'not_found', 'Token 不存在');
    s.tokens.splice(i, 1);
    return new Response(null, { status: 204 });
  });
}
