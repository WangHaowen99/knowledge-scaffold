// mock 数据：运维管控与健康（O-13～O-47）。项目只给元数据视图，人名和项目取 platform-data.js。
// 维护向导、排空、后台任务的推进都是按时间推算的纯函数（不开定时器）：每次请求先 tick(now)，测试可以传入 now。
import { platformProjects } from './platform-data.js';

const SEC = 1000;
const MIN = 60 * SEC;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const GB = 1024 ** 3;
const TB = 1024 ** 4;
const MB = 1024 ** 2;
const iso = (t) => new Date(t).toISOString();
const pad2 = (n) => String(n).padStart(2, '0');
const stamp = (t) => {
  const d = new Date(t);
  return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}`;
};
// 某天的固定时刻（本地时间），daysAgo=0 为今天
const at = (now, daysAgo, h, m = 0) => {
  const d = new Date(now);
  d.setDate(d.getDate() - daysAgo);
  d.setHours(h, m, 0, 0);
  return d.getTime();
};

// 排空时每个正在跑的会话跑完这一轮要多久（mock 用秒级，便于看到推进）
export const DRAIN_STEP_MS = 6 * SEC;
export const UPGRADE_MS = 5 * SEC;
export const TASK_MS = 4 * SEC;

// core 的版本号是包仓库里的真实写法（清单版本加内容摘要，B3a 第 14 条）
export const CORE = { stable: '0.1.0+3f9a1c2d', beta: '0.1.1+8e41b7c0', retired: '0.1.0+a4c19e57' };

export function metaView(p) {
  return {
    id: p.id, name: p.name, owner: p.owner, stage_label: p.stage, build_mode: p.build_mode,
    archived: !!p.archived, created_at: null, last_activity_at: p.last_activity_at || null, disk_bytes: p.disk_bytes,
  };
}
const projectByName = (name) => platformProjects().find((p) => p.name === name);

// ---------- 健康（O-13） ----------
// variant：degraded（缺省，连接器网关慢、磁盘低于阈值）| ready | not_ready
export function healthData(now, variant = 'degraded') {
  const started = now - (2 * HOUR + 40 * MIN);
  const deps = [
    { name: 'model_gateway', probe: '稳定引擎网关 · https://gateway.example.com/v1/models', latency_ms: 820, status: 'ready', last_issue: null },
    { name: 'auth_server', probe: 'JWKS 公钥拉取', latency_ms: 46, status: 'ready', last_issue: null },
    { name: 'sandbox_gateway', probe: 'sandbox-gateway /v1/status · 脚本验证沙箱已连接', latency_ms: 38, status: 'ready', last_issue: null },
    { name: 'connector_gateway', probe: 'connector-gateway /health · 已加载 3 个连接器', latency_ms: 1820, status: 'slow', last_issue: `${new Date(now - 41 * MIN).toTimeString().slice(0, 5)} 超时 1 次` },
    { name: 'disk', probe: '/data 可写探测 · 可用 18%', latency_ms: 4, status: 'warn', last_issue: '低于 20% 阈值' },
    { name: 'git', probe: '工作区读写探测 · git 2.43.0', latency_ms: 12, status: 'ready', last_issue: null },
  ];
  let ready = { status: 'degraded', passed: 5, total: 6, summary: '连接器网关响应慢、磁盘低于阈值，仍接受新会话' };
  if (variant === 'ready') {
    deps[3] = { ...deps[3], latency_ms: 120, status: 'ready', last_issue: null };
    deps[4] = { ...deps[4], probe: '/data 可写探测 · 可用 46%', status: 'ready', last_issue: null };
    ready = { status: 'ready', passed: 6, total: 6, summary: '全部依赖就绪' };
  }
  if (variant === 'not_ready') {
    deps[0] = { ...deps[0], latency_ms: 30000, status: 'fail', last_issue: '连续 3 次超时' };
    ready = { status: 'not_ready', passed: 4, total: 6, summary: '模型网关不可用，暂停接受新会话' };
  }
  const lastExit = startupLastExit(started);
  return {
    live: { status: 'ok', path: '/api/health/live', latency_ms: 3, uptime_ms: now - started, started_at: iso(started) },
    ready: { path: '/api/health/ready', ...ready },
    dependencies: deps,
    runtime: {
      engines: [
        { id: 'claude', label: '稳定引擎', runtime_version: '0.9.4', cli_version: '2.1.30', available: true, note: null },
        { id: 'jiuwen', label: '内测引擎', runtime_version: '0.4.1', cli_version: null, available: true, note: 'AVX 可用' },
      ],
      disk: { total: 1.8 * TB, used_pct: variant === 'ready' ? 54 : 82, warn: variant !== 'ready' },
      load: [3.21, 2.87, 2.4],
      cpu_cores: 8,
      memory: { used: 21.4 * GB, total: 32 * GB },
      process: { cmd: 'python run.py', pid: 1, init_managed: true },
      last_exit: lastExit,
      restarts: 3,
    },
  };
}
const startupLastExit = (started) => ({ at: iso(started - 70 * SEC), reason: 'unclean', code: null });

// O-14：重新探测一遍，耗时有小幅波动
export function probe(deps, n) {
  return deps.map((d, i) => ({ ...d, latency_ms: d.status === 'fail' ? d.latency_ms : Math.max(1, Math.round(d.latency_ms * (1 + (((n + i) % 5) - 2) / 40))) }));
}

// ---------- 会话池（O-15） ----------
// variant：缺省 8 个（运行 3、空闲 4、排队 1）；empty 没有会话
export function sessionPool(now, variant) {
  const row = (name, engine, state, events, extra = {}) => ({
    project: metaView(projectByName(name)), engine_label: engine, state, events,
    idle_ms: null, queue_position: null, autobuild: false, ...extra,
  });
  const items = variant === 'empty' ? [] : [
    row('门店补货单编制', '稳定引擎', 'running', 1284),
    row('快递分拣异常处理', '内测引擎', 'running', 866, { autobuild: true }),
    row('社区诊所排队分诊', '稳定引擎', 'idle', 412, { idle_ms: 4 * MIN + 10 * SEC }),
    row('酒店客房清洁排程', '内测引擎', 'idle', 96, { idle_ms: 9 * MIN + 2 * SEC }),
    row('共享单车故障诊断', '稳定引擎', 'idle', 2031, { idle_ms: 2 * MIN + 33 * SEC }),
    row('图书借阅管理', '内测引擎', 'idle', 58, { idle_ms: 7 * MIN + 48 * SEC }),
    row('停车场车位调度', '内测引擎', 'running', 1520, { autobuild: true }),
    row('食堂菜单与备餐', '稳定引擎', 'queued', null, { queue_position: 1 }),
  ];
  return { config: { max_sessions: 12, idle_ttl_s: 600, overflow_policy: 'evict_idle_then_queue' }, items, drain: null, at: now };
}

export function poolView(pool, now) {
  const items = pool.items.map(({ finish_at: _f, idle_since: since, ...x }) => (x.state === 'idle' && since ? { ...x, idle_ms: now - since } : x));
  return {
    config: { ...pool.config },
    active: items.filter((x) => x.state !== 'queued').length,
    queued: items.filter((x) => x.state === 'queued').length,
    items,
  };
}

// 空闲时长按时间走：第一次读时记下「从什么时候开始空闲」
export function stampIdle(pool, now) {
  pool.items.forEach((x) => { if (x.state === 'idle' && !x.idle_since) x.idle_since = now - (x.idle_ms || 0); });
}

// 开始排空：空闲的立即回收、排队的拒绝；正在跑的依次跑完这一轮（finish_at）
export function startDrain(pool, now) {
  const running = pool.items.filter((x) => x.state === 'running');
  const online = pool.items.filter((x) => x.state !== 'queued').length;
  running.forEach((x, i) => { x.finish_at = now + (i + 1) * DRAIN_STEP_MS; });
  pool.items = running;
  pool.drain = { at: now, online, autobuild: running.filter((x) => x.autobuild).length, forced: 0 };
  return { online, running: running.length, autobuild_running: pool.drain.autobuild, eta_ms: running.length * DRAIN_STEP_MS };
}

// 时间到了的会话跑完这一轮被回收；全部回收后排空结束
export function tickPool(pool, now) {
  const before = pool.items.length;
  pool.items = pool.items.filter((x) => !(x.finish_at && x.finish_at <= now));
  return before !== pool.items.length;
}

export function forceDrain(pool) {
  const n = pool.items.filter((x) => x.finish_at).length;
  pool.items = pool.items.filter((x) => !x.finish_at);
  if (pool.drain) pool.drain.forced += n;
  return n;
}

// ---------- 维护模式（O-20～O-24） ----------
const STEP_LABEL = { restrict: '限制', drain: '排空', upgrade: '升级', resume: '恢复' };
const steps = (cur) => ['restrict', 'drain', 'upgrade', 'resume'].map((id, i, all) => ({
  id, label: STEP_LABEL[id], state: i < all.indexOf(cur) ? 'done' : id === cur ? 'current' : 'pending', detail: '', progress_pct: null,
}));

export function enterMaintenance(pool, body, { now, user }) {
  startDrain(pool, now);
  const m = {
    active: true,
    mode: body.mode,
    message: body.message,
    started_at: iso(now),
    started_by: user,
    target: body.target ? { kind: 'core_upgrade', pack: body.target.pack || 'core', version: body.target.version } : null,
    until: null,
    steps: steps('drain'),
    upgrade_at: null,
    upgraded: false,
    upgrade_applied: false,
  };
  return refreshMaintenance(m, pool, now);
}

// 按时间把排空、升级的进度写回 steps；升级做完后 upgraded=true（调用方切一次 core 默认版本，记 upgrade_applied）
export function refreshMaintenance(m, pool, now) {
  if (!m?.active) return m;
  tickPool(pool, now);
  const cur = m.steps.find((s) => s.state === 'current');
  const drain = m.steps.find((s) => s.id === 'drain');
  if (pool.drain) {
    const left = pool.items.filter((x) => x.finish_at).length;
    const total = pool.drain.online || 0;
    const pct = total ? Math.round(((total - left) / total) * 100) : 100;
    if (drain.state === 'current') {
      drain.progress_pct = pct;
      drain.detail = left
        ? [`剩余 ${left} 个会话`, pool.drain.autobuild ? `${pool.drain.autobuild} 个自主构建已在安全点暂停` : null].filter(Boolean).join(' · ')
        : `已排空 · ${total} 个会话已回收${pool.drain.forced ? `（强制断开 ${pool.drain.forced} 个）` : ''}`;
    }
  }
  const up = m.steps.find((s) => s.id === 'upgrade');
  if (cur?.id === 'upgrade') {
    if (!m.target) {
      up.progress_pct = null;
      up.detail = '这次没有升级目标，直接下一步';
    } else {
      const pct = Math.min(100, Math.round(((now - m.upgrade_at) / UPGRADE_MS) * 100));
      m.upgraded = pct >= 100;
      up.progress_pct = pct;
      up.detail = pct >= 100 ? `已切到 core ${m.target.version}，重新装配完成` : '切换包仓库、重新装配、滚动重启';
    }
  }
  return m;
}

// O-22：当前步骤没完成 → blocked；第 4 步「恢复」不再推进（B3b 第 11 条）
export function advanceMaintenance(m, pool, now) {
  refreshMaintenance(m, pool, now);
  const i = m.steps.findIndex((s) => s.state === 'current');
  const cur = m.steps[i];
  if (!cur) return { error: { step: null, remaining: 0 } };
  if (cur.id === 'resume') return { error: { step: 'resume', remaining: 0 } };
  if (cur.id === 'drain' && (cur.progress_pct ?? 0) < 100) {
    return { error: { step: 'drain', remaining: pool.items.filter((x) => x.finish_at).length } };
  }
  if (cur.id === 'upgrade' && m.target && (cur.progress_pct ?? 0) < 100) return { error: { step: 'upgrade', remaining: 0 } };
  cur.state = 'done';
  const next = m.steps[i + 1];
  next.state = 'current';
  if (next.id === 'upgrade') m.upgrade_at = now;
  refreshMaintenance(m, pool, now);
  return { state: m };
}

export const publicMaintenance = (m) => {
  if (!m?.active) return { active: false };
  const { upgrade_at: _u, upgraded: _g, upgrade_applied: _a, ...rest } = m;
  return { ...rest, steps: m.steps.map((s) => ({ ...s })) };
};

// ?mock=maint:readonly / maint:reject 启动时：排空已经开始了一会儿（与设计稿那一刻一致：剩 3 个、2 个 L4 暂停）
export function maintenanceFromScenario(base, pool, now) {
  const m = enterMaintenance(pool, { mode: base.mode, message: base.message, target: { pack: 'core', version: CORE.beta } }, { now, user: base.started_by || 'ops01' });
  m.started_at = iso(now - 10 * MIN);
  // 正在跑的这几个这一轮还要再跑一会儿
  pool.items.forEach((x, i) => { if (x.finish_at) x.finish_at = now + (i + 1) * 25 * SEC; });
  return refreshMaintenance(m, pool, now);
}

// ---------- 插件运维（O-25） ----------
// rollback_to：这个通道上一个版本（前端「回滚」用；契约 O-25 没有，见 contract-changes F4-B）
export function opsPacks() {
  const p = (name, kind, version, channel, rollout, err, breaker, using, rollbackTo) => ({
    name, kind, version, channel, rollout_pct: rollout, rollout_by: 'project', error_rate_pct: err, breaker, projects_using: using, rollback_to: rollbackTo,
  });
  return [
    p('core', 'capability', CORE.stable, 'stable', 100, 0.6, 'closed', 31, CORE.retired),
    p('core', 'capability', CORE.beta, 'beta', 10, 0.4, 'closed', 3, null),
    p('trace', 'capability', '0.9.1', 'stable', 100, 1.1, 'closed', 22, '0.9.0'),
    p('codegen', 'capability', '0.9.2', 'stable', 100, 2.8, 'closed', 14, '0.9.1'),
    p('industry-retail', 'industry', '1.0.3', 'stable', 100, 12.0, 'open', 9, '1.0.2'),
    p('exporter-otel', 'exporter', '0.2.0', 'stable', 100, 0.0, 'closed', 1, '0.1.5'),
  ];
}

// 半开试探 20 秒没再出错就闭合
export const HALF_OPEN_MS = 20 * SEC;
export function tickBreakers(packs, now) {
  packs.forEach((x) => { if (x.breaker === 'half_open' && x.half_open_at && now - x.half_open_at >= HALF_OPEN_MS) { x.breaker = 'closed'; x.half_open_at = null; } });
}
export const publicPack = ({ half_open_at: _h, ...x }) => x;

// ---------- core 默认版本（O-30～O-32） ----------
export function coreDefault(now) {
  return {
    stable: CORE.stable,
    beta: { version: CORE.beta, rollout_pct: 10 },
    versions: [
      { version: CORE.beta, channel: 'beta', released_at: iso(at(now, 1, 14, 51)), is_default: false },
      { version: CORE.stable, channel: 'stable', released_at: iso(at(now, 20, 10, 20)), is_default: true },
      { version: CORE.retired, channel: 'retired', released_at: iso(at(now, 36, 16, 5)), is_default: false },
    ],
  };
}

// 把 version 设为 stable：原 stable 下线；O-25 里 core 的行跟着变
export function setCoreStable(core, packs, version) {
  const old = core.stable;
  if (old === version) return;
  core.versions.forEach((v) => {
    if (v.version === version) { v.channel = 'stable'; v.is_default = true; } else if (v.version === old) { v.channel = 'retired'; v.is_default = false; } else v.is_default = false;
  });
  core.stable = version;
  if (core.beta?.version === version) core.beta = null;
  const rows = packs.filter((x) => x.name === 'core');
  const target = rows.find((x) => x.version === version);
  const stableRow = rows.find((x) => x.channel === 'stable');
  if (target && stableRow && target !== stableRow) {
    Object.assign(target, { channel: 'stable', rollout_pct: 100, rollback_to: old, projects_using: target.projects_using + stableRow.projects_using });
    packs.splice(packs.indexOf(stableRow), 1);
  } else if (stableRow) Object.assign(stableRow, { version, rollback_to: old, error_rate_pct: 0 });
}

// ---------- 包仓库备份（O-33、O-34） ----------
export function repoBackups(now) {
  return [0, 1, 2].map((d, i) => {
    const t = at(now, d + 1, 2);
    return { name: `packs-${stamp(t)}.tar.gz`, size: [412, 398, 398][i] * MB, created_at: iso(t) };
  });
}
export const REPO_KEEP = 3;

// ---------- 启动自检（O-35；B3b 第 2 条：write_intents 在 index_lock 之后、uncommitted 之前） ----------
export function startupReport(now) {
  const started = now - (2 * HOUR + 40 * MIN);
  return {
    at: iso(started + 11 * SEC),
    last_exit: startupLastExit(started),
    items: [
      { id: 'index_lock', title: '残留 index.lock 1 个', detail: '门店补货单编制 · 确认无 git 进程且锁已存在 41 分钟', result: 'cleared', tone: 'fix' },
      { id: 'write_intents', title: '写入意图 2 条', detail: '上次退出时没走完的写入：网页保存 1 处、MCP 写入 1 处，盘上内容完整', result: 'ok', tone: 'ok' },
      { id: 'uncommitted', title: '未提交改动 2 处', detail: '按写入意图补提交：网页保存 1 处、MCP 写入 1 处', result: 'committed', tone: 'fix' },
      { id: 'assembly', title: '装配目录 38 / 38 一致', detail: '1 个项目哈希不符，已从包仓库重新装配', result: 'ok', tone: 'ok' },
      { id: 'dependencies', title: '依赖 6 / 6 可达', detail: '模型网关、auth-server、沙箱网关、连接器网关、磁盘、git', result: 'ok', tone: 'ok' },
      { id: 'interrupted_execs', title: '中断的执行 1 个', detail: '已标记「在操作提取第 3 步中断」，用户回来时给出续跑卡', result: 'awaiting_user', tone: 'warn' },
    ],
  };
}

// ---------- 备份（O-36～O-39；B2 第 20 条、B3b 第 14 条） ----------
export const BACKUP_SCOPE = ['workspaces', 'session_history', 'session_index', 'config', 'mcp_tokens', 'jiuwen_archives', 'studio_drafts', 'audit', 'cli_transcripts', 'service_tokens', 'releases', 'packs'];
export function backupsData(now) {
  const item = (d, size, full) => {
    const t = at(now, d, 2);
    return { name: `backup-${stamp(t)}.tar.gz`, size: size * GB, created_at: iso(t), scope: full ? 'full' : 'workspace_only', scope_items: full ? [...BACKUP_SCOPE] : ['workspaces'] };
  };
  return {
    schedule: { daily_at: '02:00', interval_hours: null, next_due_at: iso(at(now, -1, 2)), keep: 3, dir_volume: '/backup' },
    scope: [...BACKUP_SCOPE],
    items: [item(0, 14.2, true), item(1, 14.0, true), item(2, 9.6, false)],
  };
}
export function drills(now) {
  return [{ id: 'drill-1028', backup: `backup-${stamp(at(now, 4, 2))}.tar.gz`, at: iso(at(now, 4, 10, 30)), duration_ms: 6 * MIN + 12 * SEC, result: 'passed', detail: '抽检 5 个项目可打开、版本历史完整' }];
}

// ---------- 任务中心（O-40、O-41） ----------
export function tasks(now) {
  const t = (id, type, target, project, initiator, state, pct, started, finished, error = null, extra = {}) => ({
    id, type, target, project: project ? metaView(projectByName(project)) : null, initiator, state, progress_pct: pct,
    started_at: iso(started), finished_at: finished ? iso(finished) : null, error, ...extra,
  });
  const today = (h, m) => at(now, 0, h, m);
  const s1530 = Math.min(now - 14 * MIN, today(15, 30));
  return [
    t('task-e1530', 'export', '门店补货单编制 · 迁移包', '门店补货单编制', 'zhangsan', 'running', 64, s1530, null),
    t('task-e1512', 'export', '快递分拣异常处理 · 迁移包', '快递分拣异常处理', 'wanggong', 'succeeded', 100, Math.min(now - 30 * MIN, today(15, 12)), now - 28 * MIN),
    t('task-b0200', 'backup', '定时备份 · 全量范围', null, 'system', 'succeeded', 100, today(2, 0), today(2, 6)),
    t('task-s0300', 'sweep', '回收站过期项目 2 个', null, 'system', 'succeeded', 100, today(3, 0), today(3, 1)),
    t('task-e1741', 'export', '门店补货单编制 · 知识包 v0.12', '门店补货单编制', 'zhangsan', 'failed', 40, at(now, 1, 17, 41), at(now, 1, 17, 44), '渲染超时'),
  ];
}

// 进行中的任务到点结束；返回结束了的任务（调用方据此落地备份、演练等结果）
export function tickTasks(list, now) {
  const done = [];
  list.forEach((x) => {
    if (x.state === 'running' && x.done_at) {
      if (now >= x.done_at) {
        Object.assign(x, { state: x.done_state || 'succeeded', progress_pct: 100, finished_at: iso(x.done_at) });
        done.push(x);
      } else x.progress_pct = Math.max(x.progress_pct || 0, Math.round(((now - Date.parse(x.started_at)) / (x.done_at - Date.parse(x.started_at))) * 100));
    }
  });
  return done;
}
export const publicTask = ({ done_at: _d, done_state: _s, ...x }) => x;

// ---------- 版本清单（O-42） ----------
export function versions() {
  const v = (category, component, version, note = null) => ({ category, component, version, note });
  return [
    v('kernel', 'onto-modeling', '2.0.0'),
    v('kernel', 'kernel_api', '1.0'),
    v('kernel', 'page_api', '1.0'),
    v('capability', 'core', `${CORE.stable} · ${CORE.beta} beta`),
    v('capability', 'trace', '0.9.1'),
    v('capability', 'codegen', '0.9.2'),
    v('capability', 'diagram', '0.5.0'),
    v('industry', 'industry-retail', '1.0.3'),
    v('industry', 'industry-property', '1.2.0'),
    v('engine', '稳定引擎', '2.1.30'),
    v('engine', '内测引擎', '0.4.1'),
    v('provider', 'convert-pdf · convert-docx · convert-xlsx', '1.0.0'),
    v('data_format', 'ontology-package', 'v0.1'),
    v('data_format', 'operation-centric', 'v1'),
    v('lockfile', 'packs.lock · flow.lock', 'v1'),
  ];
}

// ---------- 数据保留（O-43、O-44） ----------
export function retention(now) {
  const r = (id, label, ret, rule, next, editable = true) => ({ id, label, retention: ret, rule, next_run_at: next == null ? null : iso(next), editable });
  return [
    r('session_history', '会话历史', { days: 180 }, '超期归档到备份后删除', at(now, -1, 3)),
    r('logs', '结构化日志', { days: 30 }, '按天滚动', at(now, -1, 0, 5)),
    r('audit', '审计日志', { days: 365 }, '只追加，不可编辑', null, false),
    r('backups', '备份', { keep_last: 3 }, '滚动覆盖最旧的一份', at(now, -1, 2)),
    r('trash', '回收站', { days: 30 }, '到期永久删除', at(now, -1, 3)),
    r('diagnostics', '诊断包', { days: 7 }, '到期删除', at(now, -1, 3)),
  ];
}
export function retentionProblems(items, patch) {
  const fields = {};
  (patch || []).forEach((p) => {
    const cur = items.find((x) => x.id === p.id);
    if (!cur) { fields[p.id] = 'unknown'; return; }
    if (!cur.editable) { fields[p.id] = 'not_editable'; return; }
    const [k, v] = Object.entries(p.retention || {})[0] || [];
    const lim = k === 'days' ? [1, 3650] : k === 'keep_last' ? [1, 100] : null;
    if (!lim || !Number.isInteger(v) || v < lim[0] || v > lim[1]) fields[p.id] = 'out_of_range';
  });
  return fields;
}

// ---------- 诊断包（O-45～O-47） ----------
export const DIAG_INCLUDE = ['logs_24h', 'config_refs', 'versions', 'recent_errors', 'startup_report', 'pool_alert_snapshot'];
export function diagnostics(now) {
  const t = at(now, 1, 17, 52);
  return [{ id: 'diag-1030', name: `diag-${stamp(t)}.zip`, size: 36 * MB, created_at: iso(t), created_by: 'ops01', expires_at: iso(t + 7 * DAY) }];
}
export function diagSize(include) {
  const per = { logs_24h: 24, config_refs: 1, versions: 1, recent_errors: 6, startup_report: 1, pool_alert_snapshot: 5 };
  return include.reduce((s, k) => s + (per[k] || 0), 0) * MB + 512 * 1024;
}
export const diagName = (now) => `diag-${stamp(now)}.zip`;
export const backupName = (now) => `backup-${stamp(now)}.tar.gz`;
export const repoBackupName = (now) => `packs-${stamp(now)}.tar.gz`;
