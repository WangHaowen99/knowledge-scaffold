// 系统设置（sys）的纯逻辑：分区、模型接入表单、上下文推荐表（合并、匹配、校验、导入）、会话池、沙箱、
// 全局 MCP、配置注册表、配额与自主构建限制，以及没挂界面的模型单价（SY-18）、服务令牌（X-06）。
// mock（routes-system.js）也用这里的推荐表合并与校验，保证页面和 mock 是同一套规则。

// ---------- 分区 ----------
export const SYS_SECTIONS = [
  { key: 'model', title: '模型接入', icon: 'cpu' },
  { key: 'ctx', title: '上下文推荐表', icon: 'table' },
  { key: 'sess', title: '会话与权限', icon: 'shield' },
  { key: 'box', title: '脚本验证沙箱', icon: 'server' },
  { key: 'mcp', title: 'MCP 服务器', icon: 'link' },
  { key: 'reg', title: '配置注册表', icon: 'sliders' },
  { key: 'quota', title: '配额与自主构建', icon: 'gauge' },
];
const SECTION_ALIAS = {
  models: 'model', engine: 'model', context: 'ctx', presets: 'ctx', session: 'sess', permission: 'sess',
  sandbox: 'box', 'mcp-servers': 'mcp', registry: 'reg', config: 'reg', quotas: 'quota', l4: 'quota', autobuild: 'quota',
};
/** 地址参数 ?section= → 分区 key；认不出时落到模型接入。available 给了时，不在里面的分区也落到模型接入（R17） */
export function sysSection(q, available) {
  const k = String(q || '').trim().toLowerCase();
  const key = SYS_SECTIONS.some((s) => s.key === k) ? k : SECTION_ALIAS[k] || 'model';
  return available && !available.includes(key) ? 'model' : key;
}

// 分区随接口出现（R17）：进系统设置时读一次各分区的接口，回 404 的分区不进左栏。
// 配额分区有两组接口（SY-15 配额、SY-17 自主构建限制），任一组在就出；模型接入是落脚点，一直在。
export const SECTION_PROBES = {
  model: [], ctx: ['contextPresets'], sess: ['sessionSettings'], box: ['sandboxSettings'], mcp: ['globalMcpServers'], reg: ['configRegistry'],
  quota: ['quotas', 'autobuildLimits'],
};
/** results：{接口名: 'ok' | 'missing' | 'error'}（missing = 404）；读不出来但不是 404 的分区照常出，进去看错误和重试 */
export function availableSections(results) {
  return SYS_SECTIONS.map((s) => s.key).filter((k) => {
    const probes = SECTION_PROBES[k] || [];
    return !probes.length || probes.some((name) => results?.[name] !== 'missing');
  });
}

// ---------- 数字与时间 ----------
export const fmtInt = (n) => (n == null || Number.isNaN(Number(n)) ? '—' : Number(n).toLocaleString('en-US'));
/** 输入框里的整数：容许千分位、空格、末尾的 %；空串返回 null，不是整数返回 NaN */
export function parseIntText(s) {
  const t = String(s ?? '').replace(/[,，\s]/g, '').replace(/%$/, '');
  if (!t) return null;
  return /^-?\d+$/.test(t) ? Number(t) : NaN;
}
/** 输入框里的数：容许小数 */
export function parseNumText(s) {
  const t = String(s ?? '').replace(/[,，\s]/g, '');
  if (!t) return null;
  return /^-?\d+(\.\d+)?$/.test(t) ? Number(t) : NaN;
}
const pad = (n) => String(n).padStart(2, '0');
/** RFC 3339 → 「2026-10-31 21:40」（本地时区） */
export function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
/** RFC 3339 → 「10-20 14:02」 */
export function fmtShort(iso) {
  const s = fmtDateTime(iso);
  return s === '—' ? s : s.slice(5);
}
/** RFC 3339 → 「21:40」 */
export function fmtHm(iso) {
  const s = fmtDateTime(iso);
  return s === '—' ? s : s.slice(11);
}

// ---------- 模型接入（SY-01～SY-04） ----------
// 引擎卡副标题是接口类型：配网关时要知道对接哪一种
export const ENGINE_META = {
  claude: { sub: 'Anthropic 兼容接口' },
  jiuwen: { sub: 'OpenAI 兼容接口' },
};
/** 引擎卡右上角：已安装或当前部署未安装（运行时版本在运维管控的版本清单里） */
export function installChip(e) {
  if (!e?.installed) return { text: '当前部署未安装', tone: 'amber', icon: 'alert' };
  return { text: '已安装', tone: 'teal', icon: 'check' };
}
const SOURCE_TEXT = { env: '.env', default: '内置默认', derived: '按窗口与最大输出推算' };
/** context_defaults 一项的来源说法（B2 第 8 条：env / default，没有值时交给引擎默认） */
export function defaultSourceText(d) {
  if (!d || d.value == null) return '引擎默认';
  return SOURCE_TEXT[d.source] || '默认值';
}
/** 留空时生效的值，用作输入框占位 */
export function defaultPlaceholder(d) {
  if (!d || d.value == null) return '留空交给引擎默认';
  return `留空按 ${d.value}（${defaultSourceText(d)}）`;
}
/** 压缩触发点的字段提示：只在取 .env 时说一声；页面填的值只校验范围，服务端按窗口与最大输出推算（B2 第 9 条） */
export function compactHint(d) {
  return d?.source === 'env' ? '当前取 .env' : null;
}

export function engineForm(e) {
  const c = e?.context || {};
  return {
    base_url: e?.base_url || '',
    api_key: '',
    model: e?.model || '',
    window: c.window == null ? '' : String(c.window),
    max_output: c.max_output == null ? '' : String(c.max_output),
    compact: c.compact_at_pct == null ? '' : `${c.compact_at_pct}%`,
  };
}

const URL_RE = /^https?:\/\/[^\s/]+[^\s]*$/i;
/** 模型接入表单校验；返回 {字段: 说明}，没问题时是空对象 */
export function engineProblems(f) {
  const out = {};
  const url = (f.base_url || '').trim();
  if (url && !URL_RE.test(url)) out.base_url = '要以 http:// 或 https:// 开头';
  if (/\s/.test((f.api_key || '').trim())) out.api_key = 'Key 里不能有空格';
  const w = parseIntText(f.window);
  const o = parseIntText(f.max_output);
  const p = parseIntText(f.compact);
  if (w !== null && (Number.isNaN(w) || w < 1000 || w > 10000000)) out.window = '整数 1,000–10,000,000';
  if (o !== null && (Number.isNaN(o) || o < 1000 || o > 1000000)) out.max_output = '整数 1,000–1,000,000';
  else if (o !== null && w !== null && !out.window && o >= w) out.max_output = '要小于上下文窗口';
  if (p !== null && (Number.isNaN(p) || p < 50 || p > 95)) out.compact = '50%–95% 之间';
  return out;
}

/**
 * SY-02 的请求体：只带改过的字段（字段不传表示不改，B2 第 9 条）；Key 留空不改；context 里传 null 表示留空。
 * 没有改动时返回 null。
 */
export function engineBody(e, f) {
  const base = engineForm(e);
  const body = {};
  if (f.base_url.trim() !== base.base_url) body.base_url = f.base_url.trim();
  if (f.api_key.trim()) body.api_key = f.api_key.trim();
  if (f.model.trim() !== base.model) body.model = f.model.trim();
  const ctx = {};
  if (parseIntText(f.window) !== parseIntText(base.window)) ctx.window = parseIntText(f.window);
  if (parseIntText(f.max_output) !== parseIntText(base.max_output)) ctx.max_output = parseIntText(f.max_output);
  if (parseIntText(f.compact) !== parseIntText(base.compact) && parseIntText(f.compact) !== null) ctx.compact_at_pct = parseIntText(f.compact);
  if (Object.keys(ctx).length) body.context = ctx;
  return Object.keys(body).length ? body : null;
}

/** 保存后的提示：压缩触发点填了别的值、服务端按推算值返回时，说清楚没有保存 */
export function engineSavedText(label, sent, saved) {
  const pct = sent?.context?.compact_at_pct;
  const eff = saved?.context?.compact_at_pct;
  if (pct != null && eff != null && pct !== eff) {
    return `已保存，新会话生效；自动压缩阈值按推算为 ${eff}%`;
  }
  return `已保存，${label}的新会话生效`;
}

/**
 * 内测引擎卡底部那行：「上下文窗口 131072 · 最大输出 8192（来自推荐表，可覆盖）」。
 * 生效值：表单里填的 > 留空时的默认；来源：与推荐表一致 / 已保存 / .env / 内置默认。
 */
export function contextSummary(e, f, preset) {
  const d = e?.context_defaults || {};
  const w = parseIntText(f.window);
  const o = parseIntText(f.max_output);
  const win = w ?? d.window?.value ?? null;
  const out = o ?? d.max_output?.value ?? null;
  let origin;
  if (w == null && o == null) origin = defaultSourceText(d.window);
  else if (preset && preset.window === w && preset.max_output === o) origin = '来自推荐表';
  else origin = '已填写';
  return { window: win, max_output: out, origin, text: `上下文窗口 ${win ?? '—'} · 最大输出 ${out ?? '—'}（${origin}，可覆盖）` };
}

// ---------- 上下文推荐表（SY-05～SY-08） ----------
// 网关常在模型名后追加 6 或 8 位日期版本
const DATE_SUFFIX = /-(\d{8}|\d{6})$/;
/** 小写、去掉 provider/ 前缀、把 . 和 _ 统一成 -、去掉网关日期后缀（与后端 model_context_presets 一致） */
export function normalizeModelName(name) {
  const base = String(name || '').trim().toLowerCase().split('/').pop();
  return base.replace(/[._]/g, '-').replace(DATE_SUFFIX, '');
}
const keysOf = (row) => [row.model, ...(row.aliases || [])].map(normalizeModelName);

/**
 * 内置表 + 自定义表 → SY-05 的 rows：自定义行覆盖内置同名行（origin=override，带内置值），其余自定义行追加在后面。
 * builtin、custom 的项都是 {model, vendor, window, max_output, note, aliases?, sources?}。
 */
export function mergePresets(builtin, custom) {
  const rows = (builtin || []).map((b) => ({ aliases: [], sources: [], ...b, origin: 'builtin', builtin: null }));
  (custom || []).forEach((c) => {
    const key = normalizeModelName(c.model);
    const i = rows.findIndex((r) => r.origin === 'builtin' && keysOf(r).includes(key));
    const row = { vendor: '', note: '', aliases: [], sources: [], ...c };
    if (i >= 0) {
      const b = rows[i];
      rows[i] = { ...row, model: b.model, aliases: c.aliases?.length ? c.aliases : b.aliases, origin: 'override', builtin: { window: b.window, max_output: b.max_output } };
    } else rows.push({ ...row, origin: 'custom', builtin: null });
  });
  return rows;
}

/** SY-08：按模型名查推荐（规范化后比对模型名与别名），查不到返回 null */
export function matchPreset(rows, model) {
  const key = normalizeModelName(model);
  if (!key) return null;
  return (rows || []).find((r) => keysOf(r).includes(key)) || null;
}

/** 表里每行能做什么：内置可覆盖；覆盖行可编辑、恢复内置；自定义行可编辑、删除 */
export function presetRowActions(row) {
  if (row.origin === 'override') return ['edit', 'restore'];
  if (row.origin === 'custom') return ['edit', 'delete'];
  return ['override'];
}

// 别名、来源链接页面不编辑：不传，服务端沿用已存的同名条目（B2 第 10 条）
const toPreset = (r) => ({ model: r.model, vendor: r.vendor || '', window: r.window, max_output: r.max_output, note: r.note || '' });
/** 当前自定义表（SY-06 整份替换用）：rows 里覆盖行和自定义行 */
export const customPresets = (rows) => (rows || []).filter((r) => r.origin !== 'builtin').map(toPreset);

/** 按规范化模型名替换（保持位置），没有就追加；replaced 是被编辑那一行原来的模型名 */
export function upsertPreset(list, entry, replaced) {
  const key = normalizeModelName(replaced || entry.model);
  const i = list.findIndex((p) => normalizeModelName(p.model) === key);
  if (i < 0) return [...list, entry];
  return list.map((p, j) => (j === i ? entry : p));
}
export const removePreset = (list, model) => list.filter((p) => normalizeModelName(p.model) !== normalizeModelName(model));

export const PRESET_COLUMNS = { model: '模型', vendor: '厂商', window: '上下文窗口', max_output: '最大输出', note: '备注' };
// 导入文件的表头 → 字段
const HEADER_ALIAS = {
  model: 'model', id: 'model', 模型: 'model', vendor: 'vendor', 厂商: 'vendor',
  window: 'window', max_context_tokens: 'window', 上下文窗口: 'window',
  max_output: 'max_output', max_output_tokens: 'max_output', 最大输出: 'max_output',
  note: 'note', 备注: 'note', 说明: 'note', 说明与来源: 'note', aliases: 'aliases', 别名: 'aliases', sources: 'sources', 来源链接: 'sources',
};

/** 一行推荐值的问题：{column, message}，没问题返回 null */
export function presetProblem(p) {
  if (!String(p.model || '').trim()) return { column: 'model', message: '不能为空' };
  if (String(p.model).length > 120) return { column: 'model', message: '不超过 120 个字符' };
  const w = typeof p.window === 'number' ? p.window : parseIntText(p.window);
  const o = typeof p.max_output === 'number' ? p.max_output : parseIntText(p.max_output);
  if (w === null || Number.isNaN(w)) return { column: 'window', message: '不是数字' };
  if (w < 1000 || w > 10000000) return { column: 'window', message: '要在 1,000–10,000,000 之间' };
  if (o === null || Number.isNaN(o)) return { column: 'max_output', message: '不是数字' };
  if (o < 1000 || o > 1000000) return { column: 'max_output', message: '要在 1,000–1,000,000 之间' };
  if (o >= w) return { column: 'max_output', message: '要小于上下文窗口' };
  if ((p.sources || []).some((u) => !/^https:\/\//.test(u))) return { column: 'note', message: '来源链接要以 https:// 开头' };
  return null;
}

/** 整份自定义表的第一处问题（行号从 1 起）：{row, column, message} 或 null；同名（规范化后）也算 */
export function validatePresets(list) {
  const seen = new Map();
  for (let i = 0; i < list.length; i += 1) {
    const bad = presetProblem(list[i]);
    if (bad) return { row: i + 1, ...bad };
    const key = normalizeModelName(list[i].model);
    if (seen.has(key)) return { row: i + 1, column: 'model', message: `与第 ${seen.get(key)} 行重名` };
    seen.set(key, i + 1);
  }
  return null;
}

/**
 * 校验失败的位置说法（SY-05 custom.error、SY-06 的 details.error）：「第 4 行「上下文窗口」不是数字」。
 * column 可以是字段名，也可以是文件里的列号；message 已经带了位置时不再重复。
 */
export function presetErrorText(error) {
  if (!error) return '';
  const msg = String(error.message || '').trim();
  if (error.row == null || msg.includes(`第 ${error.row} `)) return msg || '格式不对';
  const col = error.column == null ? '' : typeof error.column === 'number' ? `第 ${error.column} 列` : `「${PRESET_COLUMNS[error.column] || error.column}」`;
  return `第 ${error.row} 行${col}${msg ? (col ? msg : `：${msg}`) : '格式不对'}`;
}

/** 校验失败对应的那一行（导入的文件或本次提交的列表），用来在提示里点出模型名 */
export function locatePresetError(error, list) {
  if (!error || error.row == null) return null;
  const p = (list || [])[error.row - 1];
  return p ? { row: error.row, model: p.model || '', field: typeof error.column === 'string' ? error.column : null } : null;
}

function splitCsvLine(line) {
  const out = [];
  let cur = '';
  let q = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (q) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i += 1; } else if (ch === '"') q = false; else cur += ch;
    } else if (ch === '"') q = true;
    else if (ch === ',') { out.push(cur); cur = ''; } else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}
const listOf = (v) => (Array.isArray(v) ? v : String(v || '').split(/[;；|、]/)).map((s) => String(s).trim()).filter(Boolean);
function fromRecord(rec) {
  const p = { model: '', vendor: '', window: null, max_output: null, note: '' };
  Object.entries(rec || {}).forEach(([k, v]) => {
    const f = HEADER_ALIAS[String(k).trim()] || HEADER_ALIAS[String(k).trim().toLowerCase()];
    if (!f) return;
    if (f === 'aliases' || f === 'sources') p[f] = listOf(v);
    else if (f === 'window' || f === 'max_output') p[f] = typeof v === 'number' ? v : parseIntText(v);
    else p[f] = String(v ?? '').trim();
  });
  return p;
}

/**
 * 导入自定义表：JSON（数组，或 {presets: [...]}）或 CSV（第一行是表头：模型、厂商、上下文窗口、最大输出、说明）。
 * 返回 {presets, error}；error 同 SY-06 的 details.error，行号从 1 起（CSV 不算表头那行）。
 */
export function parsePresetFile(name, text) {
  const raw = String(text || '').replace(/^﻿/, '');
  let records;
  if (/\.json$/i.test(name || '') || /^\s*[[{]/.test(raw)) {
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      return { presets: [], error: { row: null, column: null, message: '不是合法的 JSON' } };
    }
    records = Array.isArray(data) ? data : data?.presets || data?.models;
    if (!Array.isArray(records)) return { presets: [], error: { row: null, column: null, message: 'JSON 里要有 presets 数组' } };
  } else {
    const lines = raw.split(/\r?\n/).filter((l) => l.trim());
    if (lines.length < 2) return { presets: [], error: { row: null, column: null, message: 'CSV 至少要有表头和一行数据' } };
    const head = splitCsvLine(lines[0]);
    if (!head.some((h) => HEADER_ALIAS[h] === 'model' || HEADER_ALIAS[h.toLowerCase()] === 'model')) {
      return { presets: [], error: { row: null, column: null, message: '表头里没有「模型」列' } };
    }
    records = lines.slice(1).map((l) => Object.fromEntries(splitCsvLine(l).map((v, i) => [head[i] || `_${i}`, v])));
  }
  if (!records.length) return { presets: [], error: { row: null, column: null, message: '文件里没有数据行' } };
  const presets = records.map(fromRecord);
  const error = validatePresets(presets.map((p) => ({ ...p, window: p.window === null ? '' : p.window, max_output: p.max_output === null ? '' : p.max_output })));
  return { presets, error };
}

/** 导入前的确认说法：覆盖内置多少行、新增多少行，替换掉现有自定义表多少行 */
export function importSummary(presets, rows) {
  const builtin = (rows || []).filter((r) => r.origin === 'builtin' || r.origin === 'override');
  const over = presets.filter((p) => builtin.some((b) => keysOf(b).includes(normalizeModelName(p.model)))).length;
  return { total: presets.length, overrides: over, added: presets.length - over, replacing: customPresets(rows).length };
}

/** 推荐表顶部提示条：只在有误、停用时出；正常时不出（覆盖、自定义看表格「来源」列） */
export function presetsNote(view) {
  const c = view?.custom || {};
  if (c.status === 'invalid') {
    const fallback = c.loaded_at ? `当前沿用 ${fmtShort(c.loaded_at)} 加载的自定义表` : '当前未生效';
    return { tone: 'red', lead: `自定义表有误，${fallback}：`, text: presetErrorText(c.error) };
  }
  if (c.status === 'disabled') {
    return { tone: 'gray', lead: '', text: `当前只用内置表${view?.builtin_verified_at ? `（核对日期 ${view.builtin_verified_at}）` : ''}` };
  }
  return null;
}

export function filterPresets(rows, q) {
  const k = String(q || '').trim().toLowerCase();
  if (!k) return rows || [];
  return (rows || []).filter((r) => [r.model, r.vendor, ...(r.aliases || [])].some((x) => String(x || '').toLowerCase().includes(k)));
}

/** 推荐表编辑卡：从一行（或空白新增）得到表单；mode = override | edit | new */
export function presetForm(row, mode) {
  if (mode === 'new' || !row) return { mode: 'new', replaced: null, model: '', vendor: '', window: '', max_output: '', note: '', builtin: null };
  return {
    mode, replaced: row.model, model: row.model, vendor: row.vendor || '', window: String(row.window ?? ''), max_output: String(row.max_output ?? ''),
    note: mode === 'override' ? '' : row.note || '', builtin: row.origin === 'builtin' ? { window: row.window, max_output: row.max_output } : row.builtin,
  };
}
/** 编辑卡的字段问题（每个字段各查一遍）；同名（规范化后、含别名）另算，正在编辑的那一行不算 */
export function presetFormProblems(f, rows) {
  const out = {};
  const fill = { model: 'model-x', window: 10000000, max_output: 1000 };
  [['model', { model: f.model }], ['window', { window: f.window }], ['max_output', { max_output: f.max_output }]].forEach(([k, part]) => {
    const bad = presetProblem({ ...fill, ...part });
    if (bad && bad.column === k) out[k] = bad.message;
  });
  // 两个数都对时再比大小
  if (!out.window && !out.max_output && parseIntText(f.max_output) >= parseIntText(f.window)) out.max_output = '要小于上下文窗口';
  const key = normalizeModelName(f.model);
  if (!out.model && key) {
    const owner = (rows || []).find((r) => keysOf(r).includes(key) && normalizeModelName(r.model) !== normalizeModelName(f.replaced || ''));
    if (owner && !(f.mode === 'new' && owner.origin === 'builtin')) out.model = `与表里的 ${owner.model} 重名`;
  }
  return out;
}
/** 编辑卡 → 一条自定义表条目 */
export const presetEntry = (f) => ({
  model: f.model.trim(), vendor: f.vendor.trim(), window: parseIntText(f.window), max_output: parseIntText(f.max_output), note: f.note.trim(),
});

// ---------- 会话与权限（SY-09） ----------
export const PERMISSION_MODES = [
  ['bypassPermissions', '全放行（默认）', '不弹确认卡'],
  ['default', '逐操作确认', '写文件、执行命令都要确认'],
  ['acceptEdits', '仅自动接受编辑', '改产物自动通过，执行命令仍要确认'],
  ['plan', '计划模式', '只出计划，不改文件'],
];
export const permissionModes = () => PERMISSION_MODES;
/** 会话池三格（契约 #56：sys 只读，运维面可改）；运维改过时标「运维已调整」 */
export function poolRows(pool) {
  const p = pool || {};
  const sub = () => (p.source === 'env' || !p.source ? '' : '运维已调整');
  return [
    { key: 'max_sessions', label: '活跃会话上限', value: p.max_sessions == null ? '—' : String(p.max_sessions), sub: sub('max_sessions') },
    { key: 'idle_ttl_s', label: '空闲回收', value: p.idle_ttl_s == null ? '—' : p.idle_ttl_s === 0 ? '不回收' : `${p.idle_ttl_s} 秒`, sub: sub('idle_ttl_s') },
    { key: 'permission_timeout_s', label: '确认卡超时', value: p.permission_timeout_s == null ? '—' : `${p.permission_timeout_s} 秒`, sub: sub('permission_timeout_s') },
  ];
}

// ---------- 脚本验证沙箱（SY-10、SY-11） ----------
/** 「非 root、只读根文件系统、内存 256MB、最多 64 个进程」（网关地址在右侧「脚本验证网关」） */
export function sandboxLine(v) {
  if (!v) return '';
  const i = v.isolation || {};
  const parts = [
    i.non_root ? '非 root' : i.non_root === false ? '以 root 运行' : '',
    i.readonly_root ? '只读根文件系统' : '',
    i.memory_mb ? `内存 ${i.memory_mb}MB` : '',
    i.max_procs ? `最多 ${i.max_procs} 个进程` : '',
  ].filter(Boolean);
  if (!v.connected) return v.detail || `连不上 ${v.gateway || '脚本验证网关'}`;
  return parts.join('、');
}
export function filterPackages(list, q) {
  const k = String(q || '').trim().toLowerCase();
  return k ? (list || []).filter((p) => String(p.name || '').toLowerCase().includes(k)) : list || [];
}

// ---------- 全局 MCP 服务器（SY-12、SY-13） ----------
/** 地址 / 命令列：http 给 url，stdio 给命令加参数（B2 第 12 条带 args） */
export function mcpTarget(s) {
  if (!s) return '';
  if (s.transport === 'stdio') return [s.command, ...(s.args || [])].filter(Boolean).join(' ');
  return s.url || '';
}
/** 状态列：本页测过的结果优先（B2 第 12 条：服务端不落盘状态，恒为 unknown） */
export function mcpStatus(s, tested) {
  const t = tested?.[s.name];
  if (t) return t.ok ? ['已连接', 'teal', 'check'] : [`连接失败${t.code ? ` · ${t.code}` : ''}`, 'red', 'x'];
  if (s.status === 'connected') return ['已连接', 'teal', 'check'];
  if (s.status === 'failed') return [`连接失败${s.status_detail ? ` · ${String(s.status_detail).match(/\d{3}/)?.[0] || ''}`.replace(/ · $/, '') : ''}`, 'red', 'x'];
  return ['未测试', 'gray', null];
}
/** 工具列：测过就用测出来的；服务端不列工具时（B2）显示 — */
export function mcpTools(s, tested) {
  const list = tested?.[s.name]?.tools?.length ? tested[s.name].tools : s.tools || [];
  return list;
}
export function mcpForm(s) {
  if (!s) return { name: '', transport: 'http', target: '', auth: 'none', secret_ref: '' };
  return { name: s.name, transport: s.transport === 'stdio' ? 'stdio' : 'http', target: mcpTarget(s), auth: s.auth?.kind || 'none', secret_ref: s.auth?.secret_ref || '' };
}
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
export function mcpProblems(f, items, editing) {
  const out = {};
  const name = (f.name || '').trim();
  if (!name) out.name = '起个名字';
  else if (!NAME_RE.test(name)) out.name = '只用字母、数字、- _ .，64 个字符以内';
  else if ((items || []).some((x) => x.name === name && x.name !== editing)) out.name = '已经有同名的服务器';
  const t = (f.target || '').trim();
  if (!t) out.target = f.transport === 'stdio' ? '填启动命令' : '填地址';
  else if (f.transport !== 'stdio' && !URL_RE.test(t)) out.target = '要以 http:// 或 https:// 开头';
  if (f.auth === 'bearer' && !/^cred:\/\/\S+$/.test((f.secret_ref || '').trim())) out.secret_ref = '填密钥引用，如 cred://mcp/<名称>';
  return out;
}
/** 表单 → SY-12 的一项（保留原来的 enabled 等字段）；stdio 的命令按空白拆成 command + args */
export function mcpItemFromForm(f, prev) {
  const t = f.target.trim();
  const base = { ...(prev || {}), name: f.name.trim(), transport: f.transport, auth: f.auth === 'bearer' ? { kind: 'bearer', secret_ref: f.secret_ref.trim() } : { kind: 'none' } };
  delete base.status; delete base.status_detail; delete base.tools; delete base.last_test_at; delete base.latency_ms;
  if (f.transport === 'stdio') {
    const [command, ...args] = t.split(/\s+/);
    return { ...base, url: null, command, args, enabled: prev?.enabled ?? true };
  }
  return { ...base, url: t, command: null, args: [], enabled: prev?.enabled ?? true };
}
// 整份替换时只发配置，不带状态、工具这些运行时字段
const mcpStrip = (x) => mcpItemFromForm(mcpForm(x), x);
/** 保存后的整份列表：编辑时替换原位，新增时追加 */
export function mcpItemsAfterSave(items, item, editing) {
  const list = (items || []).map(mcpStrip);
  if (editing) return list.map((x) => (x.name === editing ? item : x));
  return [...list, item];
}
/** 删除一项后的整份列表 */
export const mcpItemsAfterRemove = (items, name) => (items || []).filter((x) => x.name !== name).map(mcpStrip);
/** 保存失败的说法：后端这一版不支持鉴权（B2 第 12 条）时直接说清楚，其余返回 null 交给通用错误提示 */
export function mcpSaveError(e) {
  const f = e?.details?.fields || {};
  if (e?.code === 'invalid_argument' && f['auth.kind']) return '暂不支持鉴权，请选「不鉴权」';
  return null;
}
/** 测试连接的提示：「kb-search：连接正常，4 个工具，延迟 86ms」 */
export function mcpTestText(name, r) {
  if (!r) return '';
  if (!r.ok) return `${name}：${r.error || r.detail || '连接失败'}`;
  const tools = r.tools?.length ? `，${r.tools.length} 个工具` : '';
  return `${name}：连接正常${tools}${r.latency_ms != null ? `，延迟 ${r.latency_ms}ms` : ''}`;
}

// ---------- 配置注册表（SY-14） ----------
export const SOURCE_LABEL = { env: '环境变量', data_dir: '数据目录', pack: '包', default: '默认值' };
export const SOURCE_CLASS = { env: 'env', data_dir: 'data', pack: 'pkg', default: 'def' };
export const EFFECT_LABEL = { immediate: '立即', next_message: '下一条消息', new_session: '新会话', restart: '重启' };
export const EFFECT_TONE = { immediate: 'teal', next_message: 'blue', new_session: 'indigo', restart: 'gray' };
export const EFFECT_SEG = [['all', '全部'], ['immediate', '立即'], ['next_message', '下一条消息'], ['new_session', '新会话'], ['restart', '重启']];
export const precedenceText = (list) => (list || []).map((s) => SOURCE_LABEL[s] || s).join(' > ');
/** 搜配置项：键名、说明、当前值；effect 为 all 时不按生效时机筛 */
export function filterRegistry(items, { effect, q } = {}) {
  const k = String(q || '').trim().toLowerCase();
  return (items || []).filter((it) => (!effect || effect === 'all' || it.effect === effect)
    && (!k || [it.key, it.description, it.value].some((x) => String(x || '').toLowerCase().includes(k))));
}
/** 这一项自己的来源优先级和默认顺序不同时，给一句短说明（B3b 第 3 条：模型接入、权限模式、会话池是设置页优先） */
export function itemPrecedenceNote(item, top) {
  const own = item?.precedence;
  if (!own?.length || !top?.length || own.join() === top.join()) return null;
  return { text: own[0] === 'data_dir' ? '设置页优先' : `${SOURCE_LABEL[own[0]] || own[0]}优先`, title: `这一项的优先级：${precedenceText(own)}` };
}
/** 当前值的显示：密钥只给引用；空值写「未设置」 */
export function registryValue(item) {
  const v = item?.value;
  if (v == null || v === '') return { text: '未设置', mono: false, muted: true };
  return { text: String(v), mono: item.secret || /^[a-z]+:\/\//i.test(String(v)), muted: false };
}

// ---------- 配额与自主构建限制（SY-15～SY-17） ----------
export const QUOTA_ITEMS = {
  user: [['concurrent_sessions', '同时在线会话'], ['daily_tokens', '每日 token 预算'], ['collections_per_source_per_hour', '证据采集频率']],
  project: [['monthly_tokens', '每月 token 预算'], ['snapshot_git_mb', '单份快照在 git 里的上限']],
};
export const quotaItemLabel = (item) => [...QUOTA_ITEMS.user, ...QUOTA_ITEMS.project].find(([k]) => k === item)?.[1] || item;
/** 单独设置的值怎么显示：token 加千分位，快照加 MB，采集频率写「每小时 N 次」 */
export function quotaValueText(item, v) {
  if (v == null) return '—';
  if (item === 'snapshot_git_mb') return `${fmtInt(v)} MB`;
  if (item === 'collections_per_source_per_hour') return `每小时 ${v} 次`;
  return fmtInt(v);
}
export function subjectText(s) {
  if (!s) return '—';
  return s.kind === 'project' ? `项目 ${s.label || s.id}` : `用户 ${s.id}`;
}

export function quotaForm(q) {
  const u = q?.per_user || {};
  const p = q?.per_project || {};
  const s = (v) => (v == null ? '' : fmtInt(v));
  return {
    concurrent_sessions: s(u.concurrent_sessions), daily_tokens: s(u.daily_tokens), collections_per_source_per_hour: s(u.collections_per_source_per_hour),
    monthly_tokens: s(p.monthly_tokens), snapshot_git_mb: s(p.snapshot_git_mb),
  };
}
const QUOTA_RANGE = {
  concurrent_sessions: [1, 64], daily_tokens: [1, 1e12], collections_per_source_per_hour: [0, 1000], monthly_tokens: [1, 1e13], snapshot_git_mb: [1, 10240],
};
export function quotaProblems(f) {
  const out = {};
  Object.entries(QUOTA_RANGE).forEach(([k, [lo, hi]]) => {
    const v = parseIntText(f[k]);
    if (v === null || Number.isNaN(v)) out[k] = '填整数';
    else if (v < lo || v > hi) out[k] = `整数 ${fmtInt(lo)}–${fmtInt(hi)}`;
  });
  return out;
}
/** SY-15 PUT：只改 per_user、per_project（online_engines 只读，不发） */
export function quotaBody(f) {
  return {
    per_user: { concurrent_sessions: parseIntText(f.concurrent_sessions), daily_tokens: parseIntText(f.daily_tokens), collections_per_source_per_hour: parseIntText(f.collections_per_source_per_hour) },
    per_project: { monthly_tokens: parseIntText(f.monthly_tokens), snapshot_git_mb: parseIntText(f.snapshot_git_mb) },
  };
}
export const quotaDirty = (q, f) => JSON.stringify(quotaForm(q)) !== JSON.stringify(quotaForm(quotaBodyToView(q, f)));
function quotaBodyToView(q, f) {
  const b = quotaBody(f);
  return { ...q, per_user: { ...q?.per_user, ...b.per_user }, per_project: { ...q?.per_project, ...b.per_project } };
}

const USERNAME_RE = /^[A-Za-z0-9_.-]{1,64}$/;
/** 单独设置（SY-16）表单校验；同一对象同一项已经有单独设置时不让重复加 */
export function overrideProblems(f, existing) {
  const out = {};
  const id = (f.id || '').trim();
  if (!id) out.id = f.kind === 'project' ? '填项目 ID' : '选一个用户';
  else if (f.kind === 'user' && !USERNAME_RE.test(id)) out.id = '用户名只含字母、数字、- _ .';
  if (!QUOTA_ITEMS[f.kind]?.some(([k]) => k === f.item)) out.item = '选一项';
  const v = parseIntText(f.value);
  if (v === null || Number.isNaN(v) || v < 0) out.value = '填非负整数';
  const reason = (f.reason || '').trim();
  if (!reason) out.reason = '填写原因';
  else if (reason.length > 200) out.reason = '不超过 200 个字';
  if (!out.id && !out.item && (existing || []).some((o) => o.subject?.kind === f.kind && o.subject?.id === id && o.item === f.item)) {
    out.item = '已有这一项的单独设置';
  }
  return out;
}
export const overrideBody = (f) => ({ subject: { kind: f.kind, id: f.id.trim() }, item: f.item, value: parseIntText(f.value), reason: f.reason.trim() });

const SYMBOL = { CNY: '¥', USD: '$' };
export const moneySymbol = (c) => SYMBOL[c] || c || '';
export function limitsForm(l) {
  return {
    max_concurrent_runs: l?.max_concurrent_runs == null ? '' : String(l.max_concurrent_runs),
    hours: l?.max_duration_min == null ? '' : String(Math.round((l.max_duration_min / 60) * 100) / 100),
    max_tokens: l?.max_tokens == null ? '' : fmtInt(l.max_tokens),
    cost: l?.max_cost?.amount == null ? '' : String(l.max_cost.amount),
    currency: l?.max_cost?.currency || 'CNY',
    allow_production_collection: !!l?.allow_production_collection,
    night: !!l?.night_priority?.enabled,
  };
}
export function limitsProblems(f) {
  const out = {};
  const r = parseIntText(f.max_concurrent_runs);
  if (r === null || Number.isNaN(r) || r < 0 || r > 16) out.max_concurrent_runs = '整数 0–16';
  const h = parseNumText(f.hours);
  if (h === null || Number.isNaN(h) || h <= 0 || h > 72) out.hours = '0–72 小时';
  const t = parseIntText(f.max_tokens);
  if (t === null || Number.isNaN(t) || t <= 0) out.max_tokens = '填正整数';
  const c = parseNumText(f.cost);
  if (c === null || Number.isNaN(c) || c <= 0) out.cost = '填大于 0 的金额';
  return out;
}
/** SY-17 PUT（running 只读，不发）；夜间时段沿用原值 */
export function limitsBody(f, prev) {
  return {
    max_concurrent_runs: parseIntText(f.max_concurrent_runs),
    max_duration_min: Math.round(parseNumText(f.hours) * 60),
    max_tokens: parseIntText(f.max_tokens),
    max_cost: { amount: parseNumText(f.cost), currency: f.currency || prev?.max_cost?.currency || 'CNY' },
    allow_production_collection: !!f.allow_production_collection,
    night_priority: { ...(prev?.night_priority || { start: '20:00', end: '08:00' }), enabled: !!f.night },
  };
}
export const limitsDirty = (l, f) => JSON.stringify(limitsForm(l)) !== JSON.stringify(f);
export const nightText = (n) => `${n?.start || '20:00'}–${n?.end || '08:00'} 启动的不占会话池名额`;

// ---------- 模型单价（SY-18，设计稿没画入口） ----------
export const PRICE_FIELDS = [['input_per_mtok', '输入'], ['output_per_mtok', '输出'], ['cache_read_per_mtok', '缓存读取']];
export function pricingForm(p) {
  return {
    currency: p?.currency || 'CNY',
    items: (p?.items || []).map((it) => ({
      engine: it.engine || 'claude', model: it.model || '',
      input_per_mtok: it.input_per_mtok == null ? '' : String(it.input_per_mtok),
      output_per_mtok: it.output_per_mtok == null ? '' : String(it.output_per_mtok),
      cache_read_per_mtok: it.cache_read_per_mtok == null ? '' : String(it.cache_read_per_mtok),
    })),
  };
}
export const emptyPriceRow = (engine = 'claude') => ({ engine, model: '', input_per_mtok: '', output_per_mtok: '', cache_read_per_mtok: '' });
/** 单价表校验：{rows: {行下标: {字段: 说明}}, currency?}；同一引擎下模型重名算错 */
export function pricingProblems(f) {
  const rows = {};
  const seen = new Set();
  (f.items || []).forEach((it, i) => {
    const r = {};
    const model = (it.model || '').trim();
    if (!model) r.model = '填模型名';
    else {
      const key = `${it.engine}/${normalizeModelName(model)}`;
      if (seen.has(key)) r.model = '同一引擎下重名';
      seen.add(key);
    }
    if (!['claude', 'jiuwen'].includes(it.engine)) r.engine = '选引擎';
    PRICE_FIELDS.forEach(([k]) => {
      const v = parseNumText(it[k]);
      if (k === 'cache_read_per_mtok' && v === null) return;
      if (v === null || Number.isNaN(v) || v < 0) r[k] = '填非负数';
    });
    if (Object.keys(r).length) rows[i] = r;
  });
  const out = {};
  if (Object.keys(rows).length) out.rows = rows;
  if (!['CNY', 'USD'].includes(f.currency)) out.currency = '选币种';
  return out;
}
export function pricingBody(f) {
  return {
    currency: f.currency,
    items: f.items.map((it) => ({
      engine: it.engine, model: it.model.trim(),
      input_per_mtok: parseNumText(it.input_per_mtok), output_per_mtok: parseNumText(it.output_per_mtok),
      cache_read_per_mtok: parseNumText(it.cache_read_per_mtok),
    })),
  };
}
/** 单价表没落地（接口 404）时的说法：费用是引擎报告的美元数（B2 第 1 条） */
export const PRICING_FALLBACK = '服务端还没有模型单价表：费用按引擎报告的美元数（USD）显示。';

// ---------- 服务令牌（X-06，设计稿没画入口） ----------
export const SERVICE_SCOPES = [
  ['releases.read', '拉取发布列表与发布包'],
  ['receipts.write', '提交求解验证回执'],
  ['feedback.write', '提交外部反馈'],
];
export const SERVICE_TTL = [['90', '90 天', 90 * 86400], ['365', '1 年', 365 * 86400], ['0', '永不过期', null]];
export const SERVICE_TOKEN_RE = /^onto_svc_[A-Za-z0-9_-]+$/;
export function serviceTokenProblems(f) {
  const out = {};
  const name = (f.name || '').trim();
  if (!name) out.name = '写一个用途，方便以后认出它';
  else if (name.length > 120) out.name = '不超过 120 个字';
  if (!f.scopes?.length) out.scopes = '至少选一个';
  else if (f.scopes.some((s) => !SERVICE_SCOPES.some(([k]) => k === s))) out.scopes = '权限范围不认识';
  if (f.limit === 'projects' && !f.project_ids?.length) out.project_ids = '至少选一个项目，或改成全部项目';
  return out;
}
export function serviceTokenBody(f) {
  const ttl = SERVICE_TTL.find((t) => t[0] === f.ttl) || SERVICE_TTL[0];
  return {
    name: f.name.trim(),
    scopes: SERVICE_SCOPES.map(([k]) => k).filter((k) => f.scopes.includes(k)),
    ...(f.limit === 'projects' ? { project_ids: [...f.project_ids] } : {}),
    expires_in_s: ttl[2],
  };
}
/** 有效期：「剩 21 天」「永不过期」「已过期」 */
export function expiryText(expiresAt, now = Date.now()) {
  if (!expiresAt) return '永不过期';
  const left = new Date(expiresAt).getTime() - now;
  if (Number.isNaN(left)) return '—';
  if (left <= 0) return '已过期';
  const d = Math.floor(left / 86400000);
  if (d >= 1) return `剩 ${d} 天`;
  return `剩 ${Math.max(1, Math.round(left / 3600000))} 小时`;
}
/** 项目范围：不限 → 全部项目；限定 → 列名字（认不出的给 ID） */
export function projectScopeText(ids, projects) {
  if (!ids) return '全部项目';
  if (!ids.length) return '—';
  const name = (id) => (projects || []).find((p) => p.id === id)?.name || id;
  return ids.length > 2 ? `${ids.slice(0, 2).map(name).join('、')} 等 ${ids.length} 个` : ids.map(name).join('、');
}
export const scopeShort = (s) => SERVICE_SCOPES.find(([k]) => k === s)?.[1] || s;
