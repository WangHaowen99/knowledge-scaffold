// 运维总览与可观测的合成数据（O-01～O-12，含链路检索 O-05）。人名、别人的项目取自 platform-data.js；
// 链路、日志里只有路径、key、ID，不含项目内容（契约 1.4、8.1 #29）。
import { PLATFORM_TOTALS, PLATFORM_USERS, platformProjects } from './platform-data.js';

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const pad = (n) => String(n).padStart(2, '0');
const iso = (t) => new Date(t).toISOString();
const ymd = (t) => {
  const d = new Date(t);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

// ---------- O-01 运维总览 ----------
// 最近 14 天（最后一天是今天）：调用次数、费用（元）、输出 tokens（万）
const USE = {
  calls: [1820, 2104, 1950, 2380, 2210, 980, 760, 2450, 2620, 2510, 2890, 2740, 1210, 2310],
  cost: [412, 468, 431, 540, 505, 210, 168, 566, 602, 571, 655, 630, 262, 516],
  tok: [61, 72, 66, 81, 77, 31, 25, 86, 92, 88, 99, 95, 40, 79],
};
const RANGE_N = { today: 1, '7d': 7, '14d': 14 };
// 14 天的「较前 14 天」没有更早的数据可算，直接给；7 天、今天按上面的数现算
const VS_14D = { calls: 18, cost: 15, output_tokens: 21 };
// B2 口径（?mock=ops:p1）：费用是引擎 SDK 报告的美元数
const USD_RATE = 7.2;

const sum = (a) => a.reduce((x, y) => x + y, 0);
const vsPrev = (arr, n) => {
  const cur = sum(arr.slice(arr.length - n));
  const prev = sum(arr.slice(arr.length - 2 * n, arr.length - n));
  return prev ? Math.round(((cur - prev) / prev) * 1000) / 10 : null;
};

function money(cny, usd) {
  if (cny == null) return { amount: null, currency: usd ? 'USD' : 'CNY' };
  return usd ? { amount: Math.round((cny / USD_RATE) * 100) / 100, currency: 'USD' } : { amount: cny, currency: 'CNY' };
}

export function usageDaily(range = '14d', { usd = false, now = Date.now() } = {}) {
  const n = RANGE_N[range] || 14;
  const days = [];
  for (let i = 14 - n; i < 14; i += 1) {
    days.push({
      date: ymd(now - (13 - i) * DAY),
      calls: USE.calls[i],
      cost: money(USE.cost[i], usd),
      output_tokens: USE.tok[i] * 1e4,
    });
  }
  const totals = { calls: sum(days.map((d) => d.calls)), cost: money(sum(USE.cost.slice(14 - n)), usd), output_tokens: sum(days.map((d) => d.output_tokens)) };
  const peak = days.reduce((p, d) => (d.calls > p.calls ? d : p), days[0]);
  return {
    days,
    totals,
    avg: { calls: Math.round(totals.calls / n), cost: money(Math.round(sum(USE.cost.slice(14 - n)) / n), usd), output_tokens: Math.round(totals.output_tokens / n) },
    peak,
    vs_previous_pct: n === 14 ? VS_14D : { calls: vsPrev(USE.calls, n), cost: vsPrev(USE.cost, n), output_tokens: vsPrev(USE.tok, n) },
  };
}

const metaView = (p) => ({
  id: p.id, name: p.name, owner: p.owner, stage_label: p.stage, build_mode: p.build_mode, archived: p.archived,
  created_at: null, last_activity_at: p.last_activity_at,
});

// 用户活跃：最近活跃的 7 个人（含所有者都在项目占用表里，显示名对得上）
const ACTIVE = [
  ['zhangsan', 0, 412], ['chenjing', 6, 97], ['wanggong', 3, 286], ['liming', 12, 158],
  ['zhaolei', 25, 64], ['sunyu', 41, 31], ['ops01', 0, 0],
];

export function overview({ range = '14d', p1 = false, now = Date.now() } = {}) {
  const projects = platformProjects(now);
  const byId = (id) => metaView(projects.find((p) => p.id === id));
  const users = ACTIVE.map(([username, min, calls]) => {
    const u = PLATFORM_USERS.find((x) => x.username === username);
    return { username, display_name: u.display_name, online: u.online, last_seen_at: iso(now - min * MIN), calls_today: p1 ? null : calls };
  });
  return {
    stats: {
      online_users: PLATFORM_TOTALS.online,
      accounts: p1 ? null : PLATFORM_TOTALS.accounts,
      sessions: { active: 7, max: 12, queued: 1, idle_ttl_s: 600 },
      projects: { total: PLATFORM_TOTALS.projects, active: PLATFORM_TOTALS.active, archived: PLATFORM_TOTALS.archived },
      autobuild: { running: p1 ? 0 : 2, limit: p1 ? null : 4 },
      cost: { total: money(12846, p1), this_month: money(3210, p1) },
      disk: { free_pct: 18, free_bytes: 330e9, threshold_pct: 20, warn: true },
    },
    advice: [
      { level: 'warning', text: '磁盘可用 18%，低于 20% 阈值。建议清理旧备份 / 回收站，或扩容数据盘。回收站里有 4 个项目共 1.6 GB，最早的 5 天后自动清除。' },
      // 回收站的数与用户与审计页（admin-data.js seedTrash：4 个、共约 1.6 GB、最早的 25 天前删除、保留 30 天）一致
    ],
    usage_daily: usageDaily(range, { usd: p1, now }),
    alerts: p1 ? [] : [
      {
        rule_id: 'node-fail-rate', name: '节点失败率过高 · retail.compliance', detail: 'industry-retail@1.0.3 · 近 1 小时失败 12%（阈值 5%）',
        level: 'critical', state: 'firing', since: iso(now - 42 * MIN), resolved_at: null, auto_action: 'breaker',
      },
      {
        rule_id: 'disk-free', name: '磁盘可用低于 20%', detail: '/data', level: 'warning', state: 'firing',
        since: iso(now - 3 * HOUR - 5 * MIN), resolved_at: null, auto_action: null,
      },
      {
        rule_id: 'pool-queue', name: '会话排队过长', detail: '峰值排队 3', level: 'warning', state: 'resolved',
        since: iso(now - 2 * HOUR - 20 * MIN), resolved_at: iso(now - 2 * HOUR), auto_action: null,
      },
    ],
    autobuild_runs: p1 ? [] : [
      {
        project: byId('bike-fleet-7b20e4a1'), run_no: 3, elapsed_ms: 38 * MIN, progress: { done: 2, total: 5, pct: 58 },
        cost: money(25), state: 'running',
      },
      {
        project: byId('parcel-sort-6c1f2a90'), run_no: 1, elapsed_ms: 12 * MIN, progress: { done: 1, total: 4, pct: 25 },
        cost: money(7), state: 'running',
      },
    ],
    autobuild_today: p1
      ? { completed: 0, avg_duration_ms: null, avg_cost: { amount: null, currency: 'USD' } }
      : { completed: 3, avg_duration_ms: 112 * MIN, avg_cost: money(41) },
    // 与 ops-ctl 的 O-35 是同一次自检（ops-ctl-data.js startupReport：进程 2 小时 40 分钟前启动、11 秒后自检完），摘要取四项
    startup: {
      at: iso(now - 2 * HOUR - 40 * MIN + 11 * 1000),
      items: [
        { label: '残留 index.lock 1 个', result: 'cleared' },
        { label: '未提交改动 2 处', result: 'committed' },
        { label: '装配目录 38 / 38 一致', result: 'ok' },
        { label: '依赖 6 / 6 可达', result: 'ok' },
      ],
    },
    users,
    projects: projects.slice(0, 8).map((p) => ({ ...metaView(p), disk_bytes: p.disk_bytes })),
  };
}

// ---------- O-02 指标 ----------
const SERIES = {
  pool: [4, 5, 7, 9, 12, 11, 8, 7, 6, 7, 9, 7],
  turn_duration: [38, 41, 45, 40, 52, 47, 42, 44, 39, 43, 42, 42],
  model_latency_p95: [14, 15, 17, 16, 19, 22, 18, 17, 18, 19, 18, 18],
  output_tokens: [52, 61, 58, 70, 66, 74, 80, 72, 77, 81, 79, 79],
  model_fail_rate: [0.4, 0.5, 0.6, 0.5, 1.2, 0.9, 0.7, 0.6, 0.8, 0.7, 0.8, 0.8],
  guard_denies: [2, 3, 1, 4, 2, 6, 3, 2, 1, 2, 3, 23],
  check_failures: [5, 4, 6, 3, 7, 4, 3, 2, 4, 1, 1, 1],
  merges: [12, 18, 15, 21, 19, 14, 9, 16, 22, 17, 20, 3],
  collections: [0, 1, 0, 0, 2, 0, 1, 0, 0, 1, 0, 1],
  reconnects: [0, 0, 3, 0, 0, 1, 0, 6, 0, 2, 0, 2],
};
// 时间范围、公共标签对计数类指标的缩放（演示联动）
const RANGE_SCALE = { '1h': 0.06, '24h': 1, '7d': 6.5 };
const card = (id, label, value, unit, secondary, note, tone = 'normal') => ({ id, label, value, unit, secondary, note, series: SERIES[id], tone });

export function metrics({ range = '24h', labels = '', p1 = false } = {}) {
  const k = (RANGE_SCALE[range] ?? 1) * (labels ? 0.12 : 1);
  const n = (v) => Math.max(0, Math.round(v * k));
  if (p1) {
    // B3b 后端的形状：value 是数、unit 是计量单位；合入、证据采集恒为 0 并带 note。
    // 合入按 I1 的口径带 unavailable（变更集合入还没计数，前端不画这张卡，R18）
    return {
      cards: [
        card('pool', '会话池与排队', 7, '个', '上限 12 · 排队 1', '', 'warn'),
        card('turn_duration', '轮次时长 p95', 190, '秒', '', ''),
        card('model_latency_p95', '模型耗时 p95', 18.4, '秒', '', ''),
        card('output_tokens', '输出 tokens', n(790000), 'tokens', '', ''),
        card('model_fail_rate', '模型失败', 0.8, '%', `${n(3)} 次`, ''),
        card('guard_denies', '守卫拒绝', n(23), '次', '', ''),
        card('check_failures', '检查失败', n(41), '次', '', ''),
        { ...card('merges', '合入', 0, '次', '', '变更集在 P2'), series: Array(12).fill(0), unavailable: true },
        { ...card('collections', '证据采集', 0, '次', '', '业务系统的数据接入在 P3'), series: Array(12).fill(0) },
        card('reconnects', '热更新与重连', n(14), '次', '', ''),
      ],
    };
  }
  return {
    cards: [
      card('pool', '会话池与排队', '7 / 12', null, '峰值排队 3', '排队平均等待 38 秒'),
      card('turn_duration', '轮次时长', 42, '秒', 'p95 3 分 10 秒', 'p50 / p95'),
      card('model_latency_p95', '模型耗时 p95', 18.4, '秒', '', '按网关、模型拆分'),
      card('output_tokens', '输出 tokens', n(790000), 'tokens', range === '24h' ? '今日' : '', '输入 412 万 · 缓存命中 63%'),
      card('model_fail_rate', '模型失败', 0.8, '%', '', '超时 61% · 限流 39%'),
      card('guard_denies', '守卫拒绝', n(23), '次', '', 'resource_write_guard 占 70%'),
      card('check_failures', '检查失败', n(41), '次', '', '写入即查 34 · 门禁链 7'),
      card('merges', '合入', n(186), '次', '', '自动合入 74%（单人 + L4）'),
      card('collections', '证据采集', n(6), '次', '', '成功 4 · 部分完成 1 · 失败 1'),
      card('reconnects', '热更新与重连', n(14), '次', '', '失败 0 · 平均重连 2.3 秒'),
    ],
  };
}

// ---------- O-03 归因 ----------
const ATTR = {
  pack: [
    ['core', '1.4.2', 6284, 0.6, 38000, 14, null, 82],
    ['trace', '0.10.0', 1102, 1.1, 52000, 2, 3, 76],
    ['codegen', '0.9.2', 214, 2.8, 106000, 0, 9, 61],
    ['diagram', '0.6.0', 186, 0.5, 24000, 1, null, 88],
    ['reverse', '0.3.0', 96, 3.1, 130000, 5, 4, 58],
    ['industry-retail', '1.0.3', 312, 12.0, 11000, 0, 27, null],
  ],
  node: [
    ['core.research', 'core@1.4.2', 148, 0.7, 252000, 2, 6, 79],
    ['core.model', 'core@1.4.2', 402, 0.5, 68000, 3, 11, 84],
    ['core.metric', 'core@1.4.2', 366, 0.8, 91000, 5, 14, 77],
    ['core.validate', 'core@1.4.2', 591, 0.3, 46000, 4, null, null],
    ['trace.trace', 'trace@0.10.0', 684, 1.2, 58000, 2, 3, 76],
    ['retail.compliance', 'industry-retail@1.0.3', 212, 12.0, 11000, 0, 27, null],
  ],
};

export function attribution({ by = 'pack', p1 = false } = {}) {
  const rows = ATTR[by] || ATTR.pack;
  return {
    items: rows.map(([key, ver, calls, fail, ms, guards, reruns, accept]) => (p1
      // B3b：key 是样本标签原值（包带版本），version_or_source 取 @ 后面；reruns、first_accept_pct 为 null
      ? { key: by === 'pack' ? `${key}@${ver}` : key, version_or_source: by === 'pack' ? ver : null, calls, fail_rate_pct: fail, avg_duration_ms: ms, guard_denies: guards, reruns: null, first_accept_pct: null }
      : { key, version_or_source: ver, calls, fail_rate_pct: fail, avg_duration_ms: ms, guard_denies: guards, reruns, first_accept_pct: accept })),
  };
}

// ---------- O-04、O-05 链路 ----------
const BASE_LABELS = { project: 'retail-restock', session: 'c7a1', exec: 'ex-0412', changeset: 'cs-31', pack: 'core@1.4.2', node: 'core.operation@1.4.2' };

// 设计稿那条链路：用户消息 → 轮次 → 子代理 → 工具 / 脚本 → 提交 → 检查（总计 84.3 秒）
const MAIN_SPANS = [
  ['s1', null, '用户消息', 'message', 0, 200],
  ['s2', 's1', '轮次 · core.operation', 'turn', 340, 84100],
  ['s3', 's2', '模型调用 ×6', 'model', 840, 71300],
  ['s4', 's2', '子代理 extract-operation-agent', 'subagent', 3370, 58900],
  ['s5', 's4', '工具 Read 02-门店补货规则.docx', 'tool', 5060, 2100],
  ['s6', 's4', '脚本 source_extract.py', 'script', 8430, 400],
  ['s7', 's4', '守卫 resource_write_guard（拒绝 1 次）', 'guard', 17700, 100, 'denied'],
  ['s8', 's2', '脚本 ontology_checker.py', 'script', 62400, 3200],
  ['s9', 's2', '提交 · 写入路径 cs-31', 'commit', 72500, 600],
  ['s10', 's2', '检查 · 门禁链 4 条', 'check', 77600, 400],
  ['s11', 's2', 'flow.updated → 待确认', 'event', 83000, 0],
];

// 出错的那条：合规检查节点读规则时报错（ops-users 的错误留档会跳过来）
const FAIL_SPANS = [
  ['s1', null, '用户消息', 'message', 0, 150],
  ['s2', 's1', '轮次 · retail.compliance', 'turn', 300, 11400, 'error'],
  ['s3', 's2', '模型调用 ×2', 'model', 600, 6800],
  ['s4', 's2', '脚本 compliance_check.py', 'script', 7600, 2300, 'error'],
  ['s5', 's2', '检查 · 门禁链 1 条', 'check', 10100, 900, 'error'],
  ['s6', 's2', 'pack.breaker.open', 'event', 11500, 0],
];

const span = ([id, parent_id, name, kind, start_offset_ms, duration_ms, status = 'ok']) => ({
  id, parent_id, name, kind, start_offset_ms, duration_ms, status, attrs: {},
});

const projectKey = (id) => (id || '').replace(/-[0-9a-f]{8}$/, '');

// 链路检索的全部（最近 24 小时左右）：[id, 几分钟前, 总耗时 ms, 入口, 项目, 用户, 节点, 状态]
const TRACE_ROWS = [
  ['4f9c2e1d', 3, 84300, '轮次 · core.operation', 'retail-restock-3f9a1c2d', 'zhangsan', 'core.operation@1.4.2', 'denied'],
  ['a83d0b52', 9, 11500, '轮次 · retail.compliance', 'canteen-menu-4a9c1e08', 'sunyu', 'retail.compliance@1.0.3', 'error'],
  ['7c21e9f0', 14, 46200, '轮次 · core.object', 'parcel-sort-6c1f2a90', 'wanggong', 'core.object@1.4.2', 'ok'],
  ['d05b7a13', 22, 31800, '轮次 · trace.trace', 'clinic-queue-9e3b7d21', 'liming', 'trace.trace@0.9.1', 'ok'],
  ['19e4c6ab', 37, 52700, '轮次 · core.research', 'hotel-room-2d8a4c55', 'chenjing', 'core.research@1.4.2', 'ok'],
  ['b6f8023e', 58, 12400, '轮次 · core.validate', 'bike-fleet-7b20e4a1', 'zhangsan', 'core.validate@1.4.2', 'ok'],
  ['5a3e91d7', 85, 73900, '轮次 · core.operation', 'gym-member-7f04b6e3', 'zhaolei', 'core.operation@1.4.2', 'denied'],
  ['e2c470b9', 130, 9100, '轮次 · retail.compliance', 'canteen-menu-4a9c1e08', 'sunyu', 'retail.compliance@1.0.3', 'error'],
  ['3d9f5e21', 210, 28800, '轮次 · core.object', 'park-lot-8b2e5f71', 'zhoumin', 'core.object@1.4.2', 'ok'],
  ['8b1a6c04', 340, 41600, '轮次 · core.operation', 'retail-restock-3f9a1c2d', 'zhangsan', 'core.operation@1.4.2', 'ok'],
  ['c4e7d2a8', 620, 19700, '轮次 · trace.trace', 'school-bus-3e7d9a46', 'wuhao', 'trace.trace@0.9.1', 'ok'],
  ['f17b3e65', 1500, 66400, '轮次 · core.research', 'clinic-queue-9e3b7d21', 'liming', 'core.research@1.4.2', 'ok'],
  ['2e8c0f94', 3 * 1440, 22100, '轮次 · core.validate', 'parcel-sort-6c1f2a90', 'wanggong', 'core.validate@1.4.2', 'ok'],
];

function labelsOf(row) {
  const [id, , , , pid, user, node] = row;
  if (id === '4f9c2e1d') return { ...BASE_LABELS, user };
  const [nodeName, ver] = node.split('@');
  const pack = nodeName.startsWith('retail.') ? `industry-retail@${ver}` : `${nodeName.split('.')[0]}@${ver}`;
  return { project: projectKey(pid), session: id.slice(0, 4), exec: `ex-${id.slice(4, 8)}`, changeset: null, pack, node, user };
}

export function traceList(now = Date.now()) {
  return TRACE_ROWS.map((row) => ({
    trace_id: row[0], started_at: iso(now - row[1] * MIN), total_ms: row[2], root_name: row[3], labels: labelsOf(row), status: row[7],
  }));
}

// 没列出来的十六进制 ID 也合成一条（别的页面带过来的 trace_id 都能打开）；其余返回 null（trace_not_found）
export function traceDetail(traceId, now = Date.now()) {
  const id = String(traceId || '').toLowerCase();
  const strip = ({ user, ...rest }) => rest;
  if (id === '4f9c2e1d') return { trace_id: id, total_ms: 84300, labels: BASE_LABELS, spans: MAIN_SPANS.map(span) };
  const listed = traceList(now).find((t) => t.trace_id === id);
  if (listed?.status === 'error') return { trace_id: id, total_ms: listed.total_ms, labels: strip(listed.labels), spans: FAIL_SPANS.map(span) };
  if (!/^[0-9a-f]{6,32}$/.test(id)) return null;
  const total = listed?.total_ms || 40000;
  const k = total / 84300;
  // 合成的链路不带具体资料名与变更集（别的项目的内容运维看不到，这里只放通用的路径）
  const NAME = { s2: listed?.root_name, s5: '工具 Read 01-场景说明.docx', s9: '提交 · 写入路径' };
  const spans = MAIN_SPANS.filter((s) => s[3] !== 'guard' || listed?.status === 'denied').map((s) => span([
    s[0], s[1], NAME[s[0]] || s[2], s[3], Math.round(s[4] * k), Math.round(s[5] * k), s[6],
  ]));
  const labels = listed ? strip(listed.labels) : { ...BASE_LABELS, session: id.slice(0, 4), exec: `ex-${id.slice(4, 8)}`, changeset: null };
  return { trace_id: id, total_ms: total, labels, spans };
}

// ---------- O-06、O-07 日志 ----------
// [距最后一条的毫秒, 级别, 事件, 字段]
const LOGS = [
  [-104586, 'INFO', 'session.turn.start', { project: 'retail-restock', session: 'c7a1', exec: 'ex-0412', node: 'core.operation@1.4.2', trace: '4f9c2e1d' }],
  [-104559, 'INFO', 'assembly.fingerprint.ok', { project: 'retail-restock', pack: 'core@1.4.2', hash: 'b41e09' }],
  [-100884, 'INFO', 'subagent.start', { agent: 'extract-operation-agent', exec: 'ex-0412', trace: '4f9c2e1d' }],
  [-86672, 'WARN', 'guard.deny', { guard: 'resource_write_guard', path: 'resource/others/02-门店补货规则.docx', pack: 'core@1.4.2', trace: '4f9c2e1d' }],
  [-53238, 'INFO', 'script.run', { script: 'source_extract.py', anchors: '12', ms: '412', trace: '4f9c2e1d' }],
  [-21770, 'INFO', 'write.commit', { source: 'agent', changeset: 'cs-31', node: 'core.operation@1.4.2', files: '2', trace: '4f9c2e1d' }],
  [-21402, 'INFO', 'gate.chain.pass', { gates: '4', changeset: 'cs-31', trace: '4f9c2e1d' }],
  [-20786, 'INFO', 'flow.updated', { project: 'retail-restock', node: 'core.model', state: 'pending' }],
  [-19, 'ERROR', 'node.fail', { project: 'canteen-menu', node: 'retail.compliance', pack: 'industry-retail@1.0.3', err: 'RuleSchemaError', trace: 'a83d0b52' }],
  [0, 'WARN', 'pack.breaker.open', { pack: 'industry-retail@1.0.3', node: 'retail.compliance', reason: 'fail_rate>5%' }],
];

// 后端 B3b 的日志另有 DEBUG 与 message（脱敏后的正文）
const LOGS_P1_EXTRA = [
  [-60000, 'DEBUG', 'pool.tick', { active: '7', queued: '1', message: 'pool state refreshed' }],
  [-30000, 'INFO', 'auth.refresh', { user: 'zhangsan', message: 'token=•••• refreshed' }],
];

// 返回新的在前（与后端 read_logs 的顺序一致）
export function logs({ p1 = false, now = Date.now() } = {}) {
  const last = now - 30 * 1000;
  const rows = p1 ? [...LOGS, ...LOGS_P1_EXTRA] : LOGS;
  return rows
    .map(([dt, level, event, fields]) => ({ ts: iso(last + dt), level, event, fields }))
    .sort((a, b) => b.ts.localeCompare(a.ts));
}

// 跟随时陆续出现的日志（循环）
const LIVE = [
  ['INFO', 'session.turn.start', (n) => ({ project: 'retail-restock', session: 'c7a1', exec: `ex-04${13 + n}`, node: 'core.validate@1.4.2', trace: `9d${(0x3a10 + n).toString(16)}e7` })],
  ['INFO', 'script.run', (n) => ({ script: 'ontology_checker.py', ms: String(380 + n * 7), trace: `9d${(0x3a10 + n).toString(16)}e7` })],
  ['INFO', 'pool.queue.dequeue', () => ({ project: 'parcel-sort', waited_ms: '3800' })],
  ['WARN', 'guard.deny', (n) => ({ guard: 'resource_write_guard', path: 'resource/others/05-门店清单.xlsx', pack: 'core@1.4.2', trace: `9d${(0x3a10 + n).toString(16)}e7` })],
  ['INFO', 'write.commit', () => ({ source: 'agent', changeset: 'cs-32', node: 'core.validate@1.4.2', files: '1' })],
  ['INFO', 'flow.updated', () => ({ project: 'retail-restock', node: 'core.validate', state: 'running' })],
];

export function liveLog(n, now = Date.now()) {
  const [level, event, fields] = LIVE[n % LIVE.length];
  return { ts: iso(now), level, event, fields: fields(Math.floor(n / LIVE.length)) };
}

// ---------- O-08～O-10 告警规则 ----------
export function alertRules() {
  return [
    {
      id: 'node-fail-rate', name: '节点失败率过高', condition: 'node.fail_rate > 5%', window: '1 小时', level: 'critical', state: 'firing', enabled: true,
      source: 'industry-retail@1.0.3', auto_action: 'breaker',
      runbook_md: '熔断出错的包，再查失败原因。\n\n1. 在「运维管控与健康」确认熔断已生效，受影响的项目会改用上一版本。\n2. 在「用户与审计」的错误留档里按节点筛，看脱敏后的原始错误。\n3. 联系包的维护者修复后，解除熔断。',
    },
    {
      id: 'disk-free', name: '磁盘可用低于 20%', condition: 'disk.free_ratio < 0.2', window: '30 分钟', level: 'warning', state: 'firing', enabled: true,
      source: 'kernel', auto_action: null,
      runbook_md: '清理备份与回收站。\n\n1. 在「运维管控与健康」删掉超出保留份数的旧备份。\n2. 在「用户与审计」清空回收站里已确认不要的项目。\n3. 仍不够时扩容数据盘。',
    },
    {
      id: 'pool-queue', name: '会话排队过长', condition: 'pool.queue_len ≥ 3', window: '10 分钟', level: 'warning', state: 'ok', enabled: true,
      source: 'kernel', auto_action: null, runbook_md: '调整并发上限或排空。\n\n会话池满了以后新会话排队；可临时调高并发上限，或排空空闲会话。',
    },
    {
      id: 'model-fail', name: '模型网关失败率', condition: 'model.fail_rate > 3%', window: '15 分钟', level: 'critical', state: 'ok', enabled: true,
      source: 'kernel', auto_action: null, runbook_md: '切换网关 / 联系供应商。\n\n先看失败是超时还是限流；限流时降低并发，超时时切换到备用网关。',
    },
    {
      id: 'guard-spike', name: '守卫拒绝突增', condition: 'guard.deny 环比 > 300%', window: '', level: 'info', state: 'ok', enabled: true,
      source: 'kernel', auto_action: null, runbook_md: '检查包是否越界写入。\n\n按包归因看是哪个包的拒绝变多，再看守卫日志里的路径。',
    },
    {
      id: 'collect-fail', name: '采集任务失败', condition: 'evidence.job.failed ≥ 2', window: '1 小时', level: 'warning', state: 'ok', enabled: true,
      source: 'reverse@0.3.0', auto_action: null, runbook_md: '查看连接器网关日志。\n\n多数是凭据过期或不在访问时间窗口内。',
    },
    {
      id: 'reconnect-fail', name: '热更新重连失败', condition: 'assembly.reconnect.error > 0', window: '', level: 'critical', state: 'ok', enabled: true,
      source: 'kernel', auto_action: null, runbook_md: '回滚锁文件。\n\n在项目设置里把最近一次升级的包回滚到上一版本，再让会话重连。',
    },
  ];
}

// ---------- O-11、O-12 导出器 ----------
export function exporters() {
  return [
    { id: 'otel', label: 'OpenTelemetry（OTLP）', implementation: 'exporter-otel@0.2.0', target: 'grpc://otel-collector.example.invalid:4317', signals: ['logs', 'metrics', 'traces'], enabled: true, configured: true },
    { id: 'prometheus', label: 'Prometheus', implementation: 'builtin', target: '/metrics · 抓取间隔 15 秒', signals: ['metrics'], enabled: true, configured: true },
    { id: 'file', label: '本地文件', implementation: 'builtin', target: '/data/telemetry · 按天滚动，保留 7 天', signals: ['logs', 'traces'], enabled: true, configured: true, required: true },
    { id: 'custom', label: '外部监控平台', implementation: 'exporter-custom', target: null, signals: [], enabled: false, configured: false },
  ];
}
