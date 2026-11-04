// mock 服务端：按契约路径响应，状态放在内存里；SSE 由 StreamHub 推送。
// 只在 VITE_API_MOCK=1 的开发 / 测试构建里加载。
// 演示状态用地址参数切换：?mock=login:fail,projects:empty,auth:local,maint:readonly,engine:missing,import:conflict,role:user,scope:running|pending
// ?mock=evidence:off：证据源按本期后端不可用（EV-01 回 501、没有系统逆向建模包）
// ?mock=flow:p1：/flow 按 P1 后端（B3a）的形状返回（4 种状态、命令文本 actions、没有分段），核对前端归一处理
// 项目所处阶段（工作台显示什么）：?mock=stage:materials 等，见 stageOf；各阶段的对话、流程由 routes-*.js 的 stages 提供。
// F2、F3 各页的接口按分组放在 routes-*.js，每个文件导出 register(ctx)、可选的 stages、projectStages、lockScope、taskSeeds。
import * as F from './fixtures.js';
import { StreamHub } from './hub.js';
import { subagentTask } from './subagent-data.js';
import * as artifactRoutes from './routes-artifacts.js';
import * as autobuildRoutes from './routes-autobuild.js';
import * as inboxRoutes from './routes-inbox.js';
import * as materialRoutes from './routes-materials.js';
import * as reportRoutes from './routes-reports.js';
import * as studioRoutes from './routes-studio.js';
import * as evidenceRoutes from './routes-evidence.js';
import * as incrementalRoutes from './routes-incremental.js';
import * as knowledgeRoutes from './routes-knowledge.js';
import * as publishRoutes from './routes-publish.js';
import * as resourceRoutes from './routes-resources.js';
import * as scenarioRoutes from './routes-scenarios.js';
import * as scriptRoutes from './routes-scripts.js';
import * as settingsRoutes from './routes-settings.js';
import * as versionRoutes from './routes-versions.js';
import * as opsObsRoutes from './routes-ops-obs.js';
import * as opsCtlRoutes from './routes-ops-ctl.js';
import * as adminRoutes from './routes-admin.js';
import * as pluginRoutes from './routes-plugins.js';
import * as packUploadRoutes from './routes-pack-uploads.js';
import * as meRoutes from './routes-me.js';
import * as systemRoutes from './routes-system.js';

// F3 的分组在后面：同一路径先登记的先匹配，F3 文件只补新接口
// F4：运维（可观测、管控、用户与审计）、插件目录与上传、个人设置、系统设置；/me、/packs 已从本文件移到 routes-me、routes-plugins
const MODULES = [
  studioRoutes, reportRoutes, artifactRoutes, inboxRoutes, autobuildRoutes, materialRoutes,
  knowledgeRoutes, scenarioRoutes, scriptRoutes, incrementalRoutes, versionRoutes, publishRoutes, resourceRoutes, evidenceRoutes, settingsRoutes,
  opsObsRoutes, opsCtlRoutes, adminRoutes, pluginRoutes, packUploadRoutes, meRoutes, systemRoutes,
];
// 写入范围锁额外覆盖的条目、产物、路径（routes-*.js 导出 lockScope）
const LOCK_EXTRAS = { entries: [], artifacts: [], paths: [] };
MODULES.forEach((mod) => ['entries', 'artifacts', 'paths'].forEach((k) => LOCK_EXTRAS[k].push(...(mod.lockScope?.[k] || []))));
// ?mock=task:<页面 id> 时该页一打开就有的进行中任务（routes-*.js 导出 taskSeeds = { <页面 id>: () => CommandTask 部分字段 }）
const TASK_SEEDS = Object.assign({}, ...MODULES.map((mod) => mod.taskSeeds || {}));

function readScenario() {
  const out = {};
  try {
    const raw = new URLSearchParams(window.location.search).get('mock') || window.localStorage.getItem('onto.mock') || '';
    raw.split(',').filter(Boolean).forEach((kv) => {
      const [k, v] = kv.split(':');
      out[k] = v ?? '1';
    });
  } catch { /* 非浏览器环境 */ }
  return out;
}

const json = (status, body) => new Response(body == null ? null : JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});
const err = (status, code, message, details = null) => json(status, { code, message, details, trace_id: `mock${Math.random().toString(16).slice(2, 8)}` });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const PURPOSE_BY_EXT = { xlsx: 'schema', xls: 'schema', xlsm: 'schema', csv: 'schema' };
function detectPurpose(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (PURPOSE_BY_EXT[ext]) return 'schema';
  if (/场景说明|目标/.test(name)) return 'goal';
  if (/术语/.test(name)) return 'term';
  return 'evidence';
}

export function createMockServer() {
  const sc = readScenario();
  const hub = new StreamHub();
  const authEnabled = sc.auth !== 'local';
  const maintenance = sc.maint === 'readonly' ? F.maintenanceState('read_only') : sc.maint === 'reject' ? F.maintenanceState('reject_new_sessions') : null;
  const db = {
    cards: sc.projects === 'empty' ? [] : F.projectCards(),
    notifications: F.notifications(),
    uploads: new Map(),
    convs: new Map(),
    changesets: new Map(),
    scope: new Map(),
    flows: new Map(),
    running: new Map(),
    steer: new Map(), // 运行中插的话，还没被助手看到：`${pid}:${cid}` → [{ message_id, text, timer }]
    next: new Map(), // 平台代存的下一条命令：`${pid}:${cid}` → { id, label, command, text, origin, created_at, paused, body }
    tasks: new Map(),
    seeded: new Set(),
    seq: 2000,
  };
  const uid = () => (++db.seq).toString(16);

  function userOf(init) {
    if (!authEnabled) return { ...F.USERS.local, roles: ['user', 'admin', 'ops'] };
    const h = init?.headers?.Authorization || '';
    const name = h.startsWith('Bearer mock.') ? h.slice('Bearer mock.'.length).split('.')[0] : null;
    if (!name) return null;
    const role = sc.role && name === 'zhangsan' ? sc.role : null;
    const base = F.USERS[name] || { username: name, display_name: name, role: 'user', role_label: '建模者', auth_source: 'auth-server' };
    const u = role ? { ...base, role, role_label: { user: '建模者', admin: '管理员', ops: '运维' }[role] } : base;
    return { ...u, roles: [...new Set(['user', u.role])] };
  }
  const tokensFor = (user) => ({
    access_token: `mock.${user.username}.${Date.now()}`,
    refresh_token: `mock-refresh.${user.username}`,
    token_type: 'Bearer',
    expires_in: 3600,
    user,
  });

  const cardOf = (id) => db.cards.find((c) => c.id === id);
  const counts = () => {
    const live = db.cards.filter((c) => !c.archived);
    return {
      all: live.length,
      active: live.filter((c) => !c.status.includes('publishable')).length,
      autobuild: live.filter((c) => c.status.includes('autobuild')).length,
      publishable: live.filter((c) => c.status.includes('publishable')).length,
      archived: db.cards.filter((c) => c.archived).length,
    };
  };
  const nameTaken = (name, exclude) => {
    const n = name.trim().normalize('NFKC').toLowerCase();
    const cards = db.cards.filter((c) => c.id !== exclude).map((c) => c.name.normalize('NFKC').toLowerCase());
    // 新建向导用主示例项目名演示可用名称，mock 里不拿它判重
    if (!exclude && n === '门店补货单编制' && sc.import !== 'conflict') return false;
    return cards.includes(n);
  };
  const candidates = (name) => [2, 3, 4].map((i) => `${name} (${i})`).filter((x) => !nameTaken(x)).slice(0, 3);
  const packageNameOf = (name) => F.KNOWN_PACKAGE_NAMES[name] || `kp-${Math.abs([...name].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) | 0, 7)).toString(36).slice(0, 6)}`;
  function nameCheck(name, exclude) {
    const trimmed = (name || '').trim();
    if (!trimmed) return { available: false, normalized: '', package_name: '', conflict: null, reason: 'empty' };
    if (trimmed.length > 200) return { available: false, normalized: trimmed, package_name: '', conflict: null, reason: 'too_long' };
    if (nameTaken(trimmed, exclude)) {
      return { available: false, normalized: trimmed, package_name: packageNameOf(trimmed), conflict: { original_name: trimmed, attempted_name: trimmed, candidates: candidates(trimmed) }, reason: 'conflict' };
    }
    return { available: true, normalized: trimmed, package_name: packageNameOf(trimmed), conflict: null, reason: null };
  }

  // ---------- 项目内状态 ----------
  const convKey = (pid, cid) => `conv:${pid}:${cid}`;
  const projKey = (pid) => `proj:${pid}`;
  // 阶段：l2（陪伴建模中，wb-l2 那一屏）是主示例项目的默认；其余阶段由 routes-*.js 登记
  // 每个阶段：{ conversations?: () => Conversation[], history?: () => Event[]（第一条对话的历史）, flow?: (base) => Flow }
  const STAGES = { l2: { conversations: F.conversations, history: F.wbHistory } };
  const PROJECT_STAGES = { [F.MAIN_PID]: 'l2' };
  MODULES.forEach((mod) => { Object.assign(STAGES, mod.stages || {}); Object.assign(PROJECT_STAGES, mod.projectStages || {}); });
  const stageOf = (pid) => (pid === F.MAIN_PID && sc.stage && STAGES[sc.stage] ? sc.stage : PROJECT_STAGES[pid] || null);
  function convsOf(pid) {
    if (!db.convs.has(pid)) {
      const stage = STAGES[stageOf(pid)];
      const list = stage?.conversations ? stage.conversations() : [{ ...F.conversations()[3], id: `c${pid.slice(0, 11)}`, title: '默认会话', live: true, state: 'idle' }];
      db.convs.set(pid, list);
      list.forEach((c, i) => {
        if (stage?.history && i === 0) hub.seed(convKey(pid, c.id), 'conversation', stage.history());
        else {
          const card = cardOf(pid);
          hub.seed(convKey(pid, c.id), 'conversation', [
            { id: '1', type: 'day.marker', ts: c.created_at, data: { label: '今天' } },
            { id: '2', type: 'message.assistant', ts: c.created_at, data: { message_id: `m-${c.id}`, text_md: `你好，我是建模助手。目标我记下了：**${card?.goal || ''}**。`, anchors: [], entry_links: [], continued: false } },
          ]);
        }
      });
    }
    return db.convs.get(pid);
  }
  function flowOf(pid) {
    if (!db.flows.has(pid) && sc.flow === 'p1') db.flows.set(pid, F.flowP1());
    if (!db.flows.has(pid)) {
      const base = F.flow({ trace: true });
      const stage = STAGES[stageOf(pid)];
      db.flows.set(pid, stage?.flow ? stage.flow(base) : base);
    }
    return db.flows.get(pid);
  }
  const emitConv = (pid, cid, type, data) => {
    const ch = hub.channel(convKey(pid, cid), 'conversation');
    return hub.emit(convKey(pid, cid), 'conversation', { type, project_id: pid, conversation_id: cid, data }, { seq: ch.n + 1 });
  };
  const emitConvTransient = (pid, cid, type, data) => hub.emit(convKey(pid, cid), 'conversation', { type, project_id: pid, conversation_id: cid, data }, { transient: true });
  const emitProj = (pid, type, data) => hub.emit(projKey(pid), 'project', { type, project_id: pid, data });
  const emitUser = (type, data) => hub.emit('user', 'user', { type, data });
  const setSession = (pid, cid, state) => {
    const list = convsOf(pid);
    const c = list.find((x) => x.id === cid);
    if (c) c.state = state;
    emitConv(pid, cid, 'session.state', { state, since: new Date().toISOString(), turn_started_at: state === 'running' ? new Date().toISOString() : null, reason: null });
    // 这一轮结束：代存的命令（没被打断暂停的）接着开始
    const nx = db.next.get(`${pid}:${cid}`);
    if (state === 'idle' && nx && !nx.paused) setTimeout(() => startNext(pid, cid), 0);
  };
  const setNext = (pid, cid, next) => {
    if (next) db.next.set(`${pid}:${cid}`, next); else db.next.delete(`${pid}:${cid}`);
    const { body: _b, ...view } = next || {};
    emitConv(pid, cid, 'next.updated', { next: next ? view : null });
  };
  function startNext(pid, cid) {
    const nx = db.next.get(`${pid}:${cid}`);
    if (!nx) return;
    setNext(pid, cid, null);
    runCommand(pid, cid, nx.body);
  }
  const scopeOf = (pid) => db.scope.get(pid) || { state: 'none', changeset_id: null, conversation_id: null, run_id: null, slice: null, node: null, entries: [], artifacts: [], paths: [], reason_text: '', since: null };
  const setScope = (pid, lock) => { db.scope.set(pid, lock); emitProj(pid, 'scope.updated', lock); };
  const scopeLock = (state, cid) => ({
    state, changeset_id: F.CHANGESET_ID, conversation_id: cid, run_id: null, slice: { id: 's5', name: '超量与审批提示' }, node: { id: 'core.model', version: 'core@1.4.2' },
    // 各页 mock 导出的 lockScope（场景、求解契约等插件产物）一起并进来
    entries: [F.REFS.op, F.REFS.r09, F.REFS.safety, F.REFS.display, ...LOCK_EXTRAS.entries],
    artifacts: [{ type: 'diagram.flowchart', key: 'overage-flow', title: '超量处理流程' }, ...LOCK_EXTRAS.artifacts],
    paths: ['ext/trace/scenarios/**', ...LOCK_EXTRAS.paths], reason_text: '助手正在改「超量与审批提示」这一段', since: new Date().toISOString(),
  });
  // ?mock=scope:running / scope:pending：主示例项目一打开就处在写入范围锁的两态之一（Studio、流程图弹窗、插件页的只读对照用）
  if (sc.scope === 'running' || sc.scope === 'pending') {
    db.scope.set(F.MAIN_PID, scopeLock(sc.scope === 'running' ? 'assistant_running' : 'pending_confirm', F.conversations()[0].id));
  }

  function later(pid, cid, ms, fn) {
    const t = setTimeout(() => { db.running.delete(`${pid}:${cid}`); fn(); }, ms);
    db.running.set(`${pid}:${cid}`, t);
  }

  // 回答决策卡后：助手按方向修改 → 本轮改动卡 → 修改待确认
  function runDecision(pid, cid, opt, customText) {
    const q = F.decisionQuestion();
    const o = q.options.find((x) => x.id === opt) || q.options[0];
    const label = o.custom && customText ? customText : o.label;
    emitConv(pid, cid, 'question.answered', { question_id: q.question_id, answer: { option_id: o.id, custom_text: customText || undefined }, by: 'user', summary: `步骤 2 选了 ${o.key}：${label}`, answered_at: new Date().toISOString() });
    setSession(pid, cid, 'running');
    setScope(pid, scopeLock('assistant_running', cid));
    emitConv(pid, cid, 'progress', {
      group_id: 'pg-44', title: '正在修改', desc: `按方向 ${o.key} 改步骤 2`, state: 'running', started_at: new Date().toISOString(), duration_ms: null, collapsed: false,
      steps: [{ id: 's1', text: `按方向 ${o.key} 修改步骤 2，引用规则 R-09`, meta: '进行中', state: 'running', duration_ms: null }],
    });
    emitConv(pid, cid, 'suggestions.updated', { mode: 'normal', primary: null, secondary: [], all_commands: true });
    later(pid, cid, 1400, () => {
      emitConv(pid, cid, 'progress.updated', {
        group_id: 'pg-44', title: '已完成 3 步', desc: '修改步骤 · 写入即查 · 场景推演', state: 'done', started_at: new Date().toISOString(), duration_ms: 34000, collapsed: true,
        steps: [
          { id: 's1', text: `按方向 ${o.key} 修改步骤 2，引用规则 R-09`, meta: '12 秒', state: 'done', duration_ms: 12000 },
          { id: 's2', text: '写入即查：0 个错误', meta: '2 秒', state: 'done', duration_ms: 2000 },
          { id: 's3', text: '从样例补货单 A、B 生成 2 个验收场景，推演 2 / 2 通过', meta: '20 秒', state: 'done', duration_ms: 20000 },
        ],
      });
      // 一轮结束就合入成版本 v0.13（F3），卡片只说改了什么，建议条直接给下一步
      db.changesets.set(F.CHANGESET_ID, { opt: o.key, state: 'merged', pid, cid });
      emitConv(pid, cid, 'card', F.roundChangesCard(o.key));
      emitConv(pid, cid, 'card.updated', F.stepOutputCard(true));
      emitConv(pid, cid, 'turn.completed', { turn_id: `t-${uid()}`, duration_ms: 34000, is_error: false, usage: { input_tokens: 18400, output_tokens: 1200, cache_read_tokens: 0 }, cost: { amount: 0.42, currency: 'CNY' } });
      emitConvTransient(pid, cid, 'context.usage', { used: 88000, window: 200000, pct: 44, compact_at_pct: 80, breakdown: null, last_call: { input: 18400, output: 1200, estimated: true } });
      setSession(pid, cid, 'idle');
      setScope(pid, { ...scopeOf(pid), state: 'none', entries: [], artifacts: [], paths: [] });
      const c = cardOf(pid);
      if (c) c.version_label = 'v0.13';
      emitProj(pid, 'changeset.updated', { changeset_id: F.CHANGESET_ID, state: 'merged', source: 'assistant', conversation_id: cid, version_label: 'v0.13' });
      emitProj(pid, 'workspace.changed', { commit: 'c0ffee13', version_label: 'v0.13', source: 'assistant', changeset_id: F.CHANGESET_ID, by: 'zhangsan', entries: [F.REFS.op], artifacts: [], paths: [] });
      if (c) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(c) });
      emitConv(pid, cid, 'suggestions.updated', F.suggestionsAfterMerge());
    });
  }

  function runReply(pid, cid, text) {
    setSession(pid, cid, 'running');
    const gid = `pg-${uid()}`;
    emitConv(pid, cid, 'progress', { group_id: gid, title: '', desc: '', state: 'running', started_at: new Date().toISOString(), duration_ms: null, collapsed: false, steps: [{ id: 's1', text: '理解你的补充说明', meta: '进行中', state: 'running', duration_ms: null }] });
    later(pid, cid, sc.reply === 'slow' ? 90000 : 1200, () => {
      emitConv(pid, cid, 'progress.updated', { group_id: gid, title: '', desc: '', state: 'done', started_at: new Date().toISOString(), duration_ms: 3000, collapsed: false, steps: [{ id: 's1', text: '理解你的补充说明', meta: '3 秒', state: 'done', duration_ms: 3000 }] });
      emitConv(pid, cid, 'message.assistant', { message_id: `m-${uid()}`, text_md: `收到：「${text.slice(0, 40)}」。我按这个说明接着做。`, anchors: [], entry_links: [], continued: false });
      setSession(pid, cid, 'idle');
    });
  }

  const routes = [];
  const commandHooks = [];
  // AR-01 的产物类型：图示包的流程图之外，F3 各页的 mock 往这里登记（如场景走查的 trace.scenario、trace.mock）
  const artifactTypeDefs = [];
  const on = (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler });

  // ---------- 3.1 / 3.2 ----------
  on('GET', '/client-config', () => json(200, F.clientConfig({ authEnabled, registration: sc.login !== 'closed', maintenance })));
  on('GET', '/engines', () => json(200, F.engines({ betaInstalled: sc.engine !== 'missing' })));
  on('POST', '/auth/login', async ({ body }) => {
    await wait(250);
    if (sc.login === 'rate') return err(429, 'rate_limited', '请求过于频繁', { retry_after_s: 30 });
    if (sc.login === 'fail' || body.password === 'wrong') return err(401, 'invalid_credentials', '用户名或密码不正确', { remaining_attempts: 4 });
    const user = F.USERS[body.username] || { username: body.username, display_name: body.username, role: 'user', role_label: '建模者', auth_source: 'auth-server' };
    return json(200, tokensFor(user));
  });
  on('POST', '/auth/local', () => (authEnabled ? err(404, 'not_found', '路径不存在') : json(200, { user: F.USERS.local })));
  on('POST', '/auth/register', async ({ body }) => {
    await wait(250);
    if (sc.login === 'closed') return err(403, 'registration_disabled', '注册未开启');
    if (sc.login === 'rate') return err(429, 'rate_limited', '请求过于频繁', { retry_after_s: 30 });
    if (F.USERS[body.username]) return err(409, 'username_taken', '用户名已存在');
    return json(201, tokensFor({ username: body.username, display_name: body.username, role: 'user', role_label: '建模者', auth_source: 'auth-server' }));
  });
  on('POST', '/auth/refresh', ({ body }) => {
    const name = String(body.refresh_token || '').replace('mock-refresh.', '');
    if (!name) return err(401, 'refresh_invalid', '刷新令牌无效');
    return json(200, tokensFor(F.USERS[name] || { username: name, display_name: name, role: 'user', role_label: '建模者', auth_source: 'auth-server' }));
  });
  on('POST', '/auth/logout', () => new Response(null, { status: 204 }));
  on('GET', '/me/notifications', () => json(200, { items: db.notifications, next_cursor: null, unread_count: db.notifications.filter((n) => !n.read).length }));
  on('POST', '/me/notifications/read', ({ body }) => {
    db.notifications.forEach((n) => { if (body.all || (body.ids || []).includes(n.id)) n.read = true; });
    return json(200, { unread_count: db.notifications.filter((n) => !n.read).length });
  });
  on('GET', '/me/events', ({ query }) => hub.open('user', 'user', query));

  // ---------- 3.3 上传 ----------
  on('POST', '/uploads', async ({ form }) => {
    const file = form.get('file');
    const purpose = form.get('purpose');
    if (!file) return err(400, 'invalid_argument', '缺少文件', { fields: { file: 'required' } });
    const limit = purpose === 'project_import' ? 200 * 1024 * 1024 : 20 * 1024 * 1024;
    if (file.size > limit) return err(413, 'payload_too_large', '文件超过上限', { limit_bytes: limit });
    const id = `up-${uid()}`;
    const rec = {
      upload_id: id, filename: file.name, size: file.size, content_type: file.type || 'application/octet-stream', sha256: 'f'.repeat(64),
      expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
      detected: purpose === 'material' ? { purpose: detectPurpose(file.name), tables: [] } : null,
    };
    db.uploads.set(id, rec);
    return json(201, rec);
  });
  on('DELETE', '/uploads/([^/]+)', ({ m }) => { db.uploads.delete(m[1]); return new Response(null, { status: 204 }); });

  // ---------- 3.4 项目 ----------
  on('GET', '/projects', ({ query }) => {
    const status = query.get('status') || 'all';
    const q = (query.get('q') || '').toLowerCase();
    let items = db.cards.filter((c) => (status === 'archived' ? c.archived : !c.archived));
    if (status === 'active') items = items.filter((c) => !c.status.includes('publishable'));
    if (status === 'autobuild') items = items.filter((c) => c.status.includes('autobuild'));
    if (status === 'publishable') items = items.filter((c) => c.status.includes('publishable'));
    if (q) items = items.filter((c) => `${c.name}${c.goal}`.toLowerCase().includes(q));
    const sort = query.get('sort') || 'recent';
    items = [...items].sort((a, b) => (sort === 'name' ? a.name.localeCompare(b.name, 'zh') : sort === 'created' ? b.created_at.localeCompare(a.created_at) : 0));
    return json(200, { items, next_cursor: null, counts: counts() });
  });
  on('GET', '/projects/name-check', ({ query }) => json(200, nameCheck(query.get('name'), query.get('exclude_project_id'))));
  on('POST', '/projects', async ({ body }) => {
    await wait(300);
    const chk = nameCheck(body.name);
    if (chk.reason === 'empty') return err(400, 'project_name_required', '名称为空');
    if (chk.reason === 'conflict') return err(409, 'project_name_conflict', '同名项目已存在', { conflict: chk.conflict });
    const id = `${chk.package_name}-${uid().padStart(8, '0')}`.slice(0, 63);
    const c = {
      ...F.projectCards()[4], id, name: chk.normalized, package_name: chk.package_name, goal: body.goal || '', build_mode: body.build_mode || 'L2',
      version_label: 'v0.1', last_activity_at: new Date().toISOString(), inbox_count: 0, industry_label: null, created_at: new Date().toISOString(),
    };
    db.cards.unshift(c);
    return json(201, { ...F.projectMeta(c), landing_page: 'wb-materials' });
  });
  on('POST', '/projects/import/inspect', async () => {
    await wait(300);
    return json(200, F.importInspection(sc.import === 'conflict' ? 'conflict' : sc.import === 'bad' ? 'bad' : 'published'));
  });
  on('POST', '/projects/import', async ({ body }) => {
    await wait(400);
    if (sc.import === 'bad') return err(400, 'unsupported_archive_kind', '认不出格式', { blockers: F.importInspection('bad').blockers });
    let name = body.name;
    const chk = nameCheck(name);
    if (chk.reason === 'conflict') {
      if (body.name_strategy !== 'suggested') return err(409, 'project_name_conflict', '同名项目已存在', { conflict: chk.conflict });
      name = chk.conflict.candidates[0];
    }
    const c = { ...F.projectCards()[0], id: `retail-restock-${uid().padStart(8, '0')}`, name, build_mode: body.build_mode || 'L2', created_from: 'import', last_activity_at: new Date().toISOString() };
    db.cards.unshift(c);
    return json(201, { ...F.projectMeta(c), landing_page: 'wb-l2' });
  });
  on('GET', '/projects/([^/]+)', ({ m, user }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    if (sc.owner === 'other' && user.role !== 'user') {
      return json(200, { id: c.id, name: c.name, owner: 'modeler01', stage_label: '建模 3/5 段', build_mode: c.build_mode, archived: c.archived, created_at: c.created_at, last_activity_at: c.last_activity_at });
    }
    return json(200, F.projectMeta(c));
  });
  on('PATCH', '/projects/([^/]+)', ({ m, body }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    if (body.name !== undefined) {
      const chk = nameCheck(body.name, c.id);
      if (chk.reason === 'empty') return err(400, 'project_name_required', '名称为空');
      if (chk.reason === 'conflict') return err(409, 'project_name_conflict', '同名项目已存在', { conflict: chk.conflict });
      c.name = chk.normalized;
    }
    if (typeof body.goal === 'string') c.goal = body.goal.trim();
    emitUser('project.card.updated', { project_id: c.id, card: c });
    return json(200, F.projectMeta(c));
  });
  on('DELETE', '/projects/([^/]+)', ({ m, body }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    if (body.confirm_name !== c.name) return err(400, 'confirm_name_mismatch', '名称不一致');
    db.cards = db.cards.filter((x) => x.id !== c.id);
    emitUser('project.removed', { project_id: c.id, reason: 'deleted' });
    return json(202, { deleted: c.id });
  });
  on('POST', '/projects/([^/]+)/archive', ({ m, body }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    if (body.archived && c.status.includes('autobuild')) return err(409, 'autobuild_running', '自主构建运行中', { run_id: c.autobuild?.run_id });
    c.archived = !!body.archived;
    c.archived_at = c.archived ? new Date().toISOString() : null;
    c.status = c.archived ? ['archived'] : ['active'];
    emitUser('project.card.updated', { project_id: c.id, card: c });
    return json(200, F.projectMeta(c));
  });
  on('POST', '/projects/([^/]+)/clone', ({ m, body }) => {
    const c = cardOf(m[1]);
    if (!c) return err(404, 'project_not_found', '项目不存在');
    const name = body.name || `${c.name}（副本）`;
    const chk = nameCheck(name);
    if (chk.reason === 'conflict') return err(409, 'project_name_conflict', '同名项目已存在', { conflict: chk.conflict });
    const n = { ...c, id: `${c.package_name}-${uid().padStart(8, '0')}`, name, created_from: 'clone', last_activity_at: new Date().toISOString(), inbox_count: 0 };
    db.cards.unshift(n);
    return json(201, F.projectMeta(n));
  });
  on('GET', '/projects/([^/]+)/transfer-package', ({ m }) => new Response(new Blob(['PK\u0005\u0006'.padEnd(22, '\u0000')]), {
    status: 200, headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${encodeURIComponent(cardOf(m[1])?.name || 'project')}-transfer.zip"` },
  }));
  on('GET', '/projects/([^/]+)/build-mode', ({ m }) => json(200, { build_mode: cardOf(m[1])?.build_mode || 'L2', autobuild_running: false, run_id: null }));
  on('PUT', '/projects/([^/]+)/build-mode', ({ m, body }) => { const c = cardOf(m[1]); if (c) c.build_mode = body.build_mode; return json(200, { build_mode: body.build_mode, autobuild_running: false, run_id: null }); });
  on('GET', '/projects/([^/]+)/events', ({ m, query }) => hub.open(projKey(m[1]), 'project', query));
  on('GET', '/projects/([^/]+)/usage', () => json(200, { calls: 214, input_tokens: 3200000, output_tokens: 410000, cost: { amount: 126, currency: 'CNY' }, last_activity_at: new Date().toISOString() }));
  // 扩展项随本项目启用的能力变（routes-settings.js 登记 db.extensionsOf，启用、停用后左栏跟着变）
  on('GET', '/projects/([^/]+)/extensions', ({ m }) => json(200, db.extensionsOf ? db.extensionsOf(m[1]) : F.extensions()));
  on('GET', '/projects/([^/]+)/flow', ({ m }) => json(200, flowOf(m[1])));
  on('GET', '/projects/([^/]+)/flow/suggestions', () => json(200, F.suggestionsWaiting()));
  on('POST', '/projects/([^/]+)/flow/nodes/([^/]+)/(skip|restore)', ({ m }) => {
    const f = flowOf(m[1]);
    const n = f.nodes.find((x) => x.id === m[2]);
    if (!n) return err(404, 'node_not_found', '节点不存在');
    if (m[3] === 'skip') {
      if (n.section === 'required') return err(409, 'node_not_skippable', '主线不能跳过');
      Object.assign(n, { state: 'skipped', display_state: 'skipped', actions: [{ kind: 'restore', label: '恢复' }] });
    } else {
      if (n.state !== 'skipped') return err(409, 'node_not_skipped', '没有被跳过');
      Object.assign(n, { state: 'ready', display_state: 'ready', actions: [{ kind: 'start', label: '开始' }, { kind: 'skip', label: '跳过' }] });
    }
    emitProj(m[1], 'flow.updated', { flow_hash: f.flow_hash, nodes: [{ id: n.id, state: n.state, flags: n.flags, progress: n.progress }], slices: [], suggestions_changed: false });
    return json(200, { node: n, flow_hash: f.flow_hash });
  });
  on('POST', '/projects/([^/]+)/flow/nodes/([^/]+)/start', async ({ m, body }) => {
    // 脚本节点（B5c）：同步跑完回 run，不发对话（契约 B5c 第 10 条）；建模步骤抽屉里展开看结果（R24）
    const f = flowOf(m[1]);
    const sn = f.nodes.find((x) => x.id === m[2] && x.kind === 'script');
    if (sn) {
      await wait(600);
      const at = new Date().toISOString();
      sn.last_run = { state: 'passed', summary: sn.last_run?.summary || '检查通过', at };
      return json(200, { node: sn, flow_hash: f.flow_hash, run: { state: 'passed', output: sn.last_run.summary, commit: null, version_label: null, duration_ms: 600, runtime: 'jiuwenbox' } });
    }
    const cid = convsOf(m[1])[0].id;
    const msgId = `m-${uid()}`;
    emitConv(m[1], cid, 'message.user', { message_id: msgId, text: `开始「${flowOf(m[1]).nodes.find((x) => x.id === m[2])?.title || m[2]}」`, attachments: [], references: [], origin: body.origin ? { kind: 'page', ...body.origin, task_id: null } : null, delivery: 'immediate' });
    return json(202, { task: { task_id: `task-${uid()}`, origin: body.origin, title: '', state: 'queued', percent: null, step_label: null, conversation_id: cid, message_id: msgId, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null }, conversation_id: cid, message_id: msgId });
  });

  // ---------- 3.10 变更集 ----------
  // 带 owner 的变更集（如增量更新那一轮）由登记它的 routes-*.js 处理，这里让过去
  on('GET', '/projects/([^/]+)/scope', ({ m }) => json(200, scopeOf(m[1])));
  // CS-01：变更集列表（增量更新按待确认的助手变更集找所在对话）
  on('GET', '/projects/([^/]+)/changesets', ({ m, query }) => {
    const want = (query.get('state') || '').split(',').filter(Boolean);
    const items = [...db.changesets.entries()]
      .filter(([, cs]) => cs.pid === m[1] && (!want.length || want.includes(cs.state)))
      .map(([id, cs]) => ({ id, source: 'assistant', build_mode: 'L2', purpose: '助手的修改', state: cs.state, base: 'a71e03c9', conversation_id: cs.cid || null, run_id: null, slice_id: null, created_at: null, updated_at: null, merged: null, entity_count: 0 }));
    return json(200, { items, next_cursor: null });
  });
  on('GET', '/projects/([^/]+)/changesets/([^/]+)/diff', ({ m }) => {
    const cs = db.changesets.get(m[2]);
    if (cs?.owner) return undefined;
    const lines = F.OPTION_PREVIEWS[cs?.opt || 'A'];
    const file = (key, label, text) => ({ target: { type: 'file', key, title: label, label }, change: 'mod', sections: [{ title: '', lines: [{ kind: 'add', text }] }] });
    return json(200, { items: [
      { target: F.REFS.op, change: 'mod', sections: [{ title: '', lines }] },
      file('research/blueprint.json', '建模蓝图', '+ 第 4 段：审批提示'),
      file('notes/a.txt', '', '+ 草稿一行'),
      file('notes/b.txt', '', '+ 草稿两行'),
    ] });
  });
  on('GET', '/projects/([^/]+)/changesets/([^/]+)', ({ m }) => {
    const cs = db.changesets.get(m[2]);
    if (cs?.owner) return undefined;
    if (!cs) return err(404, 'changeset_not_found', '变更集不存在');
    return json(200, { id: m[2], source: 'assistant', build_mode: 'L2', purpose: '超量与审批提示', state: cs.state, base: 'a71e03c9', conversation_id: cs.cid, run_id: null, slice_id: 's5', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), merged: null, entity_count: 3, groups: [], checks: { errors: 0, warnings: 0, findings: [] }, next_version_label: 'v0.13' });
  });
  // CS-04：F3 起界面不再调（一轮结束自动合入）；mock 里留着，遗留的待确认变更集照旧能合入
  on('POST', '/projects/([^/]+)/changesets/([^/]+)/merge', ({ m }) => {
    const cs = db.changesets.get(m[2]);
    if (cs?.owner) return undefined;
    if (!cs) return err(404, 'changeset_not_found', '变更集不存在');
    return err(409, 'changeset_closed', '这一轮的修改已经生效或撤回', { state: cs.state });
  });
  // CS-05 撤回这一轮：合入过的回到上一个版本，另存一个版本
  on('POST', '/projects/([^/]+)/changesets/([^/]+)/discard', ({ m }) => {
    const pid = m[1];
    const cs = db.changesets.get(m[2]);
    if (cs?.owner) return undefined;
    if (!cs) return err(404, 'changeset_not_found', '变更集不存在');
    if (cs.state !== 'merged') return err(409, 'changeset_closed', '这一轮已经撤回过', { state: cs.state });
    cs.state = 'reverted';
    emitConv(pid, cs.cid, 'card.updated', F.roundChangesCard(cs.opt, 'reverted'));
    emitConv(pid, cs.cid, 'notice', { kind: 'discarded', text: '已撤回这一轮 · 版本 v0.14', version_label: 'v0.14', changeset_id: m[2], align: 'center' });
    emitConv(pid, cs.cid, 'suggestions.updated', { mode: 'normal', primary: { id: 'sg-redo', label: '重新决定步骤 2', icon: 'hand', primary: true, disabled: false, reason: '', priority: 3, action: { type: 'send', text: '重新决定步骤 2 的取值口径' } }, secondary: [], all_commands: true });
    const c = cardOf(pid);
    if (c) c.version_label = 'v0.14';
    emitProj(pid, 'changeset.updated', { changeset_id: m[2], state: 'merged', source: 'assistant', conversation_id: cs.cid, version_label: 'v0.13' });
    emitProj(pid, 'workspace.changed', { commit: 'c0ffee14', version_label: 'v0.14', source: 'revert', changeset_id: m[2], by: 'zhangsan', entries: [F.REFS.op], artifacts: [], paths: [] });
    if (c) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(c) });
    return json(200, { state: 'reverted', commit: 'c0ffee14', version_label: 'v0.14' });
  });

  // ---------- 3.11 会话 ----------
  const pool = { max: 12, active: 7, queued: 0, idle_ttl_s: 900 };
  on('GET', '/projects/([^/]+)/conversations', ({ m }) => {
    const list = convsOf(m[1]);
    const live = list.find((c) => c.live);
    return json(200, { items: list, live: live ? { conversation_id: live.id, state: live.state, busy: live.state === 'running' } : null, pool });
  });
  on('POST', '/projects/([^/]+)/conversations', ({ m, body }) => {
    const list = convsOf(m[1]);
    if (list.some((c) => c.state === 'running')) return err(409, 'session_busy', '另一条对话正在运行', { running_conversation_id: list.find((c) => c.state === 'running').id });
    const c = { id: uid().padStart(12, '0').slice(-12), title: body.title || '新会话', title_manual: !!body.title, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), live: false, state: 'idle', kind: 'chat', run_id: null };
    list.unshift(c);
    hub.seed(convKey(m[1], c.id), 'conversation', []);
    emitProj(m[1], 'conversation.updated', { conversation: c, deleted: false });
    return json(201, c);
  });
  on('PATCH', '/projects/([^/]+)/conversations/([^/]+)', ({ m, body }) => {
    const c = convsOf(m[1]).find((x) => x.id === m[2]);
    if (!c) return err(404, 'conversation_not_found', '对话不存在');
    const t = (body.title || '').trim();
    if (!t || t.length > 60) return err(400, 'title_invalid', '名称不合法');
    Object.assign(c, { title: t, title_manual: true });
    emitProj(m[1], 'conversation.updated', { conversation: c, deleted: false });
    return json(200, c);
  });
  on('DELETE', '/projects/([^/]+)/conversations/([^/]+)', ({ m }) => {
    const list = convsOf(m[1]);
    if (list.some((c) => c.state === 'running')) return err(409, 'session_busy', '对话运行中');
    db.convs.set(m[1], list.filter((c) => c.id !== m[2]));
    return json(200, { items: db.convs.get(m[1]) });
  });
  on('GET', '/projects/([^/]+)/conversations/([^/]+)', ({ m }) => {
    const c = convsOf(m[1]).find((x) => x.id === m[2]);
    if (!c) return err(404, 'conversation_not_found', '对话不存在');
    const hist = hub.history(convKey(m[1], m[2]));
    return json(200, {
      conversation_id: c.id, state: c.state, live: c.live, turn_started_at: null, queue_position: null, last_seq: Number(hist.at(-1)?.id || 0), history_count: hist.length,
      context: { used: 84000, window: 200000, pct: 42, compact_at_pct: 80, breakdown: { rules: 18000, materials: 30000, conversation: 26000, tool_results: 10000 }, last_call: { input: 18400, output: 1200, estimated: true } },
      pending_permission: null, open_questions: [], active_exec: null, open_changeset: null, config_pending: null, autobuild_run_id: null, pool,
      next: (({ body: _b, ...view }) => (view.id ? view : null))(db.next.get(`${m[1]}:${m[2]}`) || {}),
    });
  });
  on('GET', '/projects/([^/]+)/conversations/([^/]+)/messages', ({ m, query }) => {
    convsOf(m[1]);
    let items = hub.history(convKey(m[1], m[2]));
    const before = Number(query.get('before_seq') || 0);
    if (before) items = items.filter((e) => Number(e.id) < before);
    const limit = Number(query.get('limit') || 200);
    const page = items.slice(-limit);
    return json(200, { items: page, has_more: items.length > page.length, first_seq: Number(page[0]?.id || 0), last_seq: Number(page.at(-1)?.id || 0) });
  });
  // S-21 子任务步骤（说明页示例里的 t1～t3；不分页，三个子任务都不到一页）
  on('GET', '/projects/([^/]+)/conversations/([^/]+)/subagents/([^/]+)/steps', ({ m }) => {
    const found = subagentTask(decodeURIComponent(m[3]));
    if (!found) return err(404, 'subagent_not_found', '子任务不存在');
    return json(200, { ...found, next_cursor: null });
  });
  on('GET', '/projects/([^/]+)/conversations/([^/]+)/events', ({ m, query }) => { convsOf(m[1]); return hub.open(convKey(m[1], m[2]), 'conversation', query); });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/messages', ({ m, body }) => {
    const [pid, cid] = [m[1], m[2]];
    if (!body.text?.trim() && !(body.attachments || []).length) return err(400, 'message_empty', '消息为空');
    if (maintenance?.mode === 'reject_new_sessions' || maintenance?.mode === 'read_only') return err(503, maintenance.mode === 'read_only' ? 'maintenance_read_only' : 'maintenance_no_new_sessions', maintenance.message, { message: maintenance.message, until: null });
    const c = convsOf(pid).find((x) => x.id === cid);
    const running = c?.state === 'running';
    const msgId = `m-${uid()}`;
    const attachments = (body.attachments || []).map((id) => {
      const u = db.uploads.get(id);
      const ext = (u?.filename || '').split('.').pop().toLowerCase();
      return { upload_id: id, filename: u?.filename || id, size: u?.size || 0, kind: /docx?/.test(ext) ? 'doc' : /xlsx?|csv/.test(ext) ? 'xls' : ext === 'pdf' ? 'pdf' : /png|jpe?g/.test(ext) ? 'image' : 'file' };
    });
    const ev = emitConv(pid, cid, 'message.user', { message_id: msgId, text: body.text || '', attachments, references: body.references || [], origin: null, delivery: running ? 'steer' : 'immediate' });
    if (!running) runReply(pid, cid, body.text || '附件');
    else {
      // 运行中的插话：过一会儿「助手已看到」；打断时还没看到的撤回
      const key = `${pid}:${cid}`;
      const item = { message_id: msgId, text: body.text || '' };
      item.timer = setTimeout(() => {
        db.steer.set(key, (db.steer.get(key) || []).filter((x) => x !== item));
        emitConv(pid, cid, 'message.delivery', { message_id: msgId, state: 'seen', at: new Date().toISOString(), waiting_on: null });
      }, sc.steer === 'slow' ? 120000 : 2500);
      db.steer.set(key, [...(db.steer.get(key) || []), item]);
    }
    return json(202, { message_id: msgId, seq: Number(ev.id), conversation_id: cid, state: 'running', delivery: running ? 'steer' : 'immediate', queue_position: null });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/interrupt', ({ m }) => {
    const [pid, cid] = [m[1], m[2]];
    const t = db.running.get(`${pid}:${cid}`);
    if (!t) return json(200, { interrupted: false, exec: null });
    clearTimeout(t);
    db.running.delete(`${pid}:${cid}`);
    const returned = (db.steer.get(`${pid}:${cid}`) || []).map((x) => { clearTimeout(x.timer); return { message_id: x.message_id, text: x.text }; });
    db.steer.delete(`${pid}:${cid}`);
    returned.forEach((x) => emitConv(pid, cid, 'message.delivery', { message_id: x.message_id, state: 'returned', at: new Date().toISOString(), waiting_on: null }));
    const nx = db.next.get(`${pid}:${cid}`);
    if (nx) setNext(pid, cid, { ...nx, paused: true });
    emitConv(pid, cid, 'session.interrupted', { by: 'user', at: new Date().toISOString(), exec_id: 'ex-0412', position_label: '你在修改步骤 2 时打断了', saved: { files: 0, version_label: 'v0.12' } });
    setSession(pid, cid, 'idle');
    if (scopeOf(pid).state === 'assistant_running') setScope(pid, { ...scopeOf(pid), state: 'none', entries: [], artifacts: [], paths: [] });
    emitConv(pid, cid, 'suggestions.updated', F.suggestionsInterrupted());
    return json(200, { interrupted: true, exec: null, returned });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/restart', ({ m }) => {
    if (db.running.has(`${m[1]}:${m[2]}`)) return err(409, 'session_busy', '运行中，先打断');
    setSession(m[1], m[2], 'closed');
    return json(200, { state: 'closed' });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/questions/([^/]+)/answer', ({ m, body }) => {
    const [pid, cid, qid] = [m[1], m[2], m[3]];
    if (!body.option_id && !(body.option_ids || []).length && !body.custom_text) return err(400, 'answer_invalid', '没选');
    const hist = hub.history(convKey(pid, cid));
    if (hist.some((e) => e.type === 'question.answered' && e.data.question_id === qid)) return err(410, 'question_gone', '已回答', { state: 'answered' });
    if (qid === F.QUESTION_ID) runDecision(pid, cid, body.option_id, body.custom_text);
    else emitConv(pid, cid, 'question.answered', { question_id: qid, answer: body, by: 'user', summary: '已回答', answered_at: new Date().toISOString() });
    return json(200, { question: { ...F.decisionQuestion(), question_id: qid, state: 'answered', answer: body } });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/permissions/([^/]+)', ({ m, body }) => {
    emitConv(m[1], m[2], 'permission.resolved', { request_id: m[3], behavior: body.behavior, by: 'user', message: body.message || '' });
    return json(200, { ok: true });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/queue/cancel', () => err(409, 'not_queued', '当前不在排队'));
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/next/cancel', ({ m }) => {
    if (!db.next.has(`${m[1]}:${m[2]}`)) return err(409, 'next_not_found', '没有排着的命令');
    setNext(m[1], m[2], null);
    return json(200, { next: null });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/next/start', ({ m }) => {
    if (!db.next.has(`${m[1]}:${m[2]}`)) return err(409, 'next_not_found', '没有排着的命令');
    if (db.running.has(`${m[1]}:${m[2]}`)) return err(409, 'session_busy', '运行中');
    startNext(m[1], m[2]);
    return json(202, { next: null });
  });
  on('POST', '/projects/([^/]+)/conversations/([^/]+)/retry', () => err(409, 'nothing_to_retry', '没有可重试的一轮'));
  on('POST', '/projects/([^/]+)/execs/([^/]+)/(resume|abandon)', ({ m }) => json(m[3] === 'resume' ? 202 : 200, { ...(m[3] === 'resume' ? { mode: 'resume' } : {}), exec: { id: m[2], state: m[3] === 'resume' ? 'running' : 'abandoned' }, message_id: `m-${uid()}`, conversation_id: convsOf(m[1])[0].id }));
  on('GET', '/projects/([^/]+)/commands/catalog', () => json(200, F.commandCatalog()));
  // ---------- 页面命令（S-19 / S-20，契约 5.9.3：页面只发命令，进度经项目流 command.progress） ----------
  // routes-*.js 往 ctx.commandHooks 里放 { match(body) → bool, title?(body), steps?: string[], start?(pid, body, task), doneText?(body, task), finish?(pid, body, task) }：
  // 命中的命令按 steps 推进进度，开始时调 start（如改写入范围锁），跑完调 finish（改 mock 数据、发 artifacts.updated 等）；
  // doneText 换掉跑完时助手那句「做完了，结果已经回到…页」（返回空串就不发）。
  // ?mock=task:<页面 id> 让该页一打开就有一个进行中的任务（routes-*.js 导出的 taskSeeds）。
  const tasksOf = (pid) => {
    if (!db.tasks.has(pid)) db.tasks.set(pid, []);
    return db.tasks.get(pid);
  };
  const putTask = (pid, task, { transient = false } = {}) => {
    const list = tasksOf(pid);
    const i = list.findIndex((t) => t.task_id === task.task_id);
    if (i >= 0) list[i] = task; else list.push(task);
    hub.emit(projKey(pid), 'project', { type: 'command.progress', project_id: pid, data: task }, { transient });
    return task;
  };
  const seedTask = (pid, partial) => {
    const cid = convsOf(pid)[0].id;
    const task = {
      task_id: `task-${uid()}`, origin: { page: 'wb-l2', label: '工作台' }, title: '', state: 'running', percent: null, step_label: null,
      conversation_id: cid, message_id: `m-${uid()}`, exec_id: null, started_at: new Date(Date.now() - 95000).toISOString(), finished_at: null, result: null, ...partial,
    };
    tasksOf(pid).push(task);
    return task;
  };
  on('GET', '/projects/([^/]+)/commands/tasks', ({ m, query }) => {
    const pid = m[1];
    const page = query.get('origin_page');
    if (pid === F.MAIN_PID && sc.task && page === sc.task && !db.seeded.has(page) && TASK_SEEDS[page]) {
      db.seeded.add(page);
      seedTask(pid, TASK_SEEDS[page]());
    }
    const states = (query.get('state') || 'queued,running,waiting_user').split(',');
    return json(200, { items: tasksOf(pid).filter((t) => (!page || t.origin?.page === page) && states.includes(t.state)).slice(-50) });
  });
  on('POST', '/projects/([^/]+)/commands', ({ m, body }) => {
    const pid = m[1];
    if (!body.origin) return err(400, 'invalid_argument', '缺少 origin', { fields: { origin: 'required' } });
    const cid = body.conversation_id || convsOf(pid)[0].id;
    if (convsOf(pid).find((x) => x.id === cid)?.state === 'running') {
      // 运行中发的命令：平台代存，这一轮结束后开始（最多一条）
      const cur = db.next.get(`${pid}:${cid}`);
      if (cur && !body.replace) return err(409, 'next_occupied', '已经排着一条命令', { label: cur.label });
      const next = { id: `nx-${uid()}`, label: (body.text || body.command || '').replace(/^\//, ''), command: body.command || null, text: body.text || '', origin: body.origin, created_at: new Date().toISOString(), paused: false, body };
      setNext(pid, cid, next);
      const { body: _b, ...view } = next;
      return json(202, { task: null, conversation_id: cid, message_id: null, delivery: 'next', next: view });
    }
    return runCommand(pid, cid, body);
  });
  function runCommand(pid, cid, body) {
    const msgId = `m-${uid()}`;
    const text = body.text || body.command || '';
    emitConv(pid, cid, 'message.user', { message_id: msgId, text, command: body.command || null, attachments: [], references: [], origin: { kind: 'page', ...body.origin, task_id: null }, delivery: 'immediate' });
    const hook = commandHooks.find((h) => h.match(body));
    const task = putTask(pid, {
      task_id: `task-${uid()}`, origin: body.origin, title: hook?.title ? hook.title(body) : `助手正在${text.replace(/^\//, '')}`, state: 'running', percent: 0,
      step_label: hook?.steps?.[0] || null, conversation_id: cid, message_id: msgId, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null,
    });
    if (!hook) {
      runReply(pid, cid, text);
      later(pid, cid, 1300, () => putTask(pid, { ...task, state: 'done', percent: 100, finished_at: new Date().toISOString(), result: { summary: '已完成', refs: [] } }));
    } else {
      setSession(pid, cid, 'running');
      hook.start?.(pid, body, task);
      const steps = hook.steps || ['进行中'];
      steps.forEach((label, i) => {
        if (i) later(pid, cid, 900 * i, () => putTask(pid, { ...task, percent: Math.round((i / steps.length) * 100), step_label: label }, { transient: true }));
      });
      later(pid, cid, 900 * steps.length, () => {
        const done = putTask(pid, { ...task, state: 'done', percent: 100, step_label: steps.at(-1), finished_at: new Date().toISOString(), result: { summary: '已完成', refs: [] } });
        const doneText = hook.doneText ? hook.doneText(body, done) : `「${text.replace(/^\//, '')}」做完了，结果已经回到「${body.origin.label}」页。`;
        if (doneText) emitConv(pid, cid, 'message.assistant', { message_id: `m-${uid()}`, text_md: doneText, anchors: [], entry_links: [], continued: false });
        setSession(pid, cid, 'idle');
        hook.finish?.(pid, body, done);
      });
    }
    return json(202, { task, conversation_id: cid, message_id: msgId, delivery: 'immediate' });
  }

  // ---------- 检索、插件、证据 ----------
  on('GET', '/projects/([^/]+)/search', ({ query }) => {
    const q = query.get('q') || '';
    const hit = (group, title, snippet, page) => ({ group, title, snippet, target: { kind: 'object', id: 'object:display_qty_std', key: 'display_qty_std', name: title }, page });
    const items = q ? [hit('entries', `「${q}」`, '知识包 3 处、资料 2 处、报告 1 处', 'studio')] : [];
    return json(200, { q, groups: [{ group: 'entries', total: items.length, items }], term_summary: q ? [{ term: q, by_group: { entries: 3, materials: 2, reports: 1, knowledge: 0, artifacts: 0 }, referenced_by: 4 }] : [] });
  });
  on('POST', '/projects/([^/]+)/anchors/resolve', ({ body }) => json(200, {
    items: (body.anchors || []).map((a) => F.ANCHOR_RESOLUTIONS[a] || { anchor: a, scheme: a.split(':')[0], resolved: false, reason: 'not_in_snapshot', source_label: a, location_label: '', text: '', highlights: [], confidence: null, snapshot_id: null, page: null }),
  }));

  // 角色把关（契约 1.3）：roles 里没有任何一个所需角色时返回 403，否则返回 undefined 继续处理。
  // 用法：on('PUT', '/ops/...', ({ user }) => forbid(user, ['ops']) || json(200, …))
  const forbid = (user, roles) => (roles.some((r) => (user?.roles || [user?.role]).includes(r)) ? undefined : err(403, 'forbidden', '没有权限', { required_roles: roles }));

  // ---------- F2、F3、F4 各分组（routes-*.js） ----------
  const ctx = {
    on, json, err, wait, db, hub, sc, uid, F, cardOf, convKey, projKey, convsOf, flowOf, stageOf,
    emitConv, emitConvTransient, emitProj, emitUser, setSession, scopeOf, setScope, scopeLock, later, runReply,
    commandHooks, putTask, seedTask, tasksOf, artifactTypeDefs, forbid, authEnabled, maintenance,
    // 本服务端自己的入口：分组之间转调（如版本对比读 Studio 分组的 F-02）。不要用 window.__ontoMock，StrictMode 下会建两个服务端
    self: (url, init) => handle(url, init),
  };
  MODULES.forEach((mod) => mod.register?.(ctx));

  // ---------- 入口 ----------
  async function handle(input, init = {}) {
    const u = new URL(typeof input === 'string' ? input : input.url, 'http://mock.local');
    const method = (init.method || 'GET').toUpperCase();
    const path = u.pathname;
    let body = {};
    let form = null;
    if (init.body instanceof FormData) form = init.body;
    else if (typeof init.body === 'string' && init.body) {
      try { body = JSON.parse(init.body); } catch { body = {}; }
    }
    const route = routes.find((r) => r.method === method && r.re.test(path));
    await wait(60);
    if (!route) return err(404, 'not_found', `mock 没有这个接口：${method} ${path}`);
    const open = ['/api/v1/client-config', '/api/v1/auth/login', '/api/v1/auth/local', '/api/v1/auth/register', '/api/v1/auth/refresh'];
    const user = userOf(init);
    if (!open.includes(path) && !user) return err(401, 'unauthenticated', '未登录');
    // 只读维护不拦运维面（B3b 第 12 条）：运维要能推进、退出维护
    if (maintenance?.mode === 'read_only' && method !== 'GET' && !path.startsWith('/api/v1/auth/') && !path.startsWith('/api/v1/ops/')) {
      return err(503, 'maintenance_read_only', maintenance.message, { message: maintenance.message, until: null });
    }
    // 同一路径可以有几个处理者：返回 undefined 表示「不归我管」，交给后面登记的（如插件产物按类型分到各页的 mock）
    for (const r of routes.filter((x) => x.method === method && x.re.test(path))) {
      const res = await r.handler({ m: path.match(r.re), query: u.searchParams, body, form, user, init });
      if (res !== undefined) return res;
    }
    return err(404, 'not_found', `mock 没有处理这个请求：${method} ${path}`);
  }

  return { fetch: handle, hub, db, scenario: sc, emitConv, emitProj, emitUser };
}
