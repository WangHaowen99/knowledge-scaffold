// 资料区（设计稿 resources）的纯逻辑：分组与行文字、跳转参数、出处编号、上传校验、被引用、外部反馈、表格与 drawio 预览

// R-01 拿不到 purposes 时用的兜底（说明文字同设计稿 EVD_PURPOSE）
export const PURPOSE_FALLBACK = [
  { value: 'goal', label: '建模目标依据', description: '说明要解决什么问题、给谁用' },
  { value: 'schema', label: '数据 Schema', description: '含表结构，用于对象与数据表映射' },
  { value: 'term', label: '术语表', description: '业务术语与别名' },
  { value: 'evidence', label: '通用业务依据', description: '规则、制度、样例等' },
  { value: 'auto', label: '由助手判断', description: '资料分析时按内容判断用途' },
];

export const GROUP_ICON = { diagrams: 'flow', excel: 'table', others: 'filetext' };

// 行首图标：设计稿 .evd-f .fi 的三种底色（d 流程图、x 表格、w 文档）
export function fileIcon(kind) {
  if (kind === 'drawio') return { cls: 'd', icon: 'flow' };
  if (kind === 'xls' || kind === 'csv') return { cls: 'x', icon: 'table' };
  if (kind === 'html') return { cls: 'w', icon: 'globe' };
  if (kind === 'image') return { cls: 'w', icon: 'image' };
  return { cls: 'w', icon: 'filetext' };
}
// 预览卡标题旁的图标（设计稿 PV 表）
export const PREVIEW_ICON = { doc: 'filetext', xls: 'table', csv: 'list', drawio: 'flow', html: 'globe' };

export function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const two = (n) => String(n).padStart(2, '0');
// 两天内上传的带时刻（「10-30 21:40」），更早的只写日期（「10-16」）
export function shortTime(iso, now = Date.now()) {
  if (!iso) return '';
  const d = new Date(iso);
  const day = `${two(d.getMonth() + 1)}-${two(d.getDate())}`;
  return now - d.getTime() < 48 * 3600 * 1000 ? `${day} ${two(d.getHours())}:${two(d.getMinutes())}` : day;
}
export function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `${two(d.getMonth() + 1)}-${two(d.getDate())}`;
}

const num = (n) => Number(n || 0).toLocaleString('en-US');

// 表格的规模：「3 个工作表 · 1,204 行」「412 行」；拿不到就用大小
export function scaleText(m) {
  if (m.kind === 'xls' && m.sheets) return `${m.sheets} 个工作表 · ${num(m.rows)} 行`;
  if ((m.kind === 'xls' || m.kind === 'csv') && m.rows) return `${num(m.rows)} 行`;
  return fmtSize(m.size);
}

// 行的第二行：规模 · 上传时间 · 资源模板 · 被引用（修订资料的「已在对话里发起增量更新 →」由页面另画）
export function metaText(m, now = Date.now()) {
  const parts = [scaleText(m), `${shortTime(m.uploaded_at, now)} 上传`];
  if (m.is_template) parts.push('资源模板');
  if (m.referenced) parts.push(`被引用 ${m.referenced} 处`);
  return parts.join(' · ');
}

// 用途按钮上的文字；「自动识别」只在上传时自动判出来、用户没改过时显示（「由助手判断」本身不标）
export function purposeView(m, purposes = PURPOSE_FALLBACK) {
  const p = purposes.find((x) => x.value === m.purpose) || PURPOSE_FALLBACK.find((x) => x.value === m.purpose);
  return { label: p?.label || m.purpose || '由助手判断', auto: !!m.purpose_auto && m.purpose !== 'auto' };
}

export const allMaterials = (groups = []) => groups.flatMap((g) => g.items || []);
export const findMaterial = (groups, path) => allMaterials(groups).find((m) => m.path === path) || null;

// 默认选中被引用最多的那份（同样多时取文档组里靠前的）；没有引用就选第一份
export function pickDefault(groups = []) {
  const all = allMaterials(groups);
  if (!all.length) return null;
  const best = all.reduce((a, b) => ((b.referenced || 0) > (a.referenced || 0) ? b : a), all[0]);
  return best.path;
}

// 正在发起增量更新的修订资料（横幅）：取最近上传的那份
export function revisionBanner(groups = []) {
  const list = allMaterials(groups).filter((m) => m.is_revision && m.revision_task);
  if (!list.length) return null;
  return list.reduce((a, b) => (String(b.uploaded_at) > String(a.uploaded_at) ? b : a));
}

// ---------- 跳转（Studio「查看原文」、待处理「上传文本版」「逐条查看」、资料分析「补传资料」） ----------

// 文件锚点 file:<路径>#<片段>（契约 1.10 / 7.17，名字里的特殊字符可能百分号编码）
export function parseFileAnchor(anchor) {
  const m = /^file:([^#]+)(?:#(.*))?$/.exec(String(anchor || ''));
  if (!m) return null;
  const dec = (x) => { try { return decodeURIComponent(x); } catch { return x; } };
  return { path: dec(m[1]), fragment: m[2] ? dec(m[2]) : null };
}

// 地址参数 → 要选中什么：?anchor=、?path=、?group=feedback&feedback=
export function routeTarget(query = {}) {
  if (query.feedback || query.group === 'feedback') return { kind: 'feedback', id: query.feedback || null };
  if (query.anchor) {
    const a = parseFileAnchor(query.anchor);
    if (a) return { kind: 'file', path: a.path, fragment: a.fragment };
  }
  if (query.path) return { kind: 'file', path: query.path, fragment: query.fragment || null };
  return null;
}
export const targetKey = (t) => (t ? JSON.stringify(t) : '');

// 片段的种类：文档块（p:9.2、L12、table:1/row:2/cell:3）或表格单元格（sheet:品类/cell:D1、sheet:Sheet1!A1）
export function parseFragment(fragment) {
  const f = String(fragment || '');
  if (!f) return null;
  const sheet = /^sheet:([^/!]+)(?:\/cell:|!)([A-Z]+\d+)$/i.exec(f);
  if (sheet) return { type: 'cell', sheet: sheet[1], cell: sheet[2].toUpperCase() };
  return { type: 'block', anchor: f };
}

// 正文左侧的灰色编号（出处位置）：p:9.2 → §9.2；L12 → L12；table:1/row:2 → 表 1 · 行 2
export function blockLabel(anchor) {
  const a = String(anchor || '');
  let m = /^p:(.+)$/.exec(a);
  if (m) return `§${m[1]}`;
  m = /^table:(\d+)(?:\/row:(\d+))?(?:\/cell:(\d+))?$/.exec(a);
  if (m) return [`表 ${m[1]}`, m[2] ? `行 ${m[2]}` : '', m[3] ? `格 ${m[3]}` : ''].filter(Boolean).join(' · ');
  if (/^L\d+$/.test(a)) return a;
  return '';
}

// 正文开头的编号和左侧编号重复时去掉（「9.2 门店申请…」→「门店申请…」），高亮位置跟着平移
export function blockBody(block) {
  const text = block.text || '';
  const m = /^p:(.+)$/.exec(block.anchor || '');
  const lead = m ? new RegExp(`^${m[1].replace(/\./g, '\\.')}[\\s　]+`).exec(text) : null;
  const cut = lead ? lead[0].length : 0;
  const highlights = (block.highlights || [])
    .map((h) => ({ start: Math.max(0, h.start - cut), end: h.end - cut }))
    .filter((h) => h.end > h.start);
  return { text: text.slice(cut), highlights };
}

// 文字按高亮切段
export function splitHighlights(text, highlights = []) {
  const out = [];
  let pos = 0;
  [...highlights].sort((a, b) => a.start - b.start).forEach((h) => {
    const s = Math.max(pos, h.start);
    if (s > pos) out.push({ t: text.slice(pos, s), hit: false });
    if (h.end > s) out.push({ t: text.slice(s, h.end), hit: true });
    pos = Math.max(pos, h.end);
  });
  if (pos < text.length) out.push({ t: text.slice(pos), hit: false });
  return out;
}

// 读到哪的 chip：读全（teal）/ 读了一部分（amber）/ 还没读
export function coverageChip(cov) {
  if (!cov) return null;
  if (cov.complete) return { tone: 'teal', icon: 'check', text: `${cov.read_sections || '全文'}已读全` };
  if (!cov.read_pct) return null;
  return { tone: 'amber', icon: null, text: cov.read_sections || `已读 ${cov.read_pct}%` };
}

// ---------- 上传 ----------
export const UPLOAD_EXTS = ['md', 'markdown', 'doc', 'docx', 'xls', 'xlsx', 'xlsm', 'csv', 'pdf', 'drawio', 'txt', 'html', 'htm'];
export const UPLOAD_LIMIT = 20 * 1024 * 1024;
export const UPLOAD_ACCEPT = UPLOAD_EXTS.map((x) => `.${x}`).join(',');
const DRAWING_EXTS = ['dwg', 'dxf', 'vsd', 'vsdx', 'png', 'jpg', 'jpeg', 'svg', 'gif', 'bmp', 'webp'];

export const extOf = (name) => (String(name).includes('.') ? String(name).split('.').pop().toLowerCase() : '');

// 选完文件先在本地挡掉格式不对、超过 20 MB 的（标注 resources-003）
export function checkUpload(name, size) {
  const ext = extOf(name);
  if (!UPLOAD_EXTS.includes(ext)) {
    return DRAWING_EXTS.includes(ext) ? '不支持这种格式，请转成 PDF 或 drawio 后再上传' : '不支持这种格式，请转成 PDF、Word 或 Markdown 后再上传';
  }
  if (size > UPLOAD_LIMIT) return '超过 20 MB，请拆分或压缩后再上传';
  return null;
}

// 替换：用户选的文件改成被替换那份的名字上传（同名即替换），扩展名要一样
export function replaceName(target, picked) {
  if (extOf(target.name) !== extOf(picked)) return { error: `替换要用同类型的文件（.${extOf(target.name)}）` };
  return { name: target.name };
}

// ---------- 被引用（R-05） ----------
const KIND_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本' };
const TYPE_LABEL = { 'trace.scenario': '场景', 'diagram.flowchart': '流程图' };
export const targetName = (t) => t?.name || t?.title || t?.key || '';
export const targetKind = (t) => (t?.kind ? KIND_LABEL[t.kind] || t.kind : TYPE_LABEL[t?.type] || '产物');

export function refRows(items = []) {
  return items.map((x) => ({
    title: targetName(x.target),
    sub: [targetKind(x.target), x.location].filter(Boolean).join(' · '),
    anchors: x.anchors || [],
    chip: (x.anchors || []).map(blockLabel).filter(Boolean).join(' · '),
    target: x.target,
  }));
}
// 出处数：每条引用至少算一处
export const refCount = (items = []) => items.reduce((n, x) => n + Math.max(1, (x.anchors || []).length), 0);

// 删除弹窗的提示（标注 resources-016）：「操作「…」的决策依据、场景「…」」，多了只列前 3 条
export function refSentence(items = []) {
  if (!items.length) return '';
  const names = items.slice(0, 3).map((x) => `${targetKind(x.target)}「${targetName(x.target)}」${x.location ? `的${x.location}` : ''}`);
  const more = items.length > 3 ? ` 等 ${items.length} 项` : '';
  return `有 ${refCount(items)} 处出处引用了这份资料：${names.join('、')}${more}。删除后它们会变成「来源无法核验」，需要换出处或重新核验。`;
}

// 点引用去哪：条目去 Studio 表格，流程图去 Studio 表格的流程图行，场景去场景走查
export function refTarget(t) {
  if (!t) return null;
  if (t.id && t.kind) return { page: 'studio-table', query: { entry: t.id } };
  if (t.type === 'diagram.flowchart') return { page: 'studio-table', query: { entry: `flowchart:${t.key}` } };
  if (t.type === 'trace.scenario') return { page: 'scenarios', query: { key: t.key } };
  return null;
}

// ---------- 外部反馈（R-11） ----------
export const FEEDBACK_STATE = {
  open: ['未处理', 'amber', null],
  handled: ['已处理', 'teal', 'check'],
  dismissed: ['已忽略', 'gray', null],
};

// summary「评测失分用例 4 条 · 多数和「…」有关」拆成标题和说明
export function feedbackView(fb, now = Date.now()) {
  const [title, ...rest] = String(fb.summary || '').split(' · ');
  const note = rest.join(' · ');
  const evaluated = /评测/.test(fb.source?.service || '');
  const meta = fb.state === 'open'
    ? [`${fb.source?.service || '外部'}回传`, shortTime(fb.received_at, now), note]
    : [note, shortDate(fb.received_at), fb.state === 'handled' ? '已转成修订建议' : '已忽略'];
  return {
    title: title || '外部反馈',
    note,
    meta: meta.filter(Boolean).join(' · '),
    icon: evaluated ? 'target' : 'msg',
    open: fb.state === 'open',
    state: FEEDBACK_STATE[fb.state] || FEEDBACK_STATE.open,
  };
}

// ---------- 表格（F-05） ----------
export function columnLetter(index) {
  let label = '';
  let n = index;
  do {
    label = String.fromCharCode(65 + (n % 26)) + label;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return label;
}

// 首行当表头：表头写成「A 商品」，正文行号从 2 开始
export function sheetView(sheet) {
  const rows = sheet?.rows || [];
  const cols = Math.max(0, ...rows.map((r) => r.length));
  const head = rows[0] || [];
  return {
    header: Array.from({ length: cols }, (_, c) => `${columnLetter(c)} ${head[c] ?? ''}`.trim()),
    body: rows.slice(1).map((r, i) => ({ n: i + 2, cells: Array.from({ length: cols }, (_, c) => r[c] ?? '') })),
  };
}

// 单元格「D1」→ {row: 1, col: 3}（行从 1 起，列从 0 起）
export function cellPos(ref) {
  const m = /^([A-Z]+)(\d+)$/.exec(String(ref || '').toUpperCase());
  if (!m) return null;
  const col = [...m[1]].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  return { row: Number(m[2]), col };
}

export const tableTruncated = (data, sheet) => !!data?.sheet_truncated || !!sheet?.truncated || (sheet?.total_rows || 0) > 500;

// CSV 原样显示：含逗号、引号、换行的字段加引号
export function csvText(rows = []) {
  const cell = (v) => {
    const s = String(v ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return rows.map((r) => r.map(cell).join(',')).join('\n');
}

// ---------- drawio（F-02） ----------
const unescapeXml = (s) => String(s || '')
  .replace(/&#xa;|&#10;/gi, '\n').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'").replace(/&amp;/g, '&');
const attrs = (s) => {
  const out = {};
  String(s || '').replace(/([\w:-]+)="([^"]*)"/g, (_, k, v) => { out[k] = unescapeXml(v); return ''; });
  return out;
};
const styleMap = (style) => {
  const out = {};
  String(style || '').split(';').filter(Boolean).forEach((kv, i) => {
    const j = kv.indexOf('=');
    if (j < 0) out[i === 0 ? '_shape' : kv] = kv; else out[kv.slice(0, j)] = kv.slice(j + 1);
  });
  return out;
};
const plain = (html) => unescapeXml(String(html || '').replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, '')).trim();

/**
 * 读明文 drawio：节点（绝对坐标）、连线（含拐点）。压缩存储的返回 {compressed: true}，页面改用官方查看器。
 */
export function parseDrawio(xml) {
  const text = String(xml || '');
  const diagram = /<diagram\b[^>]*>([\s\S]*?)<\/diagram>/.exec(text);
  if (diagram && !/<mxGraphModel/.test(diagram[1]) && diagram[1].trim()) return { compressed: true, nodes: [], edges: [] };
  const cells = [];
  const re = /<mxCell\b([^>]*?)(?:\/>|>([\s\S]*?)<\/mxCell>)/g;
  let m;
  while ((m = re.exec(text))) {
    const a = attrs(m[1]);
    const inner = m[2] || '';
    const geo = /<mxGeometry\b([^>]*?)(?:\/>|>)/.exec(inner);
    const g = geo ? attrs(geo[1]) : {};
    const arr = /<Array\b[^>]*as="points"[^>]*>([\s\S]*?)<\/Array>/.exec(inner);
    const points = [];
    if (arr) arr[1].replace(/<mxPoint\b([^>]*)\/>/g, (_, pa) => { const q = attrs(pa); points.push({ x: Number(q.x) || 0, y: Number(q.y) || 0 }); return ''; });
    cells.push({ ...a, g, points });
  }
  const byId = new Map(cells.map((c) => [c.id, c]));
  const offset = (c) => {
    let x = 0; let y = 0;
    let p = byId.get(c.parent);
    while (p && p.vertex === '1') { x += Number(p.g.x) || 0; y += Number(p.g.y) || 0; p = byId.get(p.parent); }
    return { x, y };
  };
  const nodes = cells.filter((c) => c.vertex === '1').map((c) => {
    const o = offset(c);
    return {
      id: c.id, label: plain(c.value), style: styleMap(c.style),
      x: (Number(c.g.x) || 0) + o.x, y: (Number(c.g.y) || 0) + o.y, w: Number(c.g.width) || 80, h: Number(c.g.height) || 40,
    };
  });
  const edges = cells.filter((c) => c.edge === '1' && c.source && c.target).map((c) => ({
    id: c.id, source: c.source, target: c.target, label: plain(c.value), style: styleMap(c.style), points: c.points,
  }));
  return { compressed: false, nodes, edges };
}

const shapeOf = (st) => {
  if (st._shape === 'rhombus' || st.shape === 'rhombus') return 'diamond';
  if (st._shape === 'ellipse' || st.shape === 'ellipse') return 'ellipse';
  return 'rect';
};
// 从中心朝 p 的方向，和节点边界的交点
function clip(n, toward) {
  const cx = n.x + n.w / 2; const cy = n.y + n.h / 2;
  const dx = toward.x - cx; const dy = toward.y - cy;
  if (!dx && !dy) return { x: cx, y: cy };
  const rx = n.w / 2; const ry = n.h / 2;
  const shape = shapeOf(n.style);
  let t;
  if (shape === 'diamond') t = 1 / (Math.abs(dx) / rx + Math.abs(dy) / ry);
  else if (shape === 'ellipse') t = 1 / Math.sqrt((dx / rx) ** 2 + (dy / ry) ** 2);
  else t = Math.min(dx ? rx / Math.abs(dx) : Infinity, dy ? ry / Math.abs(dy) : Infinity);
  return { x: cx + dx * t, y: cy + dy * t };
}

/**
 * 画成 SVG 用的几何：viewBox 留边、节点形状与颜色、连线折线与标签位置。
 */
export function drawioLayout(model, pad = 16) {
  const nodes = (model?.nodes || []).map((n) => {
    const st = n.style || {};
    const shape = shapeOf(st);
    const pill = st.rounded === '1' && Number(st.arcSize) >= 50;
    return {
      ...n, shape,
      rx: shape !== 'rect' ? 0 : pill ? n.h / 2 : st.rounded === '1' ? 8 : 2,
      fill: st.fillColor && st.fillColor !== 'none' ? st.fillColor : '#FFFFFF',
      stroke: st.strokeColor && st.strokeColor !== 'none' ? st.strokeColor : '#93A1B5',
      dashed: st.dashed === '1',
      color: st.fontColor || '#0B1A33',
    };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = (model?.edges || []).map((e) => {
    const s = byId.get(e.source); const t = byId.get(e.target);
    if (!s || !t) return null;
    const center = (n) => ({ x: n.x + n.w / 2, y: n.y + n.h / 2 });
    const mid = e.points || [];
    const start = clip(s, mid[0] || center(t));
    const end = clip(t, mid[mid.length - 1] || center(s));
    const pts = [start, ...mid, end];
    const [a, b] = pts;
    const vertical = Math.abs(b.x - a.x) < Math.abs(b.y - a.y);
    const label = e.label ? { text: e.label, x: vertical ? (a.x + b.x) / 2 + 6 : (a.x + b.x) / 2, y: vertical ? (a.y + b.y) / 2 + 4 : (a.y + b.y) / 2 - 5, anchor: vertical ? 'start' : 'middle' } : null;
    return { id: e.id, d: pts.map((q, i) => `${i ? 'L' : 'M'}${q.x.toFixed(1)} ${q.y.toFixed(1)}`).join(' '), label };
  }).filter(Boolean);
  if (!nodes.length) return { viewBox: '0 0 360 200', width: 360, height: 200, nodes, edges };
  const minX = Math.min(...nodes.map((n) => n.x)) - pad;
  const minY = Math.min(...nodes.map((n) => n.y)) - pad;
  const maxX = Math.max(...nodes.map((n) => n.x + n.w)) + pad;
  const maxY = Math.max(...nodes.map((n) => n.y + n.h)) + pad;
  const width = maxX - minX; const height = maxY - minY;
  return { viewBox: `${minX} ${minY} ${width} ${height}`, width, height, nodes, edges };
}

// 菱形四个顶点
export const diamondPoints = (n) => `${n.x + n.w / 2},${n.y} ${n.x + n.w},${n.y + n.h / 2} ${n.x + n.w / 2},${n.y + n.h} ${n.x},${n.y + n.h / 2}`;
