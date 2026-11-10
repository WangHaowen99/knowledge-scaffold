// 插件详情的纯逻辑（设计稿 pages-60-plugins.js 的 plugin-detail）：主线图上插入位置的布局、「包含什么」分组、
// 英雄区按钮与菜单、统计卡、更新记录标记、依赖与大小的说法。PL-02 的实际字段见 contract-changes B3a 第 4 条：
// flow_insertions 只有 {id, title}（title 暂取 ID）、project_stats 为 null、contents 是 [{group, items}]、config 原样给清单。

// 主线 4 段（详设 6.8；core 的 flow.yaml 定义）
export const MAINLINE = [
  ['research', '资料分析'], ['model', '建模'], ['validate', '校验'], ['publish', '发布'],
];
const MAIN_IDS = MAINLINE.map(([id]) => id);

/** 插入点挂在主线哪一段：带 at 就用；只有节点 ID 时按 ID 里的词猜，猜不出算「建模」 */
// 官方包节点挂在主线哪一段（各包 flow.yaml 的 after）；后端 PL-02 只给 {id, title} 时用
const KNOWN_AT = {
  'diagram.flowchart': 'research', 'migrate.reuse': 'research', 'codegen.solver': 'model',
  'trace.trace': 'validate', 'codegen.codegen': 'validate', 'evo.evolution': 'publish',
};

export function insertionAt(ins) {
  if (MAIN_IDS.includes(ins?.at)) return ins.at;
  if (KNOWN_AT[ins?.id]) return KNOWN_AT[ins.id];
  const id = String(ins?.id || '').toLowerCase();
  if (/research|material|evidence/.test(id)) return 'research';
  if (/publish|release|compliance|gate|feedback/.test(id)) return 'publish';
  if (/validat|regression|review|check/.test(id)) return 'validate';
  return 'model';
}

const NW = 150; // 节点宽
const NG = 48; // 节点间距
const PILL_H = 44;
const PILL_GAP = 8;
const X = (i) => i * (NW + NG);

// 图下那句话的宽度估算（12px：汉字和全角标点按 12，其余按 7）
export const textWidth = (text) => [...String(text || '')].reduce((w, ch) => w + (/[\u2E80-\uFFFF]/.test(ch) ? 12 : 7), 0);

/**
 * 主线图布局（设计稿 svg：节点 150×46，间距 48，插入点是节点下方的蓝色胶囊，虚线连到节点底边）。
 * 同一段有几个插入点时竖着往下排；图下那句话居中在插入点之间，放不下时往里收。返回坐标，组件只负责画。
 */
export function flowLayout(insertions = [], { kernel = false, caption = '' } = {}) {
  const width = X(MAINLINE.length - 1) + NW + 2;
  const nodes = MAINLINE.map(([id, title], i) => ({ id, title, x: X(i), y: 23, w: NW, h: 46, hi: kernel }));
  const edges = MAINLINE.slice(0, -1).map((_, i) => ({ x1: X(i) + NW, x2: X(i + 1), y: 46 }));
  const stack = {};
  const pills = (kernel ? [] : insertions).map((ins) => {
    const at = insertionAt(ins);
    const i = MAIN_IDS.indexOf(at);
    const k = stack[at] || 0;
    stack[at] = k + 1;
    const y = 88 + k * (PILL_H + PILL_GAP);
    return {
      // 插入点下的小字（ins.note，如「节点钩子 · 写入即查之后」）是后端下发的说明，不画（口径 5）
      id: ins.id, at, title: ins.title || ins.id, sub: '',
      x: X(i) + 6, y, w: NW - 12, h: PILL_H, cx: X(i) + NW / 2,
      lineFrom: k ? y - PILL_GAP : 69, lineTo: y,
    };
  });
  const bottom = pills.length ? Math.max(...pills.map((p) => p.y + p.h)) : 69;
  const captionY = bottom + 22;
  let captionX = width / 2;
  if (pills.length) {
    const xs = pills.map((p) => p.x - 6);
    captionX = (Math.min(...xs) + Math.max(...xs) + NW) / 2;
  }
  const half = Math.min(textWidth(caption), width) / 2;
  captionX = Math.min(Math.max(captionX, half), width - half);
  return { width, height: captionY + 8, nodes, edges, pills, caption: { x: captionX, y: captionY } };
}

/** 图下方那句话：只用包自带的说明（处数和统计卡重复，不写）；不往流程里加步骤时说一句 */
export function flowCaption(d, kernel) {
  const n = d?.flow_insertions?.length || 0;
  if (kernel || n) return d?.flow_note || '';
  return d?.flow_note || '这个包不往流程里加步骤';
}

// ---------- 包含什么 ----------
const GROUP_ICON = {
  加进流程: 'branch', 命令: 'terminal', 助手角色: 'bot', 子代理: 'bot', 技能: 'sparkle', 脚本: 'code', 模板: 'filetext',
  检查规则: 'gate', 界面入口: 'layout', 报告格式: 'list', 可调设置: 'sliders', 参考指南: 'book', 术语: 'book', 规则: 'scale',
  参考对象: 'cube', 示例项目: 'folder', 表字典: 'table', 状态码: 'tag', 证据类型: 'db', 只读保障: 'shieldok', 凭据字段: 'key', 登记要求: 'filetext',
};

/** 可调设置一项的说法：字符串原样；{label, value} →「每段最多场景数 6」；清单原样给的 {id, default} 也认 */
export function configText(item) {
  if (item == null) return '';
  if (typeof item !== 'object') return String(item);
  const name = item.label || item.title || item.id || item.key || '';
  const v = item.value ?? item.default;
  return v == null || v === '' ? name : `${name} ${v}`;
}

/** 界面入口按 PackUi 推出来（真实后端的 contents 里没有这一组） */
function uiEntries(ui) {
  if (ui?.kind === 'page') return (ui.pages || []).map((p) => `左栏页面「${p.title}」`);
  if (ui?.kind === 'subpages') return (ui.subpages || []).map((p) => `资料页子页「${p.title}」`);
  if (ui?.kind === 'kernel') return ['工作台', 'Studio', '资料', '发布'];
  return [];
}

/**
 * 「包含什么」分组：加进流程（flow_insertions）→ contents 各组 → 界面入口（ui）→ 可调设置（config）。
 * contents 自带「加进流程」「界面入口」时用它的（带说明），否则按别的字段补；没有内容的组不出现。
 * count 是组里的条数（contents 带 total 时用 total，如行业包的「术语 312」）。
 */
export function contentGroups(d) {
  const contents = (d?.contents || []).filter((g) => g && g.group);
  const has = (name) => contents.some((g) => g.group === name);
  const out = [];
  const push = (title, items, note, total) => {
    const list = (items || []).map((x) => (typeof x === 'string' ? x : x?.title || x?.id || String(x)));
    if (!list.length && !total) return;
    out.push({ key: title, title, icon: GROUP_ICON[title] || 'dot', items: list, note: note || null, count: total ?? list.length });
  };
  if (!has('加进流程')) push('加进流程', (d?.flow_insertions || []).map((x) => x.title || x.id));
  contents.forEach((g) => push(g.group, g.items, g.note, g.total));
  if (!has('界面入口')) push('界面入口', uiEntries(d?.ui));
  if (!has('可调设置')) push('可调设置', (d?.config || []).map(configText));
  return out;
}
export const contentTotal = (groups) => groups.reduce((s, g) => s + g.count, 0);

// ---------- 英雄区 ----------
const DEP_LABEL = { core: '基础建模能力' };
/** 依赖范围的说法：">=1.4" →「1.4 以上」，">=1.0 <2.0" →「1.0 以上、2.0 以下」 */
export function rangeText(range) {
  const toks = String(range || '').trim().split(/\s+/).filter(Boolean);
  return toks.map((t) => {
    const m = t.match(/^(>=|<=|>|<|=)?(.+)$/);
    return { '>=': `${m[2]} 以上`, '>': `高于 ${m[2]}`, '<': `${m[2]} 以下`, '<=': `${m[2]} 及以下` }[m[1]] || m[2];
  }).join('、');
}
/** 「需要 基础建模能力 1.4 以上」；depends 是 {包名: 范围} */
export function dependsText(depends, labels = {}) {
  return Object.entries(depends || {}).map(([name, range]) => `${labels[name] || DEP_LABEL[name] || name} ${rangeText(range)}`.trim()).join('；');
}

export function fmtSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
export function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 英雄区下方那一行：需要、发布、本项目启用于（没有的项不出；大小不影响启用判断，不列） */
export function heroFacts(d) {
  return [
    ['需要', dependsText(d?.requires?.depends)],
    ['发布', fmtDate(d?.released_at)],
    ['本项目启用于', fmtDate(d?.enabled_at)],
  ].filter(([, v]) => v);
}

/**
 * 英雄区右侧：menu 是「已启用到本项目」那个按钮（点开下拉菜单），primary 是主按钮，hint 是下面那句说明。
 * 已启用、必装、待生效、可启用时不写说明：生效时机由按钮、启用弹窗和 toast 说。
 * o：{pending: PL-05 的 pending, hasProject, admin, requested}
 */
export function heroActions(d, o = {}) {
  const st = d?.project_status;
  if (d?.collector) {
    if (!d.collector.installed) return { primary: { label: '未安装', disabled: true, kind: '' }, hint: '需要管理员部署。' };
    return {
      primary: { label: '平台已安装', icon: 'check', disabled: true, kind: 'soft' },
      hint: d.collector.self_service ? '在项目的「资料 → 数据源」里自己登记。' : '在项目的「资料 → 数据源」里登记，需要管理员。',
    };
  }
  if (!o.hasProject && d?.category !== 'provider') return { hint: '先在「我的项目」里建一个项目，才能启用到项目。' };
  switch (st) {
    case 'required': return { menu: { label: '必装 · 已启用', icon: 'lock' } };
    case 'enabled': return { menu: { label: '已启用到本项目', icon: 'check' } };
    case 'update':
      return { menu: { label: '已启用到本项目', icon: 'check' }, primary: { label: `更新到 ${d.latest}`, icon: 'refresh', action: 'update' } };
    case 'pending': {
      const p = o.pending;
      const what = p?.kind === 'disable' ? '停用' : p?.kind === 'enable' ? '已启用' : p?.to ? `${p.to}` : '已启用';
      const when = p?.effective === 'next_autobuild' ? '下次自主构建时生效' : '下一条消息生效';
      return { menu: { label: `${what} · ${when}`, icon: 'clock' } };
    }
    case 'available':
      return { primary: { label: '启用到本项目', icon: 'plus', action: 'enable' } };
    case 'offline': return { primary: { label: '启用', disabled: true, title: '离线部署不可用', kind: '' }, hint: '离线部署不可用：这个包要联网。' };
    case 'not_applicable': return { primary: { label: '本项目不适用', disabled: true, kind: '' }, hint: d.usage_note || '本项目不适用这个包。' };
    case 'platform_installed':
      return { primary: { label: '平台已安装', icon: 'check', disabled: true, kind: 'soft' }, hint: d.converter ? '' : '装在连接器网关里；登记数据源在项目的资料页里。' };
    case 'platform_available':
      if (o.requested) return { primary: { label: '已申请安装', icon: 'check', disabled: true, kind: '' }, hint: '已发送安装申请，等管理员处理。' };
      return {
        primary: { label: '安装到平台', icon: 'download', action: 'install' },
        hint: o.admin ? '装进连接器网关，装好后所有项目都能登记这类数据源。' : '需要管理员安装，点了会发送安装申请。',
      };
    default: return { hint: '' };
  }
}

/**
 * 「已启用到本项目」下拉菜单（R14，设计稿 pdMenu）：只写动作——停用、只在下次自主构建时启用新版本、在项目设置里查看；
 * 有待生效的切换时多一项「撤销切换」。停用的后果在点了之后的确认框里说（disableConfirm）
 */
export function heroMenu(d, o = {}) {
  let disable = { label: '停用' };
  if (d?.project_status === 'required') disable = { label: '停用', disabled: true, title: '必装，不能停用' };
  else if (o.pending?.kind === 'disable') disable = { label: '停用', disabled: true, title: '已排队，下一条消息生效' };
  const canLater = d?.project_status === 'update';
  return [
    { key: 'disable', icon: 'ban', ...disable },
    ...(o.pending ? [{ key: 'cancel_pending', icon: 'undo', label: '撤销切换' }] : []),
    { key: 'next_autobuild', icon: 'rocket', label: '只在下次自主构建时启用新版本', disabled: !canLater, title: canLater ? null : '没有可更新的版本' },
    '-',
    { key: 'settings', icon: 'gear', label: '在项目设置里查看' },
  ];
}

/** 停用前的确认（R14）：后果只在这里说一次 */
export function disableConfirm(d) {
  const ui = d?.ui;
  let what = '';
  if (ui?.kind === 'page') what = `左栏${ui.pages.map((p) => `「${p.title}」`).join('')}页面会消失，`;
  else if (ui?.kind === 'subpages') what = `资料页的 ${ui.subpages.length} 个子页会消失，`;
  return { title: `停用${d?.label || ''}？`, body: `${what}已有产物保留。` };
}

/**
 * 转换器的默认（PL-20、PL-21，ADR-0073）：只看这个转换器能转、又有不止一个转换器的文件类型；只有一个时不出。
 * 返回 [{type, label, isDefault, canSet}]；canSet 只给管理员
 */
export function converterDefaults(d, converters, { admin = false } = {}) {
  if (!d?.converter || !converters?.types) return [];
  return converters.types
    .filter((row) => d.converter.types.includes(row.type) && (row.converters || []).length > 1)
    .map((row) => ({ type: row.type, label: row.label, isDefault: row.default === d.name, canSet: admin && row.default !== d.name }));
}

// ---------- 统计卡 ----------
// 两格：本项目产出、运行情况（「加进流程」那格和上面的主线图重复，不出）
export function statCards(d, { hasProject } = {}) {
  const ps = d?.project_stats;
  const where = d?.category === 'provider' ? '在平台上' : '在本项目产出';
  const none = !hasProject && d?.category !== 'provider' ? '没有选项目' : ['required', 'enabled', 'update', 'pending'].includes(d?.project_status) || d?.category === 'provider' ? '暂时没有统计' : '本项目没有启用';
  const second = ps?.produced ? { label: where, value: ps.produced.value, unit: ps.produced.unit, desc: ps.produced.note } : { label: where, value: '—', desc: none };
  const third = ps?.metric ? { label: ps.metric.label, value: ps.metric.value, desc: ps.metric.note } : { label: '运行情况', value: '—', desc: none };
  return [second, third];
}

// ---------- 更新记录 ----------
export function changelogView(changelog) {
  return (changelog || []).map((c) => ({
    version: c.version,
    date: fmtDate(c.date),
    notes: c.notes || '',
    chips: (c.tags || []).map((t) => (t === 'in_use'
      ? { text: '本项目在用', tone: 'blue', icon: 'check' }
      : t === 'needs_core_upgrade' ? { text: '需要先升级基础建模能力', tone: 'amber', icon: 'alert' } : { text: t, tone: 'gray', icon: null })),
  }));
}

// ---------- 采集类连接器 ----------
const EVIDENCE_TYPE_LABELS = {
  entity: '实体', field: '字段', relation: '关系', constraint: '约束', operation: '操作', rule: '规则', flow: '流程', profile: '数据画像', sample: '脱敏样本', term: '术语',
};

/** 采集类连接器详情的几行（没有包内容、流程节点和版本记录）：能采什么、只读方式、许可、怎么登记 */
export function collectorFacts(d) {
  const c = d?.collector;
  if (!c) return [];
  const types = (c.evidence_types || []).map((x) => EVIDENCE_TYPE_LABELS[x] || x).join('、');
  return [
    ['能采', types],
    ['只读方式', c.readonly_method],
    ['许可', c.license_note],
    ['登记', c.self_service ? '可自己登记' : '需管理员登记'],
  ].filter(([, v]) => v);
}
