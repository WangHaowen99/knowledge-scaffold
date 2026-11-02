// 运维管控与健康（设计稿 ops-ctl）页面纯逻辑：格式化、状态到显示的映射、维护向导、插件运维的按钮可用性、表单校验、分区参数。
// 接口见契约 3.25 O-13～O-47；后端实际口径见 contract-changes B2、B3a、B3b（字段为 null 时这里都有兜底）。
import { isEvidencePack } from '../../state/evidence.js';

const pad2 = (n) => String(n).padStart(2, '0');
const DENY = '仅运维可操作';

// ---------- 分区（?section=） ----------
export const SECTIONS = ['health', 'sessions', 'maintenance', 'packs', 'core', 'startup', 'backups', 'tasks', 'versions', 'retention', 'diagnostics'];
const SECTION_ALIAS = {
  deps: 'health', ready: 'health', live: 'health', resources: 'health',
  pool: 'sessions', maint: 'maintenance', plugins: 'packs', 'core-default': 'core', 'pack-repo': 'core',
  report: 'startup', 'startup-report': 'startup', backup: 'backups', drills: 'backups', task: 'tasks',
  inventory: 'versions', diag: 'diagnostics',
};
export function sectionOf(q) {
  if (!q) return null;
  const k = String(q).trim().toLowerCase();
  if (SECTIONS.includes(k)) return k;
  return SECTION_ALIAS[k] || null;
}

// ---------- 格式化 ----------
export function fmtBytes(n) {
  if (n == null || !Number.isFinite(n) || n < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  if (i <= 2) return `${Math.round(v)} ${units[i]}`;
  return `${v >= 100 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, '')} ${units[i]}`;
}

// 已用 / 总量用同一个单位：「21.4 / 32 GB」
export function fmtBytesPair(used, total) {
  if (total == null || !Number.isFinite(total)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let t = total;
  while (t >= 1024 && i < units.length - 1) { t /= 1024; i += 1; }
  const k = 1024 ** i;
  const f = (x) => (i <= 2 ? String(Math.round(x / k)) : (x / k).toFixed(1).replace(/\.0$/, ''));
  return `${used == null ? '—' : f(used)} / ${f(total)} ${units[i]}`;
}

export function fmtDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '—';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${pad2(s % 60)} 秒`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} 天 ${h % 24} 小时` : `${d} 天`;
}

export function fmtMs(ms) {
  if (ms == null || !Number.isFinite(ms)) return '—';
  return `${Math.round(ms).toLocaleString('en-US')} ms`;
}

export const fmtCount = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US'));

const dt = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
};
export function fmtClock(iso) {
  const d = dt(iso);
  return d ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}` : '—';
}
export function fmtDay(iso) {
  const d = dt(iso);
  return d ? `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` : '—';
}
export function fmtDateTime(iso) {
  const d = dt(iso);
  return d ? `${fmtDay(iso)} ${fmtClock(iso)}` : '—';
}
export function fmtStamp(iso) {
  const d = dt(iso);
  return d ? `${fmtDateTime(iso)}:${pad2(d.getSeconds())}` : '—';
}
// 今天的只写时刻，昨天写「昨天 HH:MM」，更早写日期
export function fmtWhen(iso, now = Date.now()) {
  const d = dt(iso);
  if (!d) return '—';
  const today = new Date(now);
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, today)) return fmtClock(iso);
  const y = new Date(now);
  y.setDate(y.getDate() - 1);
  if (same(d, y)) return `昨天 ${fmtClock(iso)}`;
  return fmtDateTime(iso);
}

// 用户名 → 显示名（AD-01 拿到的表；拿不到时用用户名，system 显示「系统」）
export function nameOf(names, username) {
  if (!username) return '—';
  if (username === 'system') return '系统';
  return names?.[username] || username;
}

// ---------- 健康（O-13、O-14） ----------
export const DEP_LABEL = {
  model_gateway: '模型网关', auth_server: 'auth-server', sandbox_gateway: '沙箱网关', connector_gateway: '连接器网关',
  disk: '磁盘', git: 'git', assembly: '装配目录',
};
const DEP_STATUS = {
  ready: { label: '就绪', tone: 'blue', icon: 'check' },
  ok: { label: '就绪', tone: 'blue', icon: 'check' },
  slow: { label: '慢', tone: 'amber', dot: true, late: true },
  warn: { label: '警告', tone: 'amber', dot: true },
  fail: { label: '失败', tone: 'red', icon: 'x', late: true },
};
export function depView(d) {
  const st = DEP_STATUS[d?.status] || { label: d?.status || '未知', tone: 'gray' };
  return {
    key: d?.name,
    name: DEP_LABEL[d?.name] || d?.name || '—',
    probe: d?.probe || '',
    latency: fmtMs(d?.latency_ms),
    late: !!st.late,
    chip: { label: st.label, tone: st.tone, icon: st.icon || null, dot: !!st.dot },
    issue: d?.last_issue || '—',
  };
}

export function liveView(live, now = Date.now()) {
  const ok = live?.status === 'ok';
  const bits = [];
  if (live?.latency_ms != null) bits.push(`${live.latency_ms} ms`);
  if (live?.uptime_ms != null) bits.push(`进程已运行 ${fmtDuration(live.uptime_ms)}${live.started_at ? `（上次启动 ${fmtWhen(live.started_at, now)}）` : ''}`);
  return {
    ok, lead: `GET ${live?.path || '/api/health/live'}`, text: bits.join(' · '),
    chip: ok ? { label: '正常', tone: 'blue', icon: 'check' } : { label: '无响应', tone: 'red', icon: 'x' },
  };
}

// 就绪：降级时整张卡琥珀色，未就绪红色；左栏角标在 parts.jsx 里按同一个 status 取
export function readyView(ready) {
  const n = ready?.total ? ` · ${ready.passed ?? 0} / ${ready.total}` : '';
  const base = { lead: `GET ${ready?.path || '/api/health/ready'}`, text: ready?.summary || '' };
  if (ready?.status === 'ready') return { ...base, chip: { label: '就绪', tone: 'blue', icon: 'check' }, tone: null };
  if (ready?.status === 'degraded') return { ...base, chip: { label: `降级${n}`, tone: 'amber', dot: true }, tone: 'amber' };
  if (ready?.status === 'not_ready') return { ...base, chip: { label: `未就绪${n}`, tone: 'red', dot: true }, tone: 'red' };
  return { ...base, chip: { label: '读取中', tone: 'gray' }, tone: null };
}

const EXIT_REASON = { unclean: '意外结束', shutdown: '正常停止' };
// 上次退出：O-13 runtime.last_exit（B2 恒为 null）为空时用 O-35 的
export function exitText(lastExit, { withTime = true } = {}) {
  if (!lastExit) return null;
  const reason = EXIT_REASON[lastExit.reason] || lastExit.reason || '未知';
  const code = lastExit.code != null && lastExit.code !== 0 ? `（exit ${lastExit.code}）` : '';
  return withTime && lastExit.at ? `${fmtDateTime(lastExit.at)} · ${reason}${code}` : `${reason}${code}`;
}

// 引擎一行：「运行时 0.9.4 · 命令行 2.1.30」，没有命令行版本时接备注（如「AVX 可用」）
export function engineParts(e) {
  const out = [];
  if (e?.runtime_version) out.push(['运行时', e.runtime_version]);
  if (e?.cli_version) out.push(['命令行', e.cli_version]);
  if (e?.note) out.push([null, e.note]);
  return out;
}

export function loadText(load) {
  if (!Array.isArray(load) || !load.length) return '—';
  return load.map((x) => (x == null ? '—' : Number(x).toFixed(2))).join(' / ');
}

// ---------- 会话池（O-15～O-19） ----------
export const SESSION_STATE = {
  running: { st: 'running', label: '运行中' },
  idle: { label: '空闲', tone: 'gray' },
  queued: { label: '排队中', tone: 'amber', dot: true },
};
export const OVERFLOW_OPTIONS = [
  { value: 'evict_idle_then_queue', label: '先驱逐最久空闲的，全忙则排队' },
  { value: 'queue_only', label: '不驱逐，池满时排队' },
];

export function poolSub(pool) {
  if (!pool) return '';
  return `活跃 ${pool.active ?? 0} / 上限 ${pool.config?.max_sessions ?? '—'} · 排队 ${pool.queued ?? 0}`;
}
export function idleText(item) {
  if (item?.state === 'queued') return item.queue_position ? `排第 ${item.queue_position} 位` : '排队中';
  if (item?.state === 'idle') return fmtDuration(item.idle_ms);
  return '—';
}
export function sessionActions(item, opsWrite) {
  const running = item?.state === 'running';
  return {
    show: item?.state !== 'queued',
    recycle: { disabled: !opsWrite || running, title: !opsWrite ? DENY : running ? '运行中的会话不能回收，只能强制断开' : null },
    kill: { disabled: !opsWrite, title: opsWrite ? null : DENY },
  };
}
// 弹窗副标题：回收带引擎（设计稿 opRecycle），强制断开不带（opKill）
export function sessionSub(item, names, { engine = true, withState = true } = {}) {
  const bits = [item?.project?.name || '—', nameOf(names, item?.project?.owner)];
  if (engine && item?.engine_label) bits.push(item.engine_label);
  if (withState) bits.push(item?.state === 'running' ? '正在运行' : item?.state === 'idle' ? `空闲 ${fmtDuration(item.idle_ms)}` : '排队中');
  return bits.join(' · ');
}
export function drainSummary(pool) {
  const items = pool?.items || [];
  const running = items.filter((x) => x.state === 'running');
  return {
    online: items.filter((x) => x.state !== 'queued').length,
    running: running.length,
    autobuild: running.filter((x) => x.autobuild).length,
  };
}

export function poolForm(config) {
  return {
    max_sessions: String(config?.max_sessions ?? ''),
    idle_ttl_s: String(config?.idle_ttl_s ?? ''),
    overflow_policy: config?.overflow_policy || 'evict_idle_then_queue',
  };
}
const intIn = (v, lo, hi) => /^\d+$/.test(String(v).trim()) && Number(v) >= lo && Number(v) <= hi;
// O-16：max_sessions 1–64，idle_ttl_s 60–86400
export function validatePool(form) {
  const e = {};
  if (!intIn(form.max_sessions, 1, 64)) e.max_sessions = '整数 1–64';
  if (!intIn(form.idle_ttl_s, 60, 86400)) e.idle_ttl_s = '整数 60–86400 秒';
  if (!OVERFLOW_OPTIONS.some((o) => o.value === form.overflow_policy)) e.overflow_policy = '请选择超限时的处理';
  return e;
}
export function poolDirty(form, config) {
  if (!config) return false;
  return String(config.max_sessions) !== String(form.max_sessions).trim()
    || String(config.idle_ttl_s) !== String(form.idle_ttl_s).trim()
    || config.overflow_policy !== form.overflow_policy;
}
export const poolBody = (form) => ({
  max_sessions: Number(form.max_sessions), idle_ttl_s: Number(form.idle_ttl_s), overflow_policy: form.overflow_policy,
});

// ---------- 维护模式（O-20～O-24，契约 1.13、#57；B3b 第 11 条） ----------
export const MAINT_MODES = {
  reject_new_sessions: { label: '拒绝新会话', desc: '已有会话跑完这一轮，网页仍可编辑' },
  read_only: { label: '只读', desc: '拒绝所有写入' },
};

// 对用户提示的默认写法：十分钟后的整十分开始
export function defaultMaintMessage(mode, { upgrade = true, now = Date.now() } = {}) {
  const d = new Date(now + 10 * 60e3);
  d.setMinutes(Math.ceil(d.getMinutes() / 10) * 10, 0, 0);
  const at = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const what = upgrade ? '升级' : '维护';
  return mode === 'read_only'
    ? `平台将在 ${at} ${what}，预计 20 分钟，期间只能查看。`
    : `平台将在 ${at} ${what}，预计 20 分钟，期间暂停新会话。`;
}

const STEP_TITLE = { drain: '排空', upgrade: '升级', resume: '恢复' };
const STEP_IDS = ['restrict', 'drain', 'upgrade', 'resume'];

// 四步条只在维护进行中出现（R19）：已完成和当前这一步显示实时状态，还没走到的只写标题
export function maintSteps(state) {
  if (!state?.active) return [];
  const byId = Object.fromEntries((state.steps || []).map((s) => [s.id, s]));
  return STEP_IDS.map((id, i) => {
    const s = byId[id];
    const st = s?.state || 'pending';
    const reached = st === 'done' || st === 'current';
    let title;
    let detail;
    if (id === 'restrict') {
      title = MAINT_MODES[state.mode]?.label || '限制';
      detail = reached ? [state.started_at ? `${fmtClock(state.started_at)} 开始` : null, s?.detail].filter(Boolean).join(' · ') : '';
    } else {
      title = STEP_TITLE[id];
      detail = reached ? s?.detail || '' : '';
    }
    return {
      id, n: i + 1, title, detail,
      cls: st === 'done' ? 'done' : st === 'current' ? 'cur' : '',
      progress: st === 'current' && s?.progress_pct != null ? s.progress_pct : null,
    };
  });
}

export function currentStep(state) {
  return state?.active ? (state.steps || []).find((s) => s.state === 'current') || null : null;
}

// 维护窗口底部三个按钮；第 4 步「恢复」不再推进（409），改调 O-24 退出
export function maintActions(state, opsWrite) {
  if (!state?.active) return null;
  const cur = currentStep(state);
  const deny = opsWrite ? null : DENY;
  const busy = cur && cur.progress_pct != null && cur.progress_pct < 100;
  const draining = cur?.id === 'drain' && busy;
  const next = cur?.id === 'resume'
    ? { label: '恢复', op: 'exit', disabled: !opsWrite, title: deny || '解除限制，退出维护模式' }
    : {
      label: '继续下一步', op: 'advance', disabled: !opsWrite || !cur || busy,
      title: deny || (!cur ? '维护步骤都已完成' : busy ? (cur.id === 'drain' ? '排空还没完成，会话都回收后才能继续' : '这一步还在进行') : null),
    };
  return {
    force: { disabled: !opsWrite || !draining, title: deny || (draining ? null : '只有排空进行中才需要强制断开') },
    cancel: { disabled: !opsWrite, title: deny },
    next,
  };
}

export function maintTitle(state) {
  const t = state?.active ? state.target : null;
  return t ? `维护窗口 · 升级 ${t.pack} ${t.version}` : '维护窗口';
}

// 进入维护模式的请求体（O-21）；target 只认 core_upgrade（B3b 第 11 条）
export function maintBody(form) {
  const body = { mode: form.mode, message: form.message.trim(), autobuild: 'pause_at_safe_point' };
  if (form.target) body.target = { kind: 'core_upgrade', pack: 'core', version: form.target };
  return body;
}
export function maintProblem(form) {
  if (!MAINT_MODES[form.mode]) return '请选择限制方式';
  if (!form.message.trim()) return '请填写对用户的提示';
  if (form.message.trim().length > 200) return '提示不超过 200 字';
  return null;
}

// 维护接口的错误说成人话
export function maintErrorText(e, fallback) {
  if (e?.code === 'maintenance_step_blocked') {
    const d = e.details || {};
    if (d.step === 'resume') return '已经到最后一步：点「恢复」退出维护模式';
    return d.remaining ? `当前步骤还没完成：还剩 ${d.remaining} 个会话` : '当前步骤还没完成，不能进入下一步';
  }
  if (e?.code === 'maintenance_active') return '已经在维护模式中';
  if (e?.code === 'maintenance_not_active') return '维护模式已经结束';
  return fallback;
}

// ---------- 插件运维（O-25～O-29） ----------
export const PACK_KIND = {
  capability: '能力包', industry: '行业包', system: '系统知识包', provider: '证据源提供方', node: '能力包', exporter: '导出器', tenant: '租户包',
};
export const CHANNEL = {
  stable: { label: 'stable', tone: 'line' },
  beta: { label: 'beta', tone: 'indigo' },
  retired: { label: '已下线', tone: 'gray' },
};
export const BREAKER = {
  closed: { label: '正常', tone: 'blue', icon: 'check' },
  half_open: { label: '半开 · 试探中', tone: 'amber', dot: true },
  open: { label: '已熔断', tone: 'red', icon: 'ban' },
};
export const ERROR_RATE_ALERT = 5;
export function errorRateView(pct) {
  if (pct == null || !Number.isFinite(pct)) return { text: '—', alert: false };
  return { text: `${pct.toFixed(1)}%`, alert: pct >= ERROR_RATE_ALERT };
}

export function packActions(item, opsWrite) {
  const retired = item?.channel === 'retired';
  const deny = opsWrite ? null : DENY;
  const open = item?.breaker === 'open';
  return {
    rollout: { disabled: !opsWrite || retired, title: deny || (retired ? '已下线的版本不再放量' : null) },
    rollback: {
      disabled: !opsWrite || retired || !item?.rollback_to,
      title: deny || (retired ? '已下线' : item?.rollback_to ? `把 ${item.channel} 通道改回 ${item.rollback_to}` : '这个通道没有更早的版本可回滚'),
    },
    disable: { disabled: !opsWrite || retired, title: deny || (retired ? '已禁用' : null) },
    breaker: open
      ? { label: '恢复', action: 'close', kind: 'soft', disabled: !opsWrite, title: deny || '先进入半开试探' }
      : { label: '熔断', action: 'open', kind: 'danger', disabled: !opsWrite || retired, title: deny || (retired ? '已下线' : null) },
  };
}

export const ROLLOUT_PRESETS = [0, 10, 20, 50, 100];
export function rolloutProblem(v) {
  if (!intIn(v, 0, 100)) return '比例是 0–100 的整数';
  return null;
}

// core 默认版本表：beta 在前、stable、其余按发布时间倒序
export function coreRows(core) {
  const order = { beta: 0, stable: 1, retired: 2 };
  return [...(core?.versions || [])].sort((a, b) => (order[a.channel] ?? 3) - (order[b.channel] ?? 3)
    || String(b.released_at || '').localeCompare(String(a.released_at || '')))
    .map((v) => ({
      ...v,
      op: v.channel === 'beta' ? 'promote' : v.channel === 'stable' || v.is_default ? 'current' : 'rollback',
    }));
}

// ---------- 启动自检（O-35；B3b 第 2 条） ----------
export const CHECK_ICON = { fix: ['fix', 'wrench'], ok: ['ok', 'check'], warn: ['w', 'history'] };
export const CHECK_RESULT = {
  cleared: { label: '已清除', tone: 'teal' },
  committed: { label: '已补提交', tone: 'teal' },
  ok: { label: '正常', tone: 'blue' },
  reassembled: { label: '已重新装配', tone: 'teal' },
  awaiting_user: { label: '待用户续跑', tone: 'amber' },
  failed: { label: '失败', tone: 'red' },
};
export function startupSub(report) {
  if (!report) return '';
  const exit = exitText(report.last_exit, { withTime: false });
  return `${fmtStamp(report.at)} · 上次退出：${exit || '没有记录'}`;
}

// ---------- 备份与恢复（O-36～O-39；B2 第 20、21 条，B3b 第 14 条） ----------
export const SCOPE_LABEL = {
  workspaces: '工作区', session_history: '会话历史', session_index: '会话索引', config: '配置', mcp_tokens: 'MCP token',
  jiuwen_archives: '内测引擎存档', studio_drafts: 'Studio 草稿', audit: '审计日志', cli_transcripts: '命令行记录',
  service_tokens: '服务令牌', releases: '发布包', packs: '包仓库',
};
export function scheduleText(s) {
  if (!s) return '';
  const keep = s.keep != null ? `保留最近 ${s.keep} 份` : null;
  const when = s.daily_at ? `每天 ${s.daily_at}` : s.interval_hours ? `每 ${s.interval_hours} 小时` : null;
  return [when, keep].filter(Boolean).join(' · ');
}
export function backupScope(item) {
  if (item?.scope === 'workspace_only') return { chip: true, text: '仅工作区' };
  return { chip: false, text: `全量 ${(item?.scope_items || []).length} 项` };
}
export function drillView(drill) {
  if (!drill) return { tone: 'gray', icon: 'info', title: '还没做过恢复演练', detail: '' };
  const bits = [fmtDay(drill.at)];
  if (drill.duration_ms != null) bits.push(`用时 ${fmtDuration(drill.duration_ms)}`);
  if (drill.detail) bits.push(drill.detail);
  if (drill.result === 'passed') return { tone: 'teal', icon: 'check', title: '最近一次恢复演练：通过', detail: bits.join(' · ') };
  return { tone: 'red', icon: 'alert', title: '最近一次恢复演练：没通过', detail: bits.join(' · ') };
}

// ---------- 任务中心（O-40、O-41） ----------
export const TASK_TYPE = {
  collection: '证据采集', export: '导出', backup: '备份', sweep: '清扫', pack_install: '插件安装', drill: '恢复演练', pack_repo_backup: '包仓库备份',
};
export const TASK_STATE = {
  queued: { label: '排队中', tone: 'gray' },
  running: { st: 'running', label: '运行中' },
  succeeded: { label: '成功', tone: 'teal' },
  partial: { label: '部分完成', tone: 'amber' },
  failed: { label: '失败', tone: 'red' },
  cancelled: { label: '已取消', tone: 'gray' },
};
const LIVE = ['queued', 'running'];
export const isLiveTask = (t) => LIVE.includes(t?.state);

export function taskAction(task, opsWrite) {
  let a = null;
  if (isLiveTask(task)) a = { op: 'cancel', label: '取消', kind: undefined };
  else if (task?.state === 'failed') a = { op: 'retry', label: '重试', kind: 'soft' };
  else if (task?.state === 'partial') a = task.type === 'collection' ? { op: 'resume', label: '续采', kind: 'soft' } : { op: 'retry', label: '重试', kind: 'soft' };
  if (!a) return null;
  return { ...a, disabled: !opsWrite, title: opsWrite ? null : DENY };
}
export function taskProgress(task) {
  const pct = task?.progress_pct ?? (task?.state === 'succeeded' ? 100 : 0);
  return { pct, tone: task?.state === 'failed' ? 'amber' : undefined };
}

// 任务从进行中变成结束时，要刷新哪几块
const TASK_RELOAD = { backup: ['backups'], drill: ['backups', 'drills'], pack_repo_backup: ['repoBackups'] };
export function reloadsAfter(prev, next) {
  const was = new Map((prev || []).map((t) => [t.id, t.state]));
  const keys = new Set();
  (next || []).forEach((t) => {
    const before = was.get(t.id);
    if (!isLiveTask(t) && (before === undefined ? false : LIVE.includes(before))) (TASK_RELOAD[t.type] || []).forEach((k) => keys.add(k));
  });
  return [...keys];
}

export function taskActionToast(op, task, e) {
  if (e?.needsConfirmation || e?.code === 'confirmation_required') return '生产环境的续采和重试要项目所有者确认，已通知所有者';
  if (e) return null;
  return { cancel: '已取消任务', retry: '已重新开始', resume: '已续采，从断点继续' }[op] || '已处理';
}

// ---------- 版本清单（O-42） ----------
export const INV_CATEGORY = {
  kernel: '内核', capability: '能力包', industry: '行业包', system: '系统知识包', provider: '提供方', exporter: '导出器',
  engine: '引擎', data_format: '数据格式', lockfile: '锁文件',
};

// ---------- 数据保留（O-43、O-44） ----------
export function retentionText(item) {
  const r = item?.retention || {};
  if (r.days != null) return `${r.days} 天`;
  if (r.keep_last != null) return item.id === 'snapshots' ? `每个数据源最近 ${r.keep_last} 份` : `最近 ${r.keep_last} 份`;
  return '—';
}
// 证据源本期不开放（ADR-0056）：证据源不可用时，运维管控不列证据相关的行——数据保留的证据快照、
// 插件运维与版本清单里的证据源提供方（和系统逆向建模包）、任务里的证据采集
export function visibleRetention(items, { evidenceOn = false } = {}) {
  return (items || []).filter((r) => evidenceOn || r.id !== 'snapshots');
}
export function visiblePacks(items, { evidenceOn = false } = {}) {
  return (items || []).filter((p) => evidenceOn || (p.kind !== 'provider' && !isEvidencePack(p.name)));
}
export function visibleInventory(items, { evidenceOn = false } = {}) {
  return (items || []).filter((r) => evidenceOn || (r.category !== 'provider' && !isEvidencePack(r.component) && !/^evidence/.test(r.component || '')));
}
export function visibleTasks(items, { evidenceOn = false } = {}) {
  return (items || []).filter((t) => evidenceOn || t.type !== 'collection');
}

export function retentionForm(items) {
  return (items || []).map((x) => {
    const unit = x.retention?.days != null ? 'days' : 'keep_last';
    return { id: x.id, label: x.label, unit, value: String(x.retention?.[unit] ?? ''), editable: !!x.editable };
  });
}
export function validateRetention(form) {
  const e = {};
  form.forEach((f) => {
    if (!f.editable) return;
    if (f.unit === 'days' && !intIn(f.value, 1, 3650)) e[f.id] = '整数 1–3650 天';
    if (f.unit === 'keep_last' && !intIn(f.value, 1, 100)) e[f.id] = '整数 1–100 份';
  });
  return e;
}
// 只送改过的、可编辑的项
export function retentionBody(form, items) {
  const before = Object.fromEntries((items || []).map((x) => [x.id, x.retention || {}]));
  return {
    items: form.filter((f) => f.editable && String(before[f.id]?.[f.unit]) !== String(f.value).trim())
      .map((f) => ({ id: f.id, retention: { [f.unit]: Number(f.value) } })),
  };
}

// ---------- 诊断包（O-45～O-47） ----------
// [include 取值, 弹窗里的说法, 卡片上的简称, 默认勾选]
export const DIAG_ITEMS = [
  ['logs_24h', '脱敏日志（最近 24 小时）', '脱敏日志 24 小时', true],
  ['config_refs', '配置（只含引用，不含密钥）', '配置（只含引用）', true],
  ['versions', '版本清单', '版本清单', true],
  ['recent_errors', '最近错误 100 条', '最近错误 100 条', true],
  ['startup_report', '启动自检报告', '启动自检报告', true],
  ['pool_alert_snapshot', '会话池与告警快照', '会话池与告警快照', false],
];
export const diagDefaults = () => DIAG_ITEMS.filter((x) => x[3]).map((x) => x[0]);
export function diagLine(d, names) {
  return [d.name, fmtBytes(d.size), nameOf(names, d.created_by)].join(' · ');
}
export function diagNote(latest, names) {
  return latest ? `上次生成：${fmtDateTime(latest.created_at)} · ${nameOf(names, latest.created_by)}` : null;
}
