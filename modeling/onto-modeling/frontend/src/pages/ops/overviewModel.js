// 运维总览（设计稿 ops）的纯逻辑：数字、金额、大小、时间的格式，按日用量的合计 / 日均 / 峰值，
// 统计卡、告警、运行中的 L4、启动自检的显示，以及柱状图的几何。
// 后端 B2 的口径（contract-changes「B2 · 非项目级路由」第 1、16 条）：calls_today、autobuild.limit、accounts、
// avg_cost.amount 可能为 null，费用币种可能是 USD；这里一律显示「—」而不出错。

const pad = (n) => String(n).padStart(2, '0');
export const DASH = '—';

export function fmtNum(v, digits = 0) {
  if (v == null || Number.isNaN(Number(v))) return DASH;
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: digits });
}

const SYMBOL = { CNY: '¥', USD: '$' };
export const currencyUnit = (currency) => (currency === 'USD' ? '美元' : '元');

// Money → 「¥ 12,846」「$ 3.27」；金额为 null 时「—」
export function fmtMoney(m) {
  if (!m || m.amount == null) return DASH;
  const sym = SYMBOL[m.currency] || m.currency || '';
  const a = Number(m.amount);
  const digits = Math.abs(a) >= 100 || Number.isInteger(a) ? 0 : 2;
  return `${sym} ${fmtNum(a, digits)}`.trim();
}

// 预算消耗列：「¥ 37 / 60」
export function moneyPair(cost, budget) {
  const left = fmtMoney(cost);
  if (!budget || budget.amount == null) return left;
  return `${left} / ${fmtNum(budget.amount, Number.isInteger(Number(budget.amount)) ? 0 : 2)}`;
}

// 字节（十进制）→「412 MB」「1.2 GB」「330 GB」
export function fmtBytes(n) {
  if (n == null) return DASH;
  const units = [['TB', 1e12], ['GB', 1e9], ['MB', 1e6], ['KB', 1e3]];
  for (const [u, k] of units) {
    if (n >= k) {
      const v = n / k;
      return `${v >= 100 ? Math.round(v) : Math.round(v * 10) / 10} ${u}`;
    }
  }
  return `${n} B`;
}

// 时长：「48 分」「1 小时 52 分」「2 小时」「不到 1 分」
export function fmtDur(ms) {
  if (ms == null) return DASH;
  const min = Math.floor(ms / 60000);
  if (min < 1) return '不到 1 分';
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (!h) return `${m} 分`;
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
export const mmdd = (iso) => {
  const m = DATE_ONLY.exec(iso || '');
  if (m) return `${m[2]}-${m[3]}`;
  const d = new Date(iso);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
export const hhmm = (iso) => {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/**
 * 相对时间：刚刚 / N 分钟前 / 今天 09:12 / 昨天 18:40 / 10-29 16:02。
 * short=true 时（项目占用表）更早的只写日期：昨天 / 10-29。
 */
export function relTime(iso, now = Date.now(), { short = false } = {}) {
  if (!iso) return DASH;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return DASH;
  const min = Math.floor((now - t) / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const n = new Date(now);
  const startToday = new Date(n.getFullYear(), n.getMonth(), n.getDate()).getTime();
  if (t >= startToday) return `今天 ${hhmm(iso)}`;
  if (t >= startToday - 86400000) return short ? '昨天' : `昨天 ${hhmm(iso)}`;
  return short ? mmdd(iso) : `${mmdd(iso)} ${hhmm(iso)}`;
}

// ---------- 时间范围 ----------
export const RANGES = [['today', '今天'], ['7d', '7 天'], ['14d', '14 天']];
const RANGE_DAYS = { today: 1, '7d': 7, '14d': 14 };
export const rangeDays = (range) => RANGE_DAYS[range] || 14;
export const rangeSub = (range) => (range === 'today' ? '今天' : `最近 ${rangeDays(range)} 天`);

// ---------- 按日用量 ----------
export const USAGE_KEYS = [['calls', '调用'], ['cost', '成本'], ['tok', '输出 tokens']];

// 某一天在某个分段下的值：调用次数、费用金额、输出 tokens（以万计）
export function usageValue(day, key) {
  if (!day) return 0;
  if (key === 'cost') return Number(day.cost?.amount) || 0;
  if (key === 'tok') return Math.round(((Number(day.output_tokens) || 0) / 1e4) * 10) / 10;
  return Number(day.calls) || 0;
}

export function usageUnit(key, usage) {
  if (key === 'cost') return currencyUnit(usage?.days?.find((d) => d.cost)?.cost?.currency);
  if (key === 'tok') return '万 tokens';
  return '次调用';
}

const VS_FIELD = { calls: 'calls', cost: 'cost', tok: 'output_tokens' };

/**
 * 柱状图上方那一排：合计、日均、峰值（取所选分段的最大值那天）、较前期（服务端给的百分比）。
 * 合计、日均、峰值由 days 现算（与服务端 totals / avg / peak 同口径，峰值按分段各自取）。
 */
export function usageSummary(usage, key, range = '14d') {
  const days = usage?.days || [];
  const values = days.map((d) => usageValue(d, key));
  const total = values.reduce((a, b) => a + b, 0);
  let peakIdx = -1;
  values.forEach((v, i) => { if (peakIdx < 0 || v > values[peakIdx]) peakIdx = i; });
  const n = days.length;
  const vsRaw = usage?.vs_previous_pct?.[VS_FIELD[key]];
  const vs = vsRaw == null ? null : Number(vsRaw);
  const digits = key === 'calls' ? 0 : 1;
  const today = range === 'today';
  return {
    values,
    unit: usageUnit(key, usage),
    totalLabel: today ? '今天合计' : `${rangeDays(range)} 天合计`,
    total: fmtNum(total, key === 'cost' ? 2 : digits),
    avg: n ? fmtNum(total / n, key === 'calls' ? 0 : digits) : DASH,
    peakLabel: peakIdx >= 0 ? `峰值 · ${mmdd(days[peakIdx].date)}` : '峰值',
    peak: peakIdx >= 0 ? fmtNum(values[peakIdx], key === 'cost' ? 2 : digits) : DASH,
    vsLabel: today ? '较昨天' : `较前 ${rangeDays(range)} 天`,
    vs: vs == null ? DASH : `${vs > 0 ? '+' : ''}${fmtNum(vs, 1)}%`,
    vsCls: vs == null ? 'faint' : vs >= 0 ? 'up' : 'down',
    axis: days.map((d) => (n > 1 ? String(Number(mmdd(d.date).slice(3))) : mmdd(d.date))),
  };
}

/**
 * 柱状图几何（设计稿 core.js bars）：每根柱占一格的 62%，最宽 maxBar；最大值顶到 h-14；最矮 2。
 * 返回 [{x, y, w, h, hi}]，hi 是要高亮的那根（今天）。
 */
export function barRects(values, { w = 720, h = 128, hi = -1, maxBar = 32 } = {}) {
  const n = values.length;
  if (!n) return [];
  const mx = Math.max(...values, 0);
  const gap = w / n;
  const bw = Math.min(gap * 0.62, maxBar);
  return values.map((v, i) => {
    const bh = Math.max(2, mx > 0 ? (v / mx) * (h - 14) : 0);
    return { x: i * gap + (gap - bw) / 2, y: h - bh, w: bw, h: bh, hi: i === hi };
  });
}

// ---------- 统计卡 ----------
export function statCards(stats) {
  const s = stats || {};
  const sessions = s.sessions || {};
  const projects = s.projects || {};
  const ab = s.autobuild || {};
  const disk = s.disk || {};
  const cost = s.cost || {};
  return {
    online: { value: fmtNum(s.online_users), desc: `共 ${s.accounts == null ? DASH : fmtNum(s.accounts)} 个账号` },
    sessions: {
      value: fmtNum(sessions.active),
      max: sessions.max == null ? DASH : fmtNum(sessions.max),
      pct: sessions.max ? Math.round(((sessions.active || 0) / sessions.max) * 100) : 0,
      desc: `排队 ${fmtNum(sessions.queued ?? 0)} · 空闲回收 ${sessions.idle_ttl_s == null ? DASH : fmtNum(sessions.idle_ttl_s)} 秒`,
    },
    projects: { value: fmtNum(projects.total), desc: `进行中 ${fmtNum(projects.active)} · 已归档 ${fmtNum(projects.archived)}` },
    autobuild: { value: fmtNum(ab.running ?? 0), desc: `平台上限 ${ab.limit == null ? DASH : fmtNum(ab.limit)} 个` },
    cost: { value: fmtMoney(cost.total), desc: `本月 ${fmtMoney(cost.this_month)}` },
    disk: {
      warn: !!disk.warn,
      value: disk.free_pct == null ? DASH : `${fmtNum(disk.free_pct)}%`,
      small: fmtBytes(disk.free_bytes),
      used: disk.free_pct == null ? 0 : 100 - disk.free_pct,
      desc: disk.warn ? `低于 ${fmtNum(disk.threshold_pct)}% 阈值` : `阈值 ${disk.threshold_pct == null ? DASH : `${fmtNum(disk.threshold_pct)}%`}`,
    },
  };
}

// 建议条：第一句加粗（到第一个句号），按级别取色
export function adviceView(a) {
  const text = a?.text || '';
  const i = text.indexOf('。');
  const tone = a?.level === 'critical' ? 'red' : a?.level === 'info' ? '' : 'amber';
  return {
    tone,
    icon: /磁盘|回收站|备份/.test(text) ? 'hdd' : undefined,
    bold: i >= 0 ? text.slice(0, i + 1) : '',
    rest: i >= 0 ? text.slice(i + 1) : text,
  };
}

// ---------- 告警 ----------
const LEVEL = { critical: ['严重', 'red', 'var(--red)'], warning: ['警告', 'amber', 'var(--amber2)'], info: ['提醒', 'gray', 'var(--ink3)'] };
export const levelChip = (level) => LEVEL[level] || LEVEL.info;

// 告警一行：标题、说明（服务端 detail 加持续时长 / 已自动熔断 / 已恢复时刻）、左侧圆点颜色、右侧标签
export function alertView(a, now = Date.now()) {
  const resolved = a.state === 'resolved';
  const parts = [a.detail].filter(Boolean);
  if (resolved) parts.push(`已恢复${a.resolved_at ? ` · ${hhmm(a.resolved_at)}` : ''}`);
  else if (a.auto_action === 'breaker') parts.push('已自动熔断');
  else if (a.since) parts.push(`持续 ${sinceText(a.since, now)}`);
  const [label, tone, dot] = levelChip(a.level);
  return {
    title: a.name,
    detail: parts.join(' · '),
    dot: resolved ? 'var(--line3)' : dot,
    chip: resolved ? ['已恢复', 'gray'] : [label, tone],
  };
}

export function sinceText(iso, now = Date.now()) {
  const min = Math.max(0, Math.floor((now - Date.parse(iso)) / 60000));
  if (min < 60) return `${Math.max(1, min)} 分钟`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h} 小时`;
  return `${Math.floor(h / 24)} 天`;
}

export const firingCount = (alerts) => (alerts || []).filter((a) => a.state === 'firing').length;

// ---------- 运行中的 L4 ----------
const RUN_STATE = {
  running: { k: 'running', label: '运行中' },
  queued: { tone: 'gray', label: '排队中' },
  pausing: { tone: 'amber', label: '正在暂停' },
  paused: { tone: 'amber', label: '已暂停' },
};
export const runStateView = (state) => RUN_STATE[state] || { tone: 'gray', label: state || DASH };

export function runRow(r) {
  const p = r.progress || {};
  return {
    pid: r.project?.id,
    name: r.project?.name || DASH,
    owner: r.project?.owner || null,
    elapsed: fmtDur(r.elapsed_ms),
    pct: p.pct ?? (p.total ? Math.round(((p.done || 0) / p.total) * 100) : 0),
    slices: p.total ? `${p.done ?? 0} / ${p.total} 片` : DASH,
    budgetPct: r.budget_used_pct ?? 0,
    budget: moneyPair(r.cost, r.budget),
    state: runStateView(r.state),
  };
}

export function autobuildTodayView(t) {
  const x = t || {};
  return [
    ['今日已完成', fmtNum(x.completed ?? 0), '次'],
    ['平均用时', x.avg_duration_ms == null ? DASH : fmtDur(x.avg_duration_ms), ''],
    ['平均成本', fmtMoney(x.avg_cost), ''],
    ['因预算停止', fmtNum(x.stopped_by_budget ?? 0), '次'],
  ];
}

// ---------- 启动自检摘要（与 ops-ctl 的 O-35 同一次自检） ----------
const STARTUP = {
  cleared: ['已清除', 'teal', 'fix'],
  committed: ['已补提交', 'teal', 'fix'],
  reassembled: ['已重建', 'teal', 'fix'],
  ok: ['正常', 'blue', 'ok'],
  awaiting_user: ['待处理', 'amber', 'w'],
  failed: ['失败', 'red', 'w'],
};
export function startupItemView(item) {
  const [label, tone, cls] = STARTUP[item.result] || [item.result || DASH, 'gray', 'w'];
  return { text: item.label || item.title || '', label, tone, cls, icon: cls === 'fix' ? 'wrench' : cls === 'ok' ? 'check' : 'alert' };
}
export const startupAt = (startup) => (startup?.at ? `${mmdd(startup.at)} ${hhmm(startup.at)}` : '');

// ---------- 用户活跃、项目占用 ----------
export const callsText = (n) => (n ? fmtNum(n) : DASH);

// 用户名 → 显示名（项目所有者只给用户名，从用户活跃列表里找；找不到就显示用户名）
export function nameMap(users) {
  const m = new Map();
  (users || []).forEach((u) => m.set(u.username, u.display_name || u.username));
  return (username) => (username ? m.get(username) || username : DASH);
}

// 头像底色：按用户名稳定取 4 种之一
export function avatarIndex(username) {
  let h = 0;
  for (const ch of username || '') h = (h * 31 + ch.charCodeAt(0)) | 0;
  return Math.abs(h) % 4;
}
