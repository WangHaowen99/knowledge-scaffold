// 可观测（设计稿 ops-obs）的纯逻辑：公共标签、指标卡、归因表、链路瀑布、结构化日志、告警规则、导出器、链路检索。
// 后端口径（contract-changes B3b 第 6～8 条）：指标的 value 是数、unit 是「个 / 秒 / tokens / % / 次」；
// merges 恒为 0 时带 note（底注是后端下发的说明，前端不画）；归因的 reruns、first_accept_pct 为 null；日志 level 另有 DEBUG。
import { DASH, fmtNum } from './overviewModel.js';

const pad = (n, w = 2) => String(n).padStart(w, '0');

// ---------- 时间范围、公共标签 ----------
export const OBS_RANGES = [['1h', '1 小时'], ['24h', '24 小时'], ['7d', '7 天']];
export const RANGE_MS = { '1h': 3600e3, '24h': 86400e3, '7d': 7 * 86400e3 };

// 公共标签的键（契约 O-04 labels）与界面叫法；user 只在链路检索里用
export const LABEL_KEYS = [
  ['project', '项目'], ['session', '会话'], ['exec', '执行'], ['changeset', '变更集'],
  ['pack', '包版本'], ['node', '节点'], ['trace', 'trace id'],
];
const LABEL_NAME = Object.fromEntries(LABEL_KEYS);
const LABEL_ALIAS = Object.fromEntries([
  ...LABEL_KEYS.map(([k, t]) => [t, k]), ['trace_id', 'trace'], ['包', 'pack'], ['用户', 'user'], ['user', 'user'],
]);
export const labelName = (key) => LABEL_NAME[key] || (key === 'user' ? '用户' : key);

/**
 * 「添加标签」输入框里的一段：key=value，key 可写中文叫法（项目=、包版本=）。
 * 返回 {key, value} 或 {error}。
 */
export function parseLabelInput(text, { allowUser = false } = {}) {
  const raw = (text || '').trim();
  const i = raw.indexOf('=');
  if (i <= 0 || i === raw.length - 1) return { error: '写成 键=值，比如 pack=core@1.4.2' };
  const k = raw.slice(0, i).trim();
  const key = LABEL_NAME[k] ? k : LABEL_ALIAS[k];
  const value = raw.slice(i + 1).trim();
  if (!key || (key === 'user' && !allowUser)) return { error: `不认识的标签「${k}」，可用：${LABEL_KEYS.map(([x]) => x).join('、')}` };
  if (/[,\s]/.test(value)) return { error: '值里不能有逗号和空格' };
  return { key, value };
}

// 同一个键只留一个（后加的替换先加的），保持顺序
export function addLabel(tags, tag) {
  const rest = (tags || []).filter((t) => t.key !== tag.key);
  const at = (tags || []).findIndex((t) => t.key === tag.key);
  if (at < 0) return [...rest, tag];
  rest.splice(at, 0, tag);
  return rest;
}

// 查询参数：project=…,pack=core@1.4.2（契约 O-02 labels）
export const labelsQuery = (tags) => (tags || []).map((t) => `${t.key}=${t.value}`).join(',');

export function parseLabelsQuery(s) {
  const out = {};
  (s || '').split(',').forEach((part) => {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  });
  return out;
}

// 链路的 labels → 标签列表（空值跳过）；节点带版本时一起显示
export function traceTags(labels) {
  return LABEL_KEYS.filter(([k]) => k !== 'trace' && labels?.[k]).map(([key]) => ({ key, value: labels[key] }));
}

// ---------- 指标卡（O-02） ----------
const METRIC_ICON = {
  pool: 'msg', turn_duration: 'clock', model_latency_p95: 'sparkle', output_tokens: 'zap', model_fail_rate: 'alert',
  guard_denies: 'shield', check_failures: 'shieldok', merges: 'merge', reconnects: 'refresh',
};
const RED = { color: '#DC2626', fill: 'rgba(220,38,38,.08)' };
const AMBER = { color: '#F59E0B', fill: 'rgba(245,158,11,.10)' };
const METRIC_COLOR = { model_fail_rate: RED, guard_denies: AMBER, check_failures: AMBER };

// 秒数 → 「42 秒」「3 分 10 秒」
export function fmtSeconds(s) {
  if (s == null || Number.isNaN(Number(s))) return DASH;
  const v = Number(s);
  if (v < 60) return `${fmtNum(v, 1)} 秒`;
  const m = Math.floor(v / 60);
  const r = Math.round(v - m * 60);
  return r ? `${m} 分 ${r} 秒` : `${m} 分`;
}

// 大数 → 「79 万」「1.2 亿」
export function fmtCount(n) {
  if (n == null || Number.isNaN(Number(n))) return DASH;
  const v = Number(n);
  if (v >= 1e8) return `${fmtNum(v / 1e8, 1)} 亿`;
  if (v >= 1e4) return `${fmtNum(v / 1e4, v >= 1e5 ? 0 : 1)} 万`;
  return fmtNum(v, 1);
}

/**
 * 指标卡的显示：大字（数值带单位）、小字（secondary，没有时给计数单位）、迷你折线颜色。
 * value 是字符串时原样显示（mock 的「7 / 12」）。
 */
// O-02 的卡片哪些画：没有数据来源的（后端不返回或带 unavailable）不画
export function visibleMetricCards(cards) {
  return (cards || []).filter((c) => c && !c.unavailable && c.available !== false);
}

export function metricView(card) {
  const { value, unit, secondary } = card;
  let big;
  let small = secondary || '';
  if (typeof value === 'string') big = unit && !['次', '个'].includes(unit) ? `${value} ${unit}` : value;
  else if (unit === '秒') big = fmtSeconds(value);
  else if (unit === '%') big = `${fmtNum(value, 1)}%`;
  else if (unit === 'tokens') big = fmtCount(value);
  else big = fmtNum(value, 1);
  if (!small && (unit === '次' || unit === '个') && value != null) small = unit;
  const color = METRIC_COLOR[card.id] || { color: '#2563EB', fill: 'rgba(37,99,235,.10)' };
  return {
    id: card.id,
    icon: METRIC_ICON[card.id] || 'activity',
    label: card.label,
    big: value == null ? DASH : big,
    small,
    series: Array.isArray(card.series) ? card.series : [],
    tone: card.tone === 'error' ? 'var(--red)' : card.tone === 'warn' ? 'var(--amber)' : undefined,
    ...color,
  };
}

/** 迷你折线几何（设计稿 core.js spark）：返回折线与填充区域的 path */
export function sparkPath(points, { w = 180, h = 30 } = {}) {
  const pts = (points || []).map((v) => Number(v) || 0);
  if (pts.length < 2) return null;
  const mx = Math.max(...pts);
  const mn = Math.min(...pts);
  const k = (w - 4) / (pts.length - 1);
  const xy = pts.map((v, i) => [2 + i * k, h - 3 - ((v - mn) / ((mx - mn) || 1)) * (h - 6)]);
  const line = xy.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  return { line, area: `${line} L ${w - 2} ${h} L 2 ${h} Z` };
}

// ---------- 按包 / 按节点归因（O-03） ----------
export function fmtMs(ms) {
  if (ms == null) return DASH;
  return fmtSeconds(ms / 1000);
}

// 归因表可选列：整列都是空值（后端还没有数据来源）时不出这一列（R18）
export const ATTRIBUTION_OPTIONAL = [['reruns', '重跑'], ['first_accept_pct', '首次接受率']];
export function attributionColumns(items) {
  const list = items || [];
  return ATTRIBUTION_OPTIONAL.filter(([k]) => list.some((it) => it?.[k] != null)).map(([k]) => k);
}

export function attributionRow(item) {
  const [name, ver] = String(item.key || '').split('@');
  const pct = item.first_accept_pct;
  return {
    name: name || DASH,
    version: item.version_or_source || ver || DASH,
    calls: fmtNum(item.calls),
    fail: item.fail_rate_pct == null ? DASH : `${Number(item.fail_rate_pct).toFixed(1)}%`,
    failHigh: (item.fail_rate_pct ?? 0) >= 5,
    duration: fmtMs(item.avg_duration_ms),
    guards: fmtNum(item.guard_denies),
    reruns: item.reruns == null ? DASH : fmtNum(item.reruns),
    accept: pct == null ? null : Math.round(pct),
  };
}

// ---------- 链路瀑布（O-04） ----------
const SPAN_ICON = {
  message: 'msg', turn: 'flow', model: 'sparkle', subagent: 'bot', tool: 'filetext', script: 'terminal',
  guard: 'shield', commit: 'merge', check: 'shieldok', event: 'branch',
};
const SPAN_CLS = { subagent: 'sub', tool: 'tool', script: 'tool', check: 'tool', guard: 'wr', commit: 'wr' };
export const MIN_BAR_PCT = 0.6;

/**
 * 瀑布每一行：图标、条的颜色类、位置（left / width，占总时长的百分比）、右侧耗时。
 * 条最窄 0.6%，超出右边界时往左收。
 */
export function waterfallRows(trace) {
  const total = Number(trace?.total_ms) || 0;
  const spans = [...(trace?.spans || [])].sort((a, b) => (a.start_offset_ms || 0) - (b.start_offset_ms || 0));
  return spans.map((s) => {
    let left = total ? ((s.start_offset_ms || 0) / total) * 100 : 0;
    let width = total ? ((s.duration_ms || 0) / total) * 100 : 0;
    left = Math.min(100, Math.max(0, left));
    width = Math.max(MIN_BAR_PCT, Math.min(width, 100 - left));
    if (left + width > 100) left = 100 - width;
    return {
      id: s.id,
      name: s.name,
      icon: SPAN_ICON[s.kind] || 'dot',
      cls: s.status === 'error' ? 'err' : s.status === 'denied' ? 'wr' : SPAN_CLS[s.kind] || '',
      left: Math.round(left * 100) / 100,
      width: Math.round(width * 100) / 100,
      ms: `${((s.duration_ms || 0) / 1000).toFixed(1)}s`,
      status: s.status,
    };
  });
}

export const traceTotalText = (trace) => (trace ? `trace ${trace.trace_id} · 总计 ${((trace.total_ms || 0) / 1000).toFixed(1)} 秒` : '');

// 「没带参数时显示最近一条」：O-05 第一页里开始时间最晚的那条
export function latestTrace(items) {
  let best = null;
  (items || []).forEach((t) => { if (!best || Date.parse(t.started_at) > Date.parse(best.started_at)) best = t; });
  return best;
}

// ---------- 结构化日志（O-06、O-07） ----------
export const LOG_LEVELS = [['all', '全部'], ['INFO', 'INFO'], ['WARN', 'WARN'], ['ERROR', 'ERROR']];
const LEVEL_CLS = { INFO: 'I', WARN: 'W', WARNING: 'W', ERROR: 'E', DEBUG: '' };
export const levelCls = (level) => LEVEL_CLS[String(level || '').toUpperCase()] ?? '';
export const levelParam = (seg) => (seg && seg !== 'all' ? seg : '');

// ISO 时间 → 「10:42:18.204」（本地时间）
export function logTime(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts || '');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

// 字段按 k=v 排开；message（脱敏后的正文）放最后
export function logFields(fields) {
  const entries = Object.entries(fields || {});
  const msg = entries.filter(([k]) => k === 'message');
  return [...entries.filter(([k]) => k !== 'message'), ...msg].map(([k, v]) => ({ k, v: String(v ?? ''), trace: k === 'trace' || k === 'trace_id' }));
}

/**
 * 一条日志是否命中筛选（与后端 _log_filter 同口径）：
 * level 多值逗号分隔；labels 逐个相等；q 按「 OR 」拆开，任一命中即可——带 = 的比字段，不带的在整条里找。
 */
export function matchLog(entry, { level = '', q = '', labels = '' } = {}) {
  const levels = String(level).split(',').map((x) => x.trim().toUpperCase()).filter(Boolean);
  if (levels.length && !levels.includes(String(entry.level).toUpperCase())) return false;
  const fields = entry.fields || {};
  const wanted = typeof labels === 'string' ? parseLabelsQuery(labels) : labels || {};
  if (Object.entries(wanted).some(([k, v]) => String(fields[k] ?? '') !== String(v))) return false;
  const alts = String(q || '').split(' OR ').map((x) => x.trim()).filter(Boolean);
  if (!alts.length) return true;
  const text = JSON.stringify(entry).toLowerCase();
  return alts.some((alt) => {
    const i = alt.indexOf('=');
    if (i > 0) return String(fields[alt.slice(0, i).trim()] ?? '') === alt.slice(i + 1).trim();
    return text.includes(alt.toLowerCase());
  });
}

export const sortLogs = (items) => [...(items || [])].sort((a, b) => String(a.ts).localeCompare(String(b.ts)));

export const MAX_LOG_LINES = 500;
// 跟随时追加一条：按 ts 去重，最多留 500 条
export function appendLog(list, entry) {
  if (list.some((x) => x.ts === entry.ts && x.event === entry.event)) return list;
  const next = [...list, entry];
  return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next;
}

// ---------- 告警规则（O-08～O-10） ----------
export const RULE_LEVELS = [['critical', '严重'], ['warning', '警告'], ['info', '提醒']];
const RULE_LEVEL_TONE = { critical: 'red', warning: 'amber', info: 'gray' };
export const isPackRule = (rule) => !!rule?.source && rule.source !== 'kernel';

export function ruleView(rule) {
  const levelLabel = (RULE_LEVELS.find(([k]) => k === rule.level) || [null, rule.level])[1];
  let state;
  if (!rule.enabled) state = { label: '已停用', tone: 'gray', dot: false };
  else if (rule.state === 'firing') state = { label: '触发中', tone: 'red', dot: true };
  else state = { label: '正常', tone: 'gray', dot: false };
  return {
    condition: rule.window ? `${rule.condition}（${rule.window}）` : rule.condition,
    level: [levelLabel, RULE_LEVEL_TONE[rule.level] || 'gray'],
    state,
    source: isPackRule(rule) ? rule.source : null,
    summary: runbookSummary(rule.runbook_md),
  };
}

// 处置手册的第一句（悬浮提示用）：去掉 Markdown 记号
export function runbookSummary(md) {
  const line = String(md || '').split('\n').map((x) => x.trim()).find(Boolean) || '';
  return line.replace(/^#+\s*|^[-*]\s+|^\d+\.\s+/, '').replace(/\*\*|__|`/g, '').trim() || '还没写处置手册';
}

export function rulesSub(items) {
  const n = (items || []).length;
  return `${n} 条`;
}

export const emptyRule = () => ({ name: '', condition: '', window: '', level: 'warning', runbook_md: '', auto_action: null, enabled: true });

/** 新建 / 修改规则的校验：名称、条件必填，条件里要有比较符 */
export function ruleProblems(form) {
  const out = {};
  if (!String(form.name || '').trim()) out.name = '要填规则名称';
  const c = String(form.condition || '').trim();
  if (!c) out.condition = '要填触发条件';
  else if (!/(>=|<=|==|!=|≥|≤|>|<)/.test(c)) out.condition = '条件里要有比较，比如 node.fail_rate > 5%';
  if (!RULE_LEVELS.some(([k]) => k === form.level)) out.level = '选一个级别';
  return out;
}

// 修改时只提交改了的字段；包声明的规则只能改 enabled
export function rulePatch(rule, form) {
  const keys = isPackRule(rule) ? ['enabled'] : ['name', 'condition', 'window', 'level', 'runbook_md', 'auto_action', 'enabled'];
  const patch = {};
  keys.forEach((k) => {
    const a = form[k] ?? null;
    const b = rule[k] ?? null;
    if (typeof a === 'string' ? a.trim() !== String(b ?? '') : a !== b) patch[k] = typeof a === 'string' ? a.trim() : a;
  });
  return patch;
}

export const firingOf = (items) => (items || []).filter((r) => r.enabled && r.state === 'firing').length;

// ---------- 导出器（O-11、O-12） ----------
export const SIGNALS = [['logs', '日志'], ['metrics', '指标'], ['traces', '链路']];
export const signalsText = (signals) => (signals?.length ? SIGNALS.filter(([k]) => signals.includes(k)).map(([, t]) => t).join(' · ') : DASH);
export const implLabel = (impl) => (!impl || impl === 'builtin' ? '内置' : impl);

export function exporterView(e) {
  return {
    label: e.label,
    target: e.configured && e.target ? e.target : '未配置',
    signals: signalsText(e.signals),
    impl: implLabel(e.implementation),
  };
}

// 保存前的检查：开着的导出器要有地址、至少一种信号
export function exporterProblems(items) {
  const out = {};
  (items || []).forEach((e) => {
    if (!e.enabled) return;
    if (!String(e.target || '').trim()) out[e.id] = `「${e.label}」要先填地址才能开启`;
    else if (!e.signals?.length) out[e.id] = `「${e.label}」至少选一种信号`;
  });
  return out;
}

export const exportersBody = (items) => ({
  items: (items || []).map((e) => ({ id: e.id, enabled: !!e.enabled, target: e.target || null, signals: e.signals || [] })),
});

export function exportersDirty(a, b) {
  return JSON.stringify(exportersBody(a)) !== JSON.stringify(exportersBody(b));
}

// ---------- 链路检索（O-05，没挂载的组件用） ----------
export const TRACE_STATUS = [['all', '全部'], ['ok', '正常'], ['denied', '有拒绝'], ['error', '出错']];
const TRACE_STATUS_VIEW = { ok: ['正常', 'teal'], denied: ['有拒绝', 'amber'], error: ['出错', 'red'] };
export const traceStatusView = (s) => TRACE_STATUS_VIEW[s] || [s || DASH, 'gray'];

/**
 * 检索框：一串十六进制当 trace id（前缀匹配，在结果里筛）；其余按空格拆成 键=值（项目=、用户=、包版本=…）。
 * 返回 {traceId, labels, error}。
 */
export function parseTraceQuery(text) {
  const parts = String(text || '').trim().split(/\s+/).filter(Boolean);
  let traceId = null;
  const labels = [];
  for (const p of parts) {
    if (/^[0-9a-f]{4,32}$/i.test(p)) { traceId = p.toLowerCase(); continue; }
    const r = parseLabelInput(p, { allowUser: true });
    if (r.error) return { traceId, labels, error: r.error };
    if (r.key === 'trace') traceId = r.value.toLowerCase();
    else labels.push({ key: r.key, value: r.value });
  }
  return { traceId, labels, error: null };
}

/**
 * 检索参数：页面上的公共标签打底，检索框里写的同名标签覆盖它；trace 不是 O-05 的标签，拿出来在结果里按前缀筛。
 * 返回 {query, traceId}。
 */
export function traceSearchQuery({ text, status, range, labels = '' }) {
  const page = parseLabelsQuery(labels);
  const parsed = parseTraceQuery(text);
  const base = Object.entries(page).filter(([k]) => k !== 'trace').map(([key, value]) => ({ key, value }));
  const merged = parsed.labels.reduce((acc, t) => addLabel(acc, t), base);
  return {
    query: { labels: labelsQuery(merged), status: status && status !== 'all' ? status : '', range: range || '24h' },
    traceId: parsed.traceId || (page.trace ? page.trace.toLowerCase() : null),
  };
}

export const filterTraces = (items, traceId) => (traceId ? (items || []).filter((t) => String(t.trace_id).toLowerCase().startsWith(traceId)) : items || []);

/**
 * 服务端（mock）按 O-05 的参数筛选：时间范围、公共标签（含 user）、状态；按开始时间倒序。
 */
export function selectTraces(all, { labels = '', status = '', range = '24h' } = {}, now = Date.now()) {
  const wanted = parseLabelsQuery(labels);
  const since = now - (RANGE_MS[range] || RANGE_MS['24h']);
  return (all || [])
    .filter((t) => Date.parse(t.started_at) >= since)
    .filter((t) => Object.entries(wanted).every(([k, v]) => String(t.labels?.[k] ?? '') === v))
    .filter((t) => !status || t.status === status)
    .sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at));
}

export function traceRowView(t, now = Date.now()) {
  const d = new Date(t.started_at);
  const min = Math.floor((now - d.getTime()) / 60000);
  return {
    id: t.trace_id,
    at: min < 1 ? '刚刚' : min < 60 ? `${min} 分钟前` : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`,
    root: t.root_name || DASH,
    project: t.labels?.project || DASH,
    user: t.labels?.user || DASH,
    total: `${((t.total_ms || 0) / 1000).toFixed(1)}s`,
    status: traceStatusView(t.status),
  };
}
