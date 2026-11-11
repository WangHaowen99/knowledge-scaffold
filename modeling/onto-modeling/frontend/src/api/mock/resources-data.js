// mock · 资料区的合成资料（主示例「门店补货单编制」，全部合成）：
// 文件清单（R-01 的 Material）、文档正文（R-06 的块）、表格（F-05）、drawio 与 HTML 原文（F-02 / F-04）、
// 被谁引用（R-05）、外部反馈（R-11）。资料名称和用途以 scenario.js 的 MATERIALS / LATE_MATERIALS 为准，
// 另补设计稿里有的流程图、术语对照、台账导出、周报。资料分析（routes-materials.js）与资料区（routes-resources.js）共用这份清单。
import { MAX_RESOURCE_BYTES, RESOURCE_TYPES } from '../../lib/resourceUpload.js';
import { LATE_MATERIALS, MATERIALS, entryRef } from './scenario.js';

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const KB = 1024;

const atDaysAgo = (days, h, m) => {
  const d = new Date(Date.now() - days * 24 * HOUR);
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};

export const PURPOSE_VALUES = ['goal', 'schema', 'term', 'evidence', 'auto'];
// R-01 的 purposes：说明文字按设计稿（标注 resources-005、006）
export const PURPOSES = [
  { value: 'goal', label: '建模目标依据', description: '说明要解决什么问题、给谁用' },
  { value: 'schema', label: '数据 Schema', description: '含表结构，用于对象与数据表映射' },
  { value: 'term', label: '术语表', description: '业务术语与别名' },
  { value: 'evidence', label: '通用业务依据', description: '规则、制度、样例等' },
  { value: 'auto', label: '由助手判断', description: '资料分析时按内容判断用途' },
];

export function kindOf(name) {
  const ext = (String(name).split('.').pop() || '').toLowerCase();
  if (/^xls[xm]?$/.test(ext)) return 'xls';
  if (ext === 'csv') return 'csv';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'drawio') return 'drawio';
  if (/^html?$/.test(ext)) return 'html';
  if (/^(md|markdown|txt)$/.test(ext)) return 'text';
  if (/^(png|jpe?g|gif|webp)$/.test(ext)) return 'image';
  return 'doc';
}
export const groupOf = (kind) => (kind === 'xls' || kind === 'csv' ? 'excel' : kind === 'drawio' ? 'diagrams' : 'others');
const previewOfKind = (kind) => ({ xls: 'xls', csv: 'csv', drawio: 'drawio', html: 'html', image: null })[kind] ?? 'doc';

// 资料区上传白名单（契约 1.8、标注 resources-003）
// 与真后端同一份白名单与上限（lib/resourceUpload.js，契约 1.8）
export const UPLOAD_EXTS = RESOURCE_TYPES.map((x) => x.slice(1));
export const UPLOAD_LIMIT = MAX_RESOURCE_BYTES;

// 文本版状态（契约 7.4 MaterialText，ADR-0073）：md、txt、csv 本身是文本；流程图没有；其余按转换器判
export const CONVERTERS = [
  { id: 'convert-pdf', label: 'PDF 转换', version: '1.0.0', types: ['.pdf'], license: 'pdfplumber（MIT）、pdfminer.six（MIT）', official: true },
  { id: 'convert-docx', label: 'Word 转换', version: '1.0.0', types: ['.docx'], license: 'python-docx（MIT）', official: true },
  { id: 'convert-xlsx', label: 'Excel 转换', version: '1.0.0', types: ['.xlsx', '.xlsm'], license: 'openpyxl（MIT）', official: true },
];
export function textState(name, kind, { scanned = false, state = null } = {}) {
  const ext = `.${(String(name).split('.').pop() || '').toLowerCase()}`;
  const base = { state: 'native', reason: null, note: null, converter: null, updated_at: null, has_text: true };
  if (kind === 'drawio') return null;
  if (kind === 'text' || kind === 'csv') return base;
  const conv = CONVERTERS.find((c) => c.types.includes(ext));
  const unsupported = (reason) => ({ ...base, state: 'unsupported', reason, has_text: false });
  if (kind === 'image') return unsupported('图片暂不支持');
  if (!conv) return unsupported(ext === '.doc' || ext === '.xls' ? '老版 Office 格式暂不支持' : '这种格式暂不支持');
  if (scanned) return { ...unsupported('扫描件或图片型 PDF，暂不支持'), converter: { id: conv.id, label: conv.label, version: conv.version } };
  const s = state || 'converted';
  return { ...base, state: s, has_text: s === 'converted', converter: { id: conv.id, label: conv.label, version: conv.version },
    reason: s === 'failed' ? '文件损坏，打不开' : null, updated_at: new Date().toISOString() };
}

let seq = 0;
// 清单里的一份资料：Material 的字段，另带 mock 自用的 scanned、sections（读全时的范围说法）
export function fileRecord(o) {
  const kind = o.kind || kindOf(o.name);
  seq += 1;
  return {
    path: o.path, name: o.name, group: groupOf(kind), kind, size: Math.round(o.size ?? 64 * KB), uploaded_at: o.uploaded_at,
    ...(o.sheets != null ? { sheets: o.sheets } : {}), ...(o.rows != null ? { rows: o.rows } : {}),
    purpose: o.purpose || 'auto', purpose_auto: o.purpose_auto ?? true, reading_mode: o.reading_mode || 'on_demand',
    is_revision: !!o.is_revision, revision_task: o.revision_task || null, is_template: !!o.is_template,
    referenced: o.referenced || 0, coverage: o.coverage === undefined ? { read_pct: 100, label: o.sections || '全文' } : o.coverage,
    preview: previewOfKind(kind), revision: `r-${seq.toString(16)}`,
    text: o.text !== undefined ? o.text : textState(o.name, kind, { scanned: o.scanned, state: o.text_state }),
    scanned: !!o.scanned, sections: o.sections || null,
  };
}

// 给接口用的 Material：去掉 mock 自用字段；资料地图里有这份时，读取进度以资料地图为准
// 同后端 workspace_labels：资料名是原文件名去扩展名，类型按扩展名
const TYPE_LABEL = { pdf: 'PDF', doc: 'Word', docx: 'Word', xls: 'Excel', xlsx: 'Excel', xlsm: 'Excel', csv: 'CSV', drawio: '流程图', html: '网页', htm: '网页', png: '图片', jpg: '图片', jpeg: '图片', md: 'Markdown', markdown: 'Markdown', txt: '文本' };
export function materialView(f, mapItem) {
  const { scanned, sections, ...rest } = f;
  const ext = (/\.([^.]+)$/.exec(rest.name || '') || [])[1]?.toLowerCase();
  const m = { ...rest, title: (rest.name || '').replace(/\.[^.]+$/, ''), type_label: TYPE_LABEL[ext] || '文件' };
  if (!mapItem) return m;
  const done = mapItem.read_pct >= 100;
  return {
    ...m,
    purpose: mapItem.purpose, purpose_auto: mapItem.purpose_auto,
    coverage: { read_pct: mapItem.read_pct, label: done ? (sections || '全文') : (mapItem.position || `${mapItem.read_pct}%`) },
  };
}

// R-06 的 coverage
export function previewCoverage(f, mapItem) {
  if (mapItem) {
    const done = mapItem.read_pct >= 100;
    return { read_sections: done ? (f?.sections || '全文') : (mapItem.position || ''), read_pct: mapItem.read_pct, complete: done };
  }
  const c = f?.coverage;
  if (!c) return { read_sections: '', read_pct: 0, complete: false };
  return { read_sections: c.read_pct >= 100 ? (f.sections || c.label || '全文') : (c.read_pct ? c.label : ''), read_pct: c.read_pct, complete: c.read_pct >= 100 };
}

const byName = (list, name) => list.find((x) => x.name === name);
const M = (name) => byName([...MATERIALS, ...LATE_MATERIALS], name);
const FLOW_PATH = 'resource/diagrams/门店补货流程.drawio';
const LEGEND_PATH = 'resource/diagrams/图示说明定义.drawio';
const GLOSSARY_PATH = 'resource/excel/08-术语对照.xlsx';
const LEDGER_PATH = 'resource/excel/10-补货台账导出.csv';
const REPORT_PATH = 'resource/others/11-补货周报.html';
export const RES_PATHS = { flow: FLOW_PATH, legend: LEGEND_PATH, glossary: GLOSSARY_PATH, ledger: LEDGER_PATH, report: REPORT_PATH };

// 规模与读全时的范围说法；被引用次数是 R-05 里出处数加起来
const SPEC = {
  '01-场景说明.docx': { size: 42 * KB, sections: '§1–§4' },
  '02-门店补货规则.docx': { size: 312 * KB, sections: '第 1–12 章' },
  '03-样例补货单-A门店.docx': { size: 88 * KB },
  '03-样例补货单-B.docx': { size: 91 * KB },
  '04-商品陈列说明.pdf': { size: 1.2 * 1024 * KB, sections: '第 1–3 页' },
  '05-门店清单.xlsx': { size: 64 * KB, sheets: 2, rows: 318 },
  '06-商品品类表.xlsx': { size: 238 * KB, sheets: 3, rows: 1204 },
  '07-门店C特殊需求.pdf': { size: 2.4 * 1024 * KB, scanned: true, coverage: { read_pct: 0, label: '扫描件，读不出文字' } },
  '09-修订通知.docx': { size: 36 * KB },
};

// 这一阶段的资料清单。stage：materials（读资料中，只有最早那 7 份）/ 空阶段（还没传）/ 其余（默认，全部资料）
export function catalogFor(stage, { cid = null, map = null } = {}) {
  const empty = !stage || /empty$/.test(stage);
  if (empty) return [];
  const spec = (m) => SPEC[m.name] || {};
  if (stage === 'materials') {
    const today = atDaysAgo(0, 9, 13);
    return syncPurpose(MATERIALS.map((m) => fileRecord({ ...m, ...spec(m), referenced: 0, uploaded_at: today, purpose_auto: true, coverage: null })), map);
  }
  const old = atDaysAgo(16, 10, 20);
  const doc = (name, o = {}) => fileRecord({ ...M(name), ...spec(M(name)), uploaded_at: old, purpose_auto: false, referenced: anchorCount(REFERENCES[name] || []), ...o });
  const list = [
    fileRecord({ path: FLOW_PATH, name: '门店补货流程.drawio', size: 186 * KB, uploaded_at: old, purpose: 'evidence', purpose_auto: false }),
    fileRecord({ path: LEGEND_PATH, name: '图示说明定义.drawio', size: 24 * KB, uploaded_at: old, purpose: 'auto', purpose_auto: false, is_template: true }),
    doc('05-门店清单.xlsx', { purpose_auto: true }),
    doc('06-商品品类表.xlsx', { purpose_auto: true }),
    fileRecord({ path: GLOSSARY_PATH, name: '08-术语对照.xlsx', size: 22 * KB, sheets: 1, rows: 86, uploaded_at: atDaysAgo(15, 14, 5), purpose: 'term', purpose_auto: true }),
    fileRecord({ path: LEDGER_PATH, name: '10-补货台账导出.csv', size: 38 * KB, rows: 412, uploaded_at: atDaysAgo(14, 16, 40), purpose: 'schema', purpose_auto: true }),
    doc('01-场景说明.docx'),
    doc('02-门店补货规则.docx'),
    doc('03-样例补货单-A门店.docx'),
    doc('03-样例补货单-B.docx'),
    doc('04-商品陈列说明.pdf', { purpose_auto: true }),
    doc('07-门店C特殊需求.pdf', { uploaded_at: atDaysAgo(3, 15, 2), purpose_auto: true }),
    doc('09-修订通知.docx', {
      uploaded_at: atDaysAgo(1, 21, 40), purpose_auto: true, is_revision: true,
      revision_task: { conversation_id: cid, label: '识别出 4 处变更点，等你确认清单' },
    }),
    fileRecord({ path: REPORT_PATH, name: '11-补货周报.html', size: 68 * KB, uploaded_at: atDaysAgo(5, 11, 30), purpose: 'auto', purpose_auto: false }),
  ];
  return syncPurpose(list, map);
}

// 资料地图（资料分析）里已有的那几份，用途以资料地图为准
function syncPurpose(list, map) {
  (map?.items || []).forEach((it) => {
    const f = list.find((x) => x.path === it.path);
    if (f) Object.assign(f, { purpose: it.purpose, purpose_auto: it.purpose_auto });
  });
  return list;
}

// ---------- 文档正文（R-06） ----------
// mark：被产物引用的那句（出处高亮）；正文开头的编号和锚点一致，资料区画成左侧灰色编号
const blk = (anchor, kind, text, extra = {}) => ({ anchor, kind, text, ...extra });
const h = (n, text) => blk(`h:${n}`, 'heading', text, { level: 2 });
const p = (n, text, mark) => blk(`p:${n}`, 'paragraph', text, mark ? { mark } : {});
const DOCS = {
  '01-场景说明.docx': [
    h(1, '1 背景'),
    p(1, '店长只需说明门店、品类、库存与到货周期，系统自动生成补货单初稿。', '店长只需说明门店、品类、库存与到货周期'),
    h(2, '2 业务目标'),
    p(2, '由店长根据门店需求起草补货单，经区域审批后下单。', '补货单'),
    h(3, '3 使用人'),
    p(3, '店长、补货专员起草补货单；区域督导负责审批超量项。'),
    h(4, '4 成功标准'),
    p(4, '补货单一次通过率不低于 90%，超量项全部有审批记录。'),
  ],
  '02-门店补货规则.docx': [
    h(1, '第 1 章　总则'),
    p('1.1', '1.1 为规范门店补货单的编制，统一补货口径，制定本规则。'),
    p('1.2', '1.2 本规则适用于连锁门店向配送中心申请补货的全过程。'),
    h(2, '第 2 章　适用范围'),
    p('2.1', '2.1 适用于乳制品、烘焙、饮料、粮油、日化等常温与冷链商品。'),
    h(3, '第 3 章　术语'),
    p('3.1', '3.1 要货方：向配送中心申请补货的门店。', '要货方'),
    p('3.2', '3.2 标准上限：补货标准表中各品类单次补货的最大箱数。'),
    h(4, '第 4 章　补货申请'),
    p('4.1', '4.1 门店特殊需求应逐条登记，并注明来源文件与日期。', '门店特殊需求'),
    p('4.2', '4.2 补货申请由店长发起，写明门店、品类、库存与到货周期。'),
    h(5, '第 5 章　受理与必填项'),
    p('5.1', '5.1 补货申请缺少门店编码、品类或申请量的，退回店长补全。', '退回店长补全'),
    p('5.2', '5.2 各品类的标准上限按补货标准表取值。', '标准上限'),
    h(6, '第 6 章　安全库存'),
    p(6, '各品类安全库存按适用标准逐类取值。', '安全库存'),
    p('6.1', '6.1 适用标准以最新版陈列标准为准。', '适用标准'),
    p('6.2', '6.2 门店申请的安全库存低于标准的，按门店执行。'),
    h(7, '第 7 章　批次与抽检'),
    p('7.1', '7.1 每批到货按「先到先出」核对临期商品。'),
    p('7.2', '7.2 同一到货批次、同一品类算一批。', '同一到货批次、同一品类算一批'),
    h(8, '第 8 章　到货与陈列'),
    p('8.1', '8.1 到货后按品类上架，冷链商品优先。'),
    p('8.2', '8.2 到货周期为每日的商品，陈列量按日均销量核定。'),
    p('8.4', '8.4 到货周期为每周时，陈列量按周峰值核定，不得超过品类标准上限。', '陈列量按周峰值核定'),
    h(9, '第 9 章　超量处理'),
    p('9.1', '9.1 补货单各品类的申请量，应与适用标准逐项比较，比较结果列入超量清单。'),
    p('9.2', '9.2 门店申请与标准不一致时，应在补货单中逐条列出超量项，注明标准值与申请值，门店申请严于标准的按门店执行，超出标准的须经区域审批。', '门店申请严于标准的按门店执行'),
    p('9.3', '9.3 同一品类在多个章节出现时，取最严值。'),
    h(10, '第 10 章　临期与损耗'),
    p('10.1', '10.1 保质期剩余 ≤3 天的商品下架。'),
    p('10.2', '10.2 临期商品按四级临期分级处理。', '按四级临期分级'),
    h(11, '第 11 章　临时加量'),
    p('11.4', '11.4 对临时加量的商品，店长应生成提示并提交区域审批，审批通过前不得下单。', '店长应生成提示并提交区域审批'),
    p('11.5', '11.5 临时加量的有效期不超过一个到货批次。'),
    h(12, '第 12 章　台账'),
    p('12.1', '12.1 下单后的补货单登记入补货台账，保存期 1 年。', '补货台账'),
  ],
  '03-样例补货单-A门店.docx': [
    h(1, '第 1 节 门店信息'),
    p(1, 'A 门店，日均销量 85 箱，到货周期每日。'),
    h(3, '第 3 节 补货明细'),
    p(3, '牛奶 96 箱、酸奶 40 箱、面包 30 箱。'),
    h(5, '第 5 节 超量说明'),
    p(5, '牛奶：品类标准上限 120 箱，门店申请 96 箱（严于标准，按门店执行）。', '门店申请 96 箱（严于标准，按门店执行）'),
  ],
  '03-样例补货单-B.docx': [
    h(3, '第 3 节 门店信息'),
    p(3, '门店 B，日均销量 60 箱，到货周期隔日。'),
    h(4, '第 4 节 补货标准'),
    p(4, '牛奶按区域统一配额 ≤100 箱 执行。', '区域统一配额 ≤100 箱'),
    p('4.2', '酸奶按品类标准上限执行。'),
    h(5, '第 5 节 超量说明'),
    p(5, '本单无超量项。'),
  ],
  '04-商品陈列说明.pdf': [
    h(1, '1 陈列原则'),
    p(1, '同一品类集中陈列，先到先出。'),
    h(2, '2 冷链陈列'),
    p(2, '乳制品陈列面不少于 2 层，冷链温度记录每 4 小时一次。', '冷链温度记录每 4 小时一次'),
    h(3, '3 临期商品'),
    p(3, '保质期剩余 ≤3 天的商品移入临期专区。'),
  ],
  '09-修订通知.docx': [
    h(1, '修订通知'),
    p(1, '自本月起，乳制品区域配额上限调整为 150 箱。', '乳制品区域配额上限调整为 150 箱'),
    p(2, '适用条件：门店日均销量 ≥80 箱。'),
    p(3, '规则 §9.2「超出标准的须经区域审批」保持不变。'),
    p(4, '本通知与 02 规则不一致的，以本通知为准。'),
  ],
};

// R-06 的块：不带 around 时高亮所有被引用的句子（出处，还没被引用就不高亮）；带 around 时只高亮那一段（页面滚到那里）
export function previewBlocks(path, around, f) {
  if (f?.scanned) return [];
  if (f && f.preview !== 'doc') return [];
  const name = String(path).split('/').pop();
  const blocks = DOCS[name] || [p(1, '（合成示例）这份资料的正文。'), p(2, '（合成示例）第二段。')];
  const cited = !f || f.referenced > 0;
  return blocks.map(({ mark, ...b }) => {
    if (!mark || (around ? b.anchor !== around : !cited)) return b;
    const start = b.text.indexOf(mark);
    return start >= 0 ? { ...b, highlights: [{ start, end: start + mark.length }] } : b;
  });
}

// ---------- 文本版（R-13）：PDF 每 3 段一页、带页码标记；Word 按标题和段落；Excel 每个工作表一张表 ----------
export function textMarkdown(f) {
  if (f.kind === 'xls') {
    return tableOf(f).map((sh) => {
      const rows = sh.rows || [];
      const line = (r) => `| ${r.map((c) => String(c ?? '').replace(/\|/g, '\\|')).join(' | ')} |`;
      const table = rows.length ? [line(rows[0]), `|${'---|'.repeat(rows[0].length)}`, ...rows.slice(1).map(line)].join('\n') : '（空表）';
      return `<!-- 工作表：${sh.name} -->\n## ${sh.name}\n\n${table}`;
    }).join('\n\n');
  }
  const blocks = previewBlocks(f.path, null, { ...f, scanned: false, preview: 'doc' });
  const md = (b) => (b.kind === 'heading' ? `${'#'.repeat(Math.min(b.level || 2, 4))} ${b.text}` : b.text);
  if (f.kind === 'pdf') {
    const pages = [];
    for (let i = 0; i < blocks.length; i += 3) pages.push(blocks.slice(i, i + 3));
    return pages.map((pg, i) => `<!-- 第 ${i + 1} 页 -->\n${pg.map(md).join('\n\n')}`).join('\n\n');
  }
  return blocks.map(md).join('\n\n');
}

// ---------- 选出处的位置（R-15）：和后端一样逐行列——本身是文本的读原文件，要转的读文本版（第一行是文件头）；
// 位置标记、小节标题、注释和表格分隔行不列，用来给下面的行定位置（文本版按页、工作表，没有时按小节标题） ----------
const POSITION_CHARS = 160;
function textSourceOf(f) {
  if (!f?.text) return null;
  if (f.text.state === 'native') {
    const text = f.kind === 'csv' ? (tableOf(f)[0]?.rows || []).map((r) => r.join(',')).join('\n') : textMarkdown(f);
    return { source: f.path, text, markers: false, headings: /\.(md|markdown)$/i.test(f.path) };
  }
  if (!f.text.has_text) return null;
  const header = `<!-- onto:text source="${f.name}" sha256="0000000000000000" converter="mock@1" -->`;
  return { source: `resource/text/${f.path.replace(/^resource\//, '')}.md`, text: `${header}\n${textMarkdown(f)}`, markers: true, headings: true };
}
function positionText(line) {
  const text = line.startsWith('|')
    ? line.replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|')).filter(Boolean).join(' · ')
    : line.replace(/^[-*+]\s+/, '');
  return text.length > POSITION_CHARS ? `${text.slice(0, POSITION_CHARS)}…` : text;
}
export function positionsOf(f) {
  const src = textSourceOf(f);
  if (!src) return null;
  let marker = '';
  let heading = '';
  const items = [];
  src.text.split('\n').forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const page = src.markers && /^<!-- 第 (\d+) 页 -->$/.exec(line);
    const sheetMark = src.markers && /^<!-- 工作表：(.+) -->$/.exec(line);
    if (page || sheetMark) { marker = page ? `第 ${page[1]} 页` : `工作表 ${sheetMark[1]}`; return; }
    const h = src.headings && /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) { heading = h[2].replace(/#/g, '').trim(); return; }
    if (/^<!--.*-->$/.test(line) || /^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?$/.test(line)) return;
    const text = positionText(line);
    if (text) items.push({ anchor: `file:${src.source}#L${i + 1}`, location: marker || heading, text });
  });
  return items;
}

// R-06 读文本版（规则出处、证据「查看原文」跳来定位）：逐行成块，文件头不出，页、工作表标记记在后面的块上；
// around（L12、L12-L15）按第一行把窗口放在中间，和后端 material_text.blocks 一致
export function textVersionOf(files, path) {
  return (files || []).find((f) => textSourceOf(f)?.source === path && f.text?.state !== 'native') || null;
}
export function textBlocksOf(f, around, limit = 200) {
  const src = textSourceOf(f);
  const rows = [];
  src.text.split('\n').forEach((raw, i) => { if (raw.trim()) rows.push({ n: i + 1, line: raw.trim() }); });
  const first = /^L(\d+)/.exec(around || '');
  const at = first ? rows.findIndex((r) => r.n === Number(first[1])) : -1;
  const start = at < 0 ? 0 : Math.max(0, at - Math.floor(limit / 2));
  let location = '';
  rows.slice(0, start).forEach(({ line }) => {
    const mk = /^<!-- (?:第 (\d+) 页|工作表：(.+)) -->$/.exec(line);
    if (mk) location = mk[1] ? `第 ${mk[1]} 页` : `工作表 ${mk[2]}`;
  });
  const out = [];
  rows.slice(start, start + limit).forEach(({ n, line }) => {
    if (line.startsWith('<!-- onto:text ')) return;
    const mk = /^<!-- (?:第 (\d+) 页|工作表：(.+)) -->$/.exec(line);
    if (mk) { location = mk[1] ? `第 ${mk[1]} 页` : `工作表 ${mk[2]}`; return; }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    const block = h ? { anchor: `L${n}`, kind: 'heading', level: h[1].length, text: h[2].trim() }
      : { anchor: `L${n}`, kind: line.startsWith('|') ? 'table' : /^[-*+]\s/.test(line) ? 'list' : 'paragraph', text: line };
    out.push(location ? { ...block, location } : block);
  });
  return out;
}

// ---------- 表格（F-05） ----------
const sheet = (name, rows, total_rows) => ({ name, rows, merges: [], total_rows, truncated: total_rows > 500 });
const TABLES = {
  '05-门店清单.xlsx': [
    sheet('门店', [
      ['门店编码', '门店名称', '门店等级', '区域', '日均销量（箱）', '到货周期'],
      ['S001', 'A 门店', '一级', '华东一区', '85', '每日'],
      ['S002', 'B 门店', '二级', '华东一区', '60', '隔日'],
      ['S003', 'C 门店', '一级', '华东二区', '92', '每日'],
      ['S004', 'D 门店', '三级', '华东二区', '35', '每周'],
      ['S005', 'E 门店', '二级', '华南一区', '58', '隔日'],
      ['S006', 'F 门店', '一级', '华南一区', '110', '每日'],
      ['S007', 'G 门店', '三级', '华南二区', '28', '每周'],
      ['S008', 'H 门店', '二级', '华南二区', '64', '隔日'],
    ], 306),
    sheet('区域', [
      ['区域', '配送中心', '乳制品配额（箱）', '审批人'],
      ['华东一区', '一号配送中心', '150', '区域督导'],
      ['华东二区', '一号配送中心', '150', '区域督导'],
      ['华南一区', '二号配送中心', '120', '区域督导'],
      ['华南二区', '二号配送中心', '120', '区域督导'],
    ], 12),
  ],
  '06-商品品类表.xlsx': [
    sheet('品类', [
      ['商品', '品类', '适用标准', '到货周期', '标准上限（箱）', '备注'],
      ['牛奶', '乳制品', '乳制品陈列标准 2024', '每日', '120', ''],
      ['酸奶', '乳制品', '乳制品陈列标准 2024', '隔日', '96', '区域配额'],
      ['面包', '烘焙', '烘焙陈列标准', '每日', '80', ''],
      ['鸡蛋', '禽蛋', '生鲜陈列标准', '隔日', '60', ''],
      ['矿泉水', '饮料', '饮料陈列标准', '每周', '200', ''],
      ['方便面', '粮油', '干货陈列标准', '每周', '150', '门店指定'],
      ['苹果', '水果', '生鲜陈列标准', '每日', '90', ''],
      ['纸巾', '日化', '日化陈列标准', '每周', '—', '不限陈列量'],
    ], 1180),
    sheet('到货周期', [
      ['到货周期', '补货间隔（天）', '适用品类'],
      ['每日', '1', '乳制品、烘焙、水果'],
      ['隔日', '2', '禽蛋、乳制品'],
      ['每周', '7', '饮料、粮油、日化'],
    ], 12),
    sheet('适用标准', [
      ['标准', '版本', '启用日期'],
      ['乳制品陈列标准', '2024 版', '2024-03-01'],
      ['乳制品陈列标准', '2019 版', '2019-06-01'],
      ['烘焙陈列标准', '2022 版', '2022-09-01'],
      ['生鲜陈列标准', '2023 版', '2023-05-15'],
    ], 12),
  ],
  '08-术语对照.xlsx': [
    sheet('术语', [
      ['术语', '别名', '说明'],
      ['要货方', '门店', '向配送中心申请补货的门店'],
      ['标准上限', '最大箱数', '补货标准表里单次补货的上限'],
      ['区域配额', '配额', '区域统一的补货上限'],
      ['到货周期', '送货频次', '每日、隔日、每周'],
      ['安全库存', '保底库存', '低于它要补货'],
      ['临期', '近效期', '保质期剩余 ≤3 天'],
      ['超量项', '超标项', '申请量超过标准上限的品类'],
      ['区域审批', '督导审批', '超量项下单前的审批'],
    ], 86),
  ],
  '10-补货台账导出.csv': [
    sheet('10-补货台账导出', [
      ['order_no', 'store', 'goods', 'delivery_cycle', 'status', 'ordered_at'],
      ['RO-2610-0412', 'A门店', '牛奶', '每日', '已下单', '2026-10-12'],
      ['RO-2610-0415', 'B门店', '酸奶', '隔日', '审批中', '—'],
      ['RO-2610-0418', 'C门店', '面包', '每日', '已下单', '2026-10-16'],
      ['RO-2610-0421', 'D门店', '鸡蛋', '隔日', '草稿', '—'],
      ['RO-2610-0423', 'A门店', '方便面', '每周', '已下单', '2026-10-20'],
      ['RO-2610-0426', 'E门店', '牛奶', '隔日', '临时加量', '—'],
    ], 412),
  ],
};

export function tableOf(f) {
  const t = TABLES[f.name];
  if (t) return t;
  const name = f.kind === 'csv' ? f.name.replace(/\.csv$/i, '') : 'Sheet1';
  return [sheet(name, [['列 A', '列 B', '列 C'], ['（合成示例）', '', '']], 2)];
}

// ---------- drawio（F-02 / F-04） ----------
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const vertex = (id, value, style, x, y, w, hh) => `<mxCell id="${id}" value="${esc(value)}" style="${style}whiteSpace=wrap;html=1;" vertex="1" parent="1"><mxGeometry x="${x}" y="${y}" width="${w}" height="${hh}" as="geometry"/></mxCell>`;
const edge = (id, s, t, value = '', points = []) => `<mxCell id="${id}" value="${esc(value)}" style="edgeStyle=orthogonalEdgeStyle;endArrow=block;strokeColor=#93A1B5;" edge="1" parent="1" source="${s}" target="${t}"><mxGeometry relative="1" as="geometry">${points.length ? `<Array as="points">${points.map(([x, y]) => `<mxPoint x="${x}" y="${y}"/>`).join('')}</Array>` : ''}</mxGeometry></mxCell>`;
const mxfile = (name, cells) => `<mxfile host="onto-modeling"><diagram id="d-1" name="${esc(name)}"><mxGraphModel dx="800" dy="600" grid="1" gridSize="10"><root><mxCell id="0"/><mxCell id="1" parent="0"/>${cells.join('')}</root></mxGraphModel></diagram></mxfile>`;
const PILL = 'rounded=1;arcSize=50;fillColor=#EFF5FF;strokeColor=#9BBEFB;';
const STEP = 'rounded=1;fillColor=#FFFFFF;strokeColor=#C3D8FD;';
const GATE = 'rhombus;fillColor=#FFF8EB;strokeColor=#F5C26B;';

export const DRAWIO = {
  [FLOW_PATH]: mxfile('门店补货流程', [
    vertex('n1', '收到申请', PILL, 140, 8, 80, 26),
    vertex('n2', '受理三道校验', STEP, 115, 58, 130, 30),
    vertex('n3', '在受理范围?', GATE, 136, 108, 88, 44),
    vertex('n4', '退回店长', STEP, 246, 174, 84, 28),
    vertex('n5', '逐类核定标准', STEP, 115, 174, 130, 30),
    vertex('n6', '生成补货单', PILL, 140, 228, 80, 26),
    edge('e1', 'n1', 'n2'),
    edge('e2', 'n2', 'n3'),
    edge('e3', 'n3', 'n5', '是'),
    edge('e4', 'n3', 'n4', '否', [[288, 130]]),
    edge('e5', 'n5', 'n6'),
  ]),
  [LEGEND_PATH]: mxfile('图示说明定义', [
    vertex('l1', '开始 / 结束', PILL, 20, 20, 110, 30),
    vertex('l2', '步骤', STEP, 160, 20, 110, 30),
    vertex('l3', '判断', GATE, 20, 80, 110, 50),
    vertex('l4', '数据表', 'shape=cylinder;fillColor=#F0FDF9;strokeColor=#5EEAD4;', 160, 80, 110, 50),
    vertex('l5', '说明（不参与流程）', 'rounded=1;dashed=1;fillColor=#FFFFFF;strokeColor=#93A1B5;', 20, 160, 250, 34),
  ]),
};
export const EMPTY_DRAWIO = mxfile('第 1 页', []);

// ---------- HTML 报告（F-04） ----------
const bars = [5, 8, 6, 9, 7, 3, 0];
const barSvg = () => {
  const w = 300; const hh = 60; const mx = Math.max(...bars); const gap = w / bars.length; const bw = gap * 0.62;
  return `<svg width="${w}" height="${hh}" style="display:block">${bars.map((v, i) => {
    const bh = Math.max(2, (v / mx) * (hh - 14));
    return `<rect x="${(i * gap + (gap - bw) / 2).toFixed(1)}" y="${(hh - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="${i === 3 ? '#2563EB' : '#BFD4FE'}"/>`;
  }).join('')}</svg>`;
};
export const HTML_REPORT = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>补货周报</title><style>
body{margin:0;font:13px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#0B1A33;background:transparent}
.rp{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(15,35,80,.08)}
h6{font-size:14px;margin:0 0 8px}.m{font-size:11.5px;color:#64748B}
.kp{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0 12px}
.kp div{border:1px solid #E5EBF3;border-radius:8px;padding:8px 10px}.kp b{display:block;font-size:17px}
</style></head><body><div class="rp"><h6>补货周报 · 第 43 周</h6><div class="m">区域补货中心 · 10-20 至 10-26</div>
<div class="kp"><div><span class="m">下单补货单</span><b>38</b></div><div><span class="m">超量项</span><b>12</b></div><div><span class="m">临时加量</span><b style="color:#B45309">3</b></div></div>
${barSvg()}<div class="m" style="margin-top:8px">超量主要集中在陈列量（7 项）和临期（3 项）。</div></div></body></html>`;

// ---------- 被谁引用（R-05） ----------
const ref = (key, location, anchors) => ({ target: entryRef(key), location, anchors });
const art = (type, key, title, location, anchors) => ({ target: { type, key, title }, location, anchors });
const REFERENCES = {
  '02-门店补货规则.docx': [
    ref('overage_and_hint', '概述、步骤 3、步骤 4', ['p:9.2', 'p:11.4']),
    ref('r09_store_stricter', '依据', ['p:9.2']),
    ref('r11_temp_increase', '依据', ['p:11.4']),
    ref('safety_stock_calc', '步骤 2', ['p:9.3']),
    ref('display_qty_check', '步骤 1、步骤 3', ['p:8.2', 'p:8.4']),
    ref('display_qty_std', '属性「标准上限」', ['p:5.2']),
    ref('store_request', '定义', ['p:4.1']),
    ref('r02_required', '依据', ['p:5.1']),
    ref('replenish_ledger', '定义', ['p:12.1']),
    ref('r08_expiry_grade', '依据', ['p:10.2']),
    art('diagram.flowchart', 'overage-flow', '超量处理流程', '节点「区域审批」', ['p:9.2']),
  ],
  '01-场景说明.docx': [
    ref('replenish_order', '定义', ['p:2']),
    ref('replenish_main', '概述', ['p:1', 'p:2']),
  ],
  '03-样例补货单-A门店.docx': [
    ref('r09_store_stricter', '样例', ['p:5']),
  ],
  '03-样例补货单-B.docx': [
    ref('overage_and_hint', '决策依据', ['p:4']),
    art('trace.scenario', 's-05', '区域统一配额取值', '', ['p:4']),
  ],
  '04-商品陈列说明.pdf': [
    ref('check_item', '属性「冷链温度记录」', ['p:2']),
  ],
};
export function referencesOf(f) {
  if (!f || !f.referenced) return [];
  return REFERENCES[f.name] || [];
}
export const anchorCount = (items) => items.reduce((n, x) => n + Math.max(1, (x.anchors || []).length), 0);

// ---------- 外部反馈（R-11，反馈演进包） ----------
export function feedbackSeed() {
  return [
    {
      id: 'fb-1101-01', source: { service: '评测服务', run: 'eval-1101-0910' }, release_version: 'v0.11', received_at: atDaysAgo(0, 9, 10),
      state: 'open', summary: '评测失分用例 4 条 · 多数和「超量与审批提示」有关', handled_version_label: null,
      items: [
        { case_id: 'c-01', question: '牛奶日均销量 60 箱、隔日到货，门店申请 ≤96 箱', expected: '按门店执行', actual: '生成了审批提示', loss_reason: '超量方向判断反了（步骤 3）', suspected: [entryRef('overage_and_hint')], related: null },
        { case_id: 'c-02', question: '酸奶临期抽检', expected: '保质期剩余 ≤3 天下架', actual: '未给出', loss_reason: '临期损耗标准缺这个商品', suspected: [entryRef('expiry_loss_std')], related: null },
        { case_id: 'c-03', question: '门店指定 2024 版陈列标准', expected: '以门店为准', actual: '用了 2019 版', loss_reason: '缺少 6.3 条', suspected: [entryRef('applicable_std')], related: null },
        { case_id: 'c-04', question: '临时加量 5 箱', expected: '需区域审批', actual: '直接下单', loss_reason: 'R-11 没有挂到操作', suspected: [entryRef('r11_temp_increase'), entryRef('overage_and_hint')], related: null },
      ],
    },
    {
      id: 'fb-1029-01', source: { service: '线上反馈', run: 'web' }, release_version: 'v0.10', received_at: atDaysAgo(3, 16, 25),
      state: 'handled', summary: '线上反馈 1 条 · 「牛奶隔日到货的陈列上限取错了」', handled_version_label: 'v0.11',
      items: [
        { case_id: 'u-01', question: '牛奶隔日到货的陈列上限', expected: '96 箱', actual: '120 箱', loss_reason: '到货周期没参与取值', suspected: [entryRef('display_qty_std')], related: null },
      ],
    },
  ];
}

// ---------- 提交 ----------
const nextVersion = (label) => {
  const m = /^v(\d+)\.(\d+)$/.exec(label || '');
  return m ? `v${m[1]}.${Number(m[2]) + 1}` : 'v0.1';
};
// 资料区的改动经写入路径提交（Onto-Source: web）：项目版本号 +1，推 workspace.changed（EP-05）
export function webCommit(ctx, pid, paths) {
  const c = ctx.cardOf?.(pid);
  const version_label = nextVersion(c?.version_label);
  const commit = `w${String(ctx.uid()).padStart(7, '0')}`;
  if (c) Object.assign(c, { version_label, head: commit });
  ctx.emitProj(pid, 'workspace.changed', { commit, version_label, source: 'web', changeset_id: null, by: 'zhangsan', entries: [], artifacts: [], paths });
  if (c && ctx.F?.projectMeta) ctx.emitProj(pid, 'project.updated', { fields: ['version_label'], meta: ctx.F.projectMeta(c) });
  return { commit, version_label };
}
