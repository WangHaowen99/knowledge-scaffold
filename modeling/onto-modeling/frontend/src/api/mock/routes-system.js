// mock：系统设置（SY-01～SY-17），以及没挂界面的模型单价（SY-18）与服务令牌（X-06）。页面：sys
// 全部只给 admin（ctx.forbid）；SY-14 配置注册表 ops 也能读。C-06 /engines 在 index.js。
// 地址参数 ?mock=sys:<状态>，几个状态用 + 连起来（如 sys:p1+ctxbad）：
// - sys:p1      按后端现状（B2、B3b）返回：SY-10 purpose 为 null；SY-12 status 恒 unknown、tools 为空，auth 只认 none；
//               SY-13 tools 为空；SY-15～SY-18、X-06 返回 404（还没做，gaps B2 第 12 条）
// - sys:ctxbad  自定义推荐表有误（custom.status=invalid，第 4 行「上下文窗口」不是数字），只用内置表
// - sys:ctxoff  自定义推荐表已停用（status=disabled，留有备份）
// - sys:boxdown 脚本验证沙箱连不上（与 F3 的 sandbox:off 等价）
// - sys:nolimits 只有 SY-17 自主构建限制回 404（配额照常），看「只缺一组接口时不画那张卡」（R17）
// 另外认 engine:missing（内测引擎没安装，SY-01 installed=false）；个人 Key 策略与个人设置共用 db.me.modelSettings.personal_allowed。
import {
  mergePresets, matchPreset, normalizeModelName, parseIntText, validatePresets,
} from '../../pages/sys/sysModel.js';
import { PLATFORM_USERS, platformProjects } from './platform-data.js';
import * as S from './system-data.js';

const ENGINES = ['claude', 'jiuwen'];
const QUOTA_KEYS = {
  user: ['concurrent_sessions', 'daily_tokens', 'collections_per_source_per_hour'],
  project: ['monthly_tokens', 'snapshot_git_mb'],
};

export function register(ctx) {
  const { on, json, err, db, sc, uid, forbid } = ctx;
  const flags = new Set(String(sc.sys || '').split('+').filter(Boolean));
  const p1 = flags.has('p1');
  const noLimits = p1 || flags.has('nolimits');
  const admin = (user) => forbid(user, ['admin']);
  const gone = () => err(404, 'not_found', '路径不存在');

  if (!db.sys) {
    const custom = flags.has('ctxbad') || flags.has('ctxoff') ? [] : S.customPresetsSeed();
    db.sys = {
      model: S.modelSettings({ betaInstalled: sc.engine !== 'missing', personalAllowed: db.me?.modelSettings?.personal_allowed ?? true }),
      custom,
      customState: flags.has('ctxbad') ? 'invalid' : flags.has('ctxoff') ? 'disabled' : 'ok',
      customVersion: 'c-7f3a',
      customVerifiedAt: '2026-10-20',
      customLoadedAt: custom.length ? S.CUSTOM_LOADED_AT() : null,
      backup: flags.has('ctxoff'),
      session: S.sessionSettings(),
      sandboxUp: !flags.has('boxdown') && sc.sandbox !== 'off',
      mcp: S.mcpServers({ p1 }),
      quotas: S.quotas(),
      limits: S.autobuildLimits(),
      pricing: S.pricing(),
      tokens: S.serviceTokens(),
      tokenSeq: 0,
    };
  }
  const st = db.sys;

  // ---------- SY-01～SY-04 模型接入 ----------
  const modelView = () => ({ ...st.model, personal_key_allowed: db.me?.modelSettings?.personal_allowed ?? st.model.personal_key_allowed });
  on('GET', '/admin/settings/model', ({ user }) => admin(user) || json(200, modelView()));
  on('PUT', '/admin/settings/model/([^/]+)', ({ m, user, body }) => {
    const no = admin(user);
    if (no) return no;
    const id = m[1];
    if (!ENGINES.includes(id)) return err(400, 'invalid_argument', '未知引擎', { fields: { engine: 'enum' } });
    const e = st.model.engines[id];
    if (body.base_url != null && body.base_url !== '' && !/^https?:\/\/\S+$/i.test(body.base_url)) {
      return err(400, 'invalid_argument', '网关地址要以 http:// 或 https:// 开头', { fields: { base_url: 'invalid' } });
    }
    const c = body.context || {};
    const bad = (v, lo, hi) => v != null && !(Number.isInteger(v) && v >= lo && v <= hi);
    if (bad(c.window, 1000, 10000000)) return err(400, 'invalid_argument', '上下文窗口要在 1,000–10,000,000 之间', { fields: { 'context.window': 'range' } });
    if (bad(c.max_output, 1000, 1000000)) return err(400, 'invalid_argument', '最大输出要在 1,000–1,000,000 之间', { fields: { 'context.max_output': 'range' } });
    if (bad(c.compact_at_pct, 50, 95)) return err(400, 'invalid_argument', '自动压缩阈值要在 50–95 之间', { fields: { 'context.compact_at_pct': 'range' } });
    const next = { ...e, context: { ...e.context } };
    if (body.base_url !== undefined) next.base_url = body.base_url;
    if (body.model !== undefined) next.model = body.model;
    if (body.api_key) {
      next.api_key_set = true;
      next.api_key_masked = 'sk-•••• •••• ••••';
    }
    if ('window' in c) next.context.window = c.window;
    if ('max_output' in c) next.context.max_output = c.max_output;
    // compact_at_pct 只校验不保存（B2 第 9 条）：按窗口与最大输出推算
    if ('window' in c || 'max_output' in c) {
      next.context.compact_at_pct = S.deriveCompact(next.context.window ?? e.context_defaults.window.value, next.context.max_output ?? e.context_defaults.max_output.value);
      next.context_defaults = { ...e.context_defaults, compact_at_pct: { value: next.context.compact_at_pct, source: 'derived' } };
    }
    next.configured = true;
    st.model.engines[id] = next;
    return json(200, next);
  });
  on('PUT', '/admin/settings/default-engine', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    const e = st.model.engines[body.engine];
    if (!e) return err(400, 'invalid_argument', '未知引擎', { fields: { engine: 'enum' } });
    if (!e.installed) return err(409, 'engine_unavailable', '当前部署未安装这个引擎', { engine: body.engine });
    st.model.default_engine = body.engine;
    return json(200, { default_engine: body.engine });
  });
  on('PUT', '/admin/settings/personal-key-policy', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    st.model.personal_key_allowed = !!body.allow;
    if (db.me?.modelSettings) db.me.modelSettings.personal_allowed = !!body.allow;
    return json(200, { allow: !!body.allow });
  });

  // ---------- SY-05～SY-08 上下文推荐表 ----------
  const presetRows = () => mergePresets(S.BUILTIN_PRESETS, st.customState === 'ok' ? st.custom : []);
  const presetsView = () => {
    const rows = presetRows();
    return {
      rows,
      builtin_verified_at: S.BUILTIN_VERIFIED_AT,
      custom: {
        version: st.customVersion,
        status: st.customState,
        error: st.customState === 'invalid' ? S.CUSTOM_ERROR : null,
        overrides: rows.filter((r) => r.origin === 'override').length,
        added: rows.filter((r) => r.origin === 'custom').length,
        verified_at: st.customState === 'disabled' ? null : st.customVerifiedAt,
        loaded_at: st.customState === 'ok' ? st.customLoadedAt : null,
        backup_available: st.backup,
      },
    };
  };
  on('GET', '/admin/settings/context-presets', ({ user }) => admin(user) || json(200, presetsView()));
  on('PUT', '/admin/settings/context-presets/custom', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (body.version !== st.customVersion) {
      return err(409, 'revision_conflict', '推荐表已被修改', { target: 'context-presets', expected: body.version, actual: st.customVersion });
    }
    const list = (body.presets || []).map((p) => ({ ...p, window: typeof p.window === 'number' ? p.window : parseIntText(p.window), max_output: typeof p.max_output === 'number' ? p.max_output : parseIntText(p.max_output) }));
    const bad = validatePresets(list.map((p) => ({ ...p, window: p.window ?? '', max_output: p.max_output ?? '' })));
    if (bad) return err(400, 'invalid_argument', '自定义表有误', { error: bad });
    // 别名、来源链接不传时沿用已存的同名条目（B2 第 10 条）
    const prev = new Map(st.custom.map((p) => [normalizeModelName(p.model), p]));
    st.custom = list.map((p) => {
      const old = prev.get(normalizeModelName(p.model)) || {};
      return { ...p, aliases: p.aliases ?? old.aliases ?? [], sources: p.sources ?? old.sources ?? [] };
    });
    st.customState = st.custom.length ? 'ok' : 'disabled';
    st.customVersion = `c-${uid()}`;
    st.customLoadedAt = new Date().toISOString();
    if (body.verified_at !== undefined) st.customVerifiedAt = body.verified_at;
    return json(200, presetsView());
  });
  on('POST', '/admin/settings/context-presets/custom/disable', ({ user }) => {
    const no = admin(user);
    if (no) return no;
    if (st.customState !== 'disabled') st.backup = true;
    st.custom = [];
    st.customState = 'disabled';
    st.customVersion = `c-${uid()}`;
    return json(200, presetsView());
  });
  on('GET', '/admin/settings/context-presets/match', ({ user, query }) => {
    const no = admin(user);
    if (no) return no;
    const model = query.get('model') || '';
    const r = matchPreset(presetRows(), model);
    return json(200, { model, preset: r ? { window: r.window, max_output: r.max_output, origin: r.origin } : null });
  });

  // ---------- SY-09 会话与权限 ----------
  on('GET', '/admin/settings/session', ({ user }) => admin(user) || json(200, st.session));
  on('PUT', '/admin/settings/session', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (!st.session.allowed.includes(body.permission_mode)) return err(400, 'invalid_argument', '权限模式不认识', { fields: { permission_mode: 'enum' } });
    st.session = { ...st.session, permission_mode: body.permission_mode };
    return json(200, st.session);
  });

  // ---------- SY-10、SY-11 脚本验证沙箱 ----------
  on('GET', '/admin/settings/sandbox', ({ user }) => admin(user) || json(200, S.sandbox({ connected: st.sandboxUp, p1 })));
  on('POST', '/admin/settings/sandbox/probe', async ({ user }) => {
    const no = admin(user);
    if (no) return no;
    await ctx.wait(500);
    return json(200, { ...S.sandbox({ connected: st.sandboxUp, p1 }), checked_at: new Date().toISOString() });
  });

  // ---------- SY-12、SY-13 全局 MCP 服务器 ----------
  const mcpProblem = (s) => {
    if (!s?.name || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(s.name)) return ['name', '名称只用字母、数字、- _ .'];
    if (!['http', 'stdio', 'sse'].includes(s.transport)) return ['transport', '传输方式不认识'];
    if (s.transport === 'stdio' ? !s.command : !/^https?:\/\/\S+$/i.test(s.url || '')) return [s.transport === 'stdio' ? 'command' : 'url', '地址或命令不对'];
    const kind = s.auth?.kind || 'none';
    if (kind !== 'none' && (p1 || kind !== 'bearer')) return ['auth.kind', '暂不支持为 MCP 服务器配置鉴权密钥'];
    return null;
  };
  on('GET', '/admin/settings/mcp-servers', ({ user }) => admin(user) || json(200, { items: st.mcp }));
  on('PUT', '/admin/settings/mcp-servers', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    const items = body.items || [];
    for (const s of items) {
      const bad = mcpProblem(s);
      if (bad) return err(400, 'invalid_argument', bad[1], { fields: { [bad[0]]: 'invalid' } });
    }
    if (new Set(items.map((s) => s.name)).size !== items.length) return err(400, 'invalid_argument', '服务器重名', { fields: { name: 'duplicate' } });
    const old = new Map(st.mcp.map((s) => [s.name, s]));
    st.mcp = items.map((s) => {
      const prev = old.get(s.name);
      const same = prev && prev.transport === s.transport && prev.url === s.url && prev.command === s.command;
      const keep = same && !p1 ? { status: prev.status, status_detail: prev.status_detail, tools: prev.tools, last_test_at: prev.last_test_at, latency_ms: prev.latency_ms }
        : { status: 'unknown', status_detail: null, tools: [], last_test_at: null, latency_ms: null };
      return { url: null, command: null, args: [], enabled: true, ...s, auth: s.auth || { kind: 'none' }, ...keep };
    });
    return json(200, { items: st.mcp });
  });
  on('POST', '/admin/settings/mcp-servers/test', async ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    const s = body.server || {};
    const bad = mcpProblem(s);
    if (bad) return err(400, 'invalid_argument', bad[1], { fields: { [bad[0]]: 'invalid' } });
    await ctx.wait(400);
    const failing = s.name === 'kb-search' || /unauthorized|401/.test(s.url || '');
    if (failing) return json(200, { ok: false, latency_ms: 63, tools: [], detail: '401 未授权', error: '401 未授权，请检查 Token' });
    const tools = p1 ? [] : S.MCP_TOOLS[s.name] || [{ name: 'ping', description: '连通性检查', read_only: true }];
    return json(200, { ok: true, latency_ms: s.transport === 'stdio' ? 140 : 86, tools, detail: p1 ? '端口可达（未做 MCP 握手）' : null, error: null });
  });

  // ---------- SY-14 配置注册表（admin；ops 只读可看） ----------
  on('GET', '/admin/config-registry', ({ user, query }) => {
    const no = forbid(user, ['admin', 'ops']);
    if (no) return no;
    const rows = presetRows();
    const extra = rows.filter((r) => r.origin !== 'builtin').length;
    const items = S.registryItems({
      stableGateway: st.model.engines.claude.base_url,
      permissionMode: st.session.permission_mode,
      presetsText: extra ? `内置 + 自定义 ${extra} 行` : '内置',
      maxRuns: st.limits.max_concurrent_runs,
      snapshotMb: st.quotas.per_project.snapshot_git_mb,
    });
    const effect = query.get('effect');
    const q = (query.get('q') || '').toLowerCase();
    if (effect && !['immediate', 'next_message', 'new_session', 'restart'].includes(effect)) return err(400, 'invalid_argument', 'effect 取值不合法', { fields: { effect: 'enum' } });
    return json(200, {
      precedence: ['env', 'data_dir', 'pack', 'default'],
      items: items.filter((it) => (!effect || it.effect === effect) && (!q || it.key.toLowerCase().includes(q) || it.description.toLowerCase().includes(q))),
    });
  });

  // ---------- SY-15～SY-17 配额与 L4 限制（sys:p1 时还没有） ----------
  on('GET', '/admin/quotas', ({ user }) => admin(user) || (p1 ? gone() : json(200, st.quotas)));
  on('PUT', '/admin/quotas', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (p1) return gone();
    const fields = {};
    const pos = (v) => Number.isInteger(v) && v >= 0;
    QUOTA_KEYS.user.forEach((k) => { if (!pos(body.per_user?.[k])) fields[`per_user.${k}`] = 'invalid'; });
    QUOTA_KEYS.project.forEach((k) => { if (!pos(body.per_project?.[k])) fields[`per_project.${k}`] = 'invalid'; });
    if (Object.keys(fields).length) return err(400, 'invalid_argument', '配额要填非负整数', { fields });
    st.quotas = {
      ...st.quotas,
      per_user: { ...st.quotas.per_user, ...body.per_user },
      per_project: { ...st.quotas.per_project, ...body.per_project, online_engines: 1 },
    };
    return json(200, st.quotas);
  });
  on('POST', '/admin/quotas/overrides', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (p1) return gone();
    const kind = body.subject?.kind;
    const id = body.subject?.id;
    if (!QUOTA_KEYS[kind]) return err(400, 'invalid_argument', '对象只能是用户或项目', { fields: { 'subject.kind': 'enum' } });
    if (!QUOTA_KEYS[kind].includes(body.item)) return err(400, 'invalid_argument', '这一项不能单独设置', { fields: { item: 'enum' } });
    if (!Number.isInteger(body.value) || body.value < 0) return err(400, 'invalid_argument', '值要是非负整数', { fields: { value: 'invalid' } });
    if (!String(body.reason || '').trim()) return err(400, 'invalid_argument', '写一下原因', { fields: { reason: 'required' } });
    let label;
    if (kind === 'user') {
      const u = PLATFORM_USERS.find((x) => x.username === id);
      if (!u) return err(404, 'user_not_found', '没有这个用户', { username: id });
      label = u.display_name;
    } else {
      const p = platformProjects().find((x) => x.id === id);
      if (!p) return err(404, 'project_not_found', '没有这个项目', { project_id: id });
      label = p.name;
    }
    if (st.quotas.overrides.some((o) => o.subject.kind === kind && o.subject.id === id && o.item === body.item)) {
      return err(409, 'conflict', '这个对象已经有这一项的单独设置', { item: body.item });
    }
    const o = { id: `qo-${uid()}`, subject: { kind, id, label }, item: body.item, value: body.value, reason: body.reason.trim(), created_at: new Date().toISOString(), created_by: user.username };
    st.quotas = { ...st.quotas, overrides: [...st.quotas.overrides, o] };
    return json(201, o);
  });
  on('DELETE', '/admin/quotas/overrides/([^/]+)', ({ m, user }) => {
    const no = admin(user);
    if (no) return no;
    if (p1) return gone();
    if (!st.quotas.overrides.some((o) => o.id === m[1])) return err(404, 'not_found', '没有这条单独设置');
    st.quotas = { ...st.quotas, overrides: st.quotas.overrides.filter((o) => o.id !== m[1]) };
    return new Response(null, { status: 204 });
  });
  on('GET', '/admin/autobuild-limits', ({ user }) => admin(user) || (noLimits ? gone() : json(200, st.limits)));
  on('PUT', '/admin/autobuild-limits', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (noLimits) return gone();
    const r = body.max_concurrent_runs;
    if (Number.isInteger(r) && r < 1) return err(422, 'invalid_argument', '同时运行上限至少是 1', { fields: { max_concurrent_runs: 'min' }, min: 1 });
    if (!Number.isInteger(r) || r > 16) return err(400, 'invalid_argument', '同时运行上限要在 1–16 之间', { fields: { max_concurrent_runs: 'range' } });
    if (!(body.max_duration_min > 0) || !(body.max_tokens > 0) || !(body.max_cost?.amount > 0)) {
      return err(400, 'invalid_argument', '单次上限要大于 0', { fields: { budget: 'invalid' } });
    }
    st.limits = { ...st.limits, ...body, running: st.limits.running };
    return json(200, st.limits);
  });

  // ---------- SY-18 模型单价（设计稿没画入口；sys:p1 时还没有） ----------
  on('GET', '/admin/settings/pricing', ({ user }) => admin(user) || (p1 ? gone() : json(200, st.pricing)));
  on('PUT', '/admin/settings/pricing', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (p1) return gone();
    if (!['CNY', 'USD'].includes(body.currency)) return err(400, 'invalid_argument', '币种只能是 CNY 或 USD', { fields: { currency: 'enum' } });
    const items = body.items || [];
    const badAt = items.findIndex((it) => !ENGINES.includes(it.engine) || !String(it.model || '').trim()
      || [it.input_per_mtok, it.output_per_mtok].some((v) => typeof v !== 'number' || v < 0)
      || (it.cache_read_per_mtok != null && (typeof it.cache_read_per_mtok !== 'number' || it.cache_read_per_mtok < 0)));
    if (badAt >= 0) return err(400, 'invalid_argument', `第 ${badAt + 1} 行单价不对`, { fields: { [`items.${badAt}`]: 'invalid' } });
    st.pricing = { currency: body.currency, items: items.map((it) => ({ ...it, model: it.model.trim(), cache_read_per_mtok: it.cache_read_per_mtok ?? null })) };
    return json(200, st.pricing);
  });

  // ---------- X-06 服务令牌（设计稿没画入口；sys:p1 时还没有） ----------
  const SCOPES = ['releases.read', 'receipts.write', 'feedback.write'];
  on('GET', '/admin/service-tokens', ({ user }) => admin(user) || (p1 ? gone() : json(200, { items: st.tokens })));
  on('POST', '/admin/service-tokens', ({ user, body }) => {
    const no = admin(user);
    if (no) return no;
    if (p1) return gone();
    const name = String(body.name || '').trim();
    if (!name || name.length > 120) return err(400, 'invalid_argument', '名称不能为空，不超过 120 个字', { fields: { name: 'invalid' } });
    if (!body.scopes?.length || body.scopes.some((s) => !SCOPES.includes(s))) return err(400, 'invalid_argument', '权限范围不对', { fields: { scopes: 'invalid' } });
    const ids = body.project_ids ?? null;
    if (ids && (!ids.length || ids.some((id) => !db.cards.some((c) => c.id === id)))) return err(400, 'invalid_argument', '项目不存在', { fields: { project_ids: 'invalid' } });
    st.tokenSeq += 1;
    const now = Date.now();
    const summary = {
      id: `svc-${uid()}`, name, scopes: body.scopes, project_ids: ids, created_at: new Date(now).toISOString(), created_by: user.username,
      expires_at: body.expires_in_s ? new Date(now + body.expires_in_s * 1000).toISOString() : null, last_used_at: null,
    };
    st.tokens = [summary, ...st.tokens];
    return json(201, { token: S.placeholderServiceToken(st.tokenSeq), summary });
  });
  on('DELETE', '/admin/service-tokens/([^/]+)', ({ m, user }) => {
    const no = admin(user);
    if (no) return no;
    if (p1) return gone();
    if (!st.tokens.some((t) => t.id === m[1])) return err(404, 'not_found', '没有这个服务令牌');
    st.tokens = st.tokens.filter((t) => t.id !== m[1]);
    return new Response(null, { status: 204 });
  });
}
