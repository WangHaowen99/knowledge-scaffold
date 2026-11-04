// 3.6 资料分析（R-01～R-10）与 3.12 建模蓝图（BP-01～BP-03）
// 阶段（主示例项目用 ?mock=stage:<名> 切换）：
//   materials        读资料中：7 份资料读到 5/7（设计稿 wb-materials）
//   materials-empty  还没传资料：欢迎与上传卡
//   blueprint        资料分析完成、建模蓝图草案 v2（F8 起不分段、不用确认，资料分析之后直接提取对象）
//   estate-empty     「物业报修工单处理」的落地页：和 materials-empty 同一屏，换成它自己的名称和目标
import { ANCHORS, ANCHOR_RESOLUTIONS, MAIN_PID, projectCards } from './fixtures.js';
import { catalogFor, materialView, previewBlocks, previewCoverage, webCommit } from './resources-data.js';
import { DB_SOURCE, ENTRIES, MATERIALS } from './scenario.js';

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const CID = 'a3f0c9e15b72';
const ESTATE_PID = 'estate-repair-c4d2b915';
const RESEARCH_REPORT = 'rp-research'; // 与 routes-reports.js 的资料分析报告同一份
const MAP_CARD = 'card-material-map';
const BP_CARD = 'card-blueprint';
const PURPOSE_VALUES = ['goal', 'schema', 'term', 'evidence', 'auto'];

const atDaysAgo = (days, h, m) => {
  const d = new Date(Date.now() - days * 24 * HOUR);
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};
const two = (n) => String(n).padStart(2, '0');
const stamp = (iso) => {
  const d = new Date(iso);
  return `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
};

function kindOf(name) {
  const ext = (String(name).split('.').pop() || '').toLowerCase();
  if (/^xls[xm]?$/.test(ext)) return 'xls';
  if (ext === 'csv') return 'csv';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'drawio') return 'drawio';
  if (/^(md|markdown|txt)$/.test(ext)) return 'text';
  if (/^(png|jpe?g)$/.test(ext)) return 'image';
  return 'doc';
}
const attKind = (k) => (k === 'xls' || k === 'csv' ? 'xls' : k === 'pdf' ? 'pdf' : k === 'image' ? 'image' : 'doc');

// ---------- 出处 ----------
const R02 = 'file:resource/others/02-门店补货规则.docx';
const ANC = {
  goal: 'file:resource/others/01-场景说明.docx#p:1',
  s2: 'file:resource/others/01-场景说明.docx#p:2',
  b4: ANCHORS.eb,
  r92: ANCHORS.e92,
  r114: ANCHORS.e114,
  r84: `${R02}#p:8.4`,
  r41: `${R02}#p:4.1`,
  r31: `${R02}#p:3.1`,
  r51: `${R02}#p:5.1`,
  r52: `${R02}#p:5.2`,
  r6: `${R02}#p:6`,
  r61: `${R02}#p:6.1`,
  r72: `${R02}#p:7.2`,
  r73: `${R02}#p:7.3`,
  r102: `${R02}#p:10.2`,
  r121: `${R02}#p:12.1`,
  cat: 'file:resource/schema/06-商品品类表.xlsx#sheet:品类/cell:D1',
  catB2: 'file:resource/schema/06-商品品类表.xlsx#sheet:品类/cell:B2',
  catS2: 'file:resource/schema/06-商品品类表.xlsx#sheet:到货周期/cell:A1',
  store: 'file:resource/schema/05-门店清单.xlsx#sheet:门店/cell:A1',
};

// 悬停看原文（EV-29）：补上本页用到的锚点
function res(anchor, source_label, location_label, marked) {
  let text = '';
  const highlights = [];
  marked.split(/(<mark>.*?<\/mark>)/).forEach((part) => {
    const mm = part.match(/^<mark>(.*)<\/mark>$/);
    if (mm) { highlights.push({ start: text.length, end: text.length + mm[1].length }); text += mm[1]; } else text += part;
  });
  return { anchor, scheme: 'file', resolved: true, source_label, location_label, text, highlights, confidence: null, snapshot_id: null, page: 'resources' };
}
const MORE_RESOLUTIONS = [
  res(ANC.goal, '01-场景说明.docx', '§1 背景', '<mark>店长只需说明门店、品类、库存与到货周期</mark>，系统自动生成补货单初稿。'),
  res(ANC.s2, '01-场景说明.docx', '§2 业务目标', '由店长根据门店需求起草<mark>补货单</mark>，经区域审批后下单。'),
  res(ANC.r84, '02-门店补货规则.docx', '§8.4', '8.4 到货周期为每周时，<mark>陈列量按周峰值核定</mark>……（前一条为 8.2）'),
  res(ANC.r41, '02-门店补货规则.docx', '§4.1', '<mark>门店特殊需求</mark>应逐条登记，并注明来源文件与日期。'),
  res(ANC.r31, '02-门店补货规则.docx', '§3.1 术语', '<mark>要货方</mark>：向配送中心申请补货的门店。'),
  res(ANC.r51, '02-门店补货规则.docx', '§5.1', '补货申请缺少门店编码、品类或申请量的，<mark>退回店长补全</mark>。'),
  res(ANC.r52, '02-门店补货规则.docx', '§5.2', '各品类的<mark>标准上限</mark>按补货标准表取值。'),
  res(ANC.r6, '02-门店补货规则.docx', '§6 安全库存', '各品类<mark>安全库存</mark>按适用标准逐类取值。'),
  res(ANC.r61, '02-门店补货规则.docx', '§6.1', '<mark>适用标准</mark>以最新版陈列标准为准。'),
  res(ANC.r72, '02-门店补货规则.docx', '§7.2', '<mark>同一到货批次、同一品类算一批</mark>。'),
  res(ANC.r102, '02-门店补货规则.docx', '§10.2', '临期商品<mark>按四级临期分级</mark>处理。'),
  res(ANC.r121, '02-门店补货规则.docx', '§12.1', '下单后的补货单登记入<mark>补货台账</mark>，保存期 1 年。'),
  res(ANC.cat, '06-商品品类表.xlsx', 'Sheet 品类!D1', '列「<mark>到货周期</mark>」取值：每日、隔日、每周。'),
  res(ANC.catB2, '06-商品品类表.xlsx', 'Sheet 品类!B2', '<mark>品类</mark>、适用标准、到货周期、日均销量范围。'),
  res(ANC.store, '05-门店清单.xlsx', 'Sheet 门店!A1', '<mark>门店编码</mark>、门店名称、门店等级、日均销量。'),
];

// ---------- 资料地图（R-07） ----------
const mapItem = (m, o = {}) => ({
  path: m.path, name: m.name, kind: kindOf(m.name), purpose: m.purpose, purpose_auto: true, read_pct: 0, status: 'queued',
  position: null, queue_reason: null, contradiction: null, blocked: null, counts: { objects: 0, rules: 0, terms: 0 }, ...o,
});

function readingItems() {
  const [m1, m2, m3a, m3b, m4, m5, m6] = MATERIALS;
  return [
    mapItem(m1, { read_pct: 100, status: 'done', counts: { objects: 3, rules: 0, terms: 4 } }),
    mapItem(m2, { read_pct: 78, status: 'reading', position: '读到 §10 · 还剩 §10–§12', counts: { objects: 6, rules: 14, terms: 21 } }),
    mapItem(m3a, { read_pct: 100, status: 'done', counts: { objects: 2, rules: 3, terms: 2 } }),
    mapItem(m3b, { read_pct: 100, status: 'done', contradiction: '与规则 §9.2 有 1 处矛盾', counts: { objects: 1, rules: 2, terms: 0 } }),
    mapItem(m4, { purpose: 'auto', status: 'queued', queue_reason: '读完 02 后开始' }),
    mapItem(m5, { read_pct: 100, status: 'done', counts: { objects: 1, fields: 12, terms: 0 } }),
    mapItem(m6, {
      read_pct: 50, status: 'partial', counts: { objects: 2, fields: 9, terms: 6 },
      blocked: { reason: '只读了 Sheet「品类」，Sheet「到货周期」还没读', unread_parts: ['Sheet「到货周期」'] },
    }),
  ];
}

function doneItems() {
  const [m1, m2, m3a, m3b, m4, m5, m6] = MATERIALS;
  const done = (m, counts, o = {}) => mapItem(m, { read_pct: 100, status: 'done', counts, ...o });
  return [
    done(m1, { objects: 3, rules: 0, terms: 4 }),
    done(m2, { objects: 7, rules: 18, terms: 26 }),
    done(m3a, { objects: 2, rules: 3, terms: 2 }),
    done(m3b, { objects: 1, rules: 2, terms: 0 }, { contradiction: '与规则 §9.2 有 1 处矛盾' }),
    done(m4, { objects: 2, rules: 4, terms: 5 }),
    done(m5, { objects: 1, fields: 12, terms: 0 }, { purpose_auto: false }),
    done(m6, { objects: 3, fields: 12, terms: 8 }),
  ];
}

function problems() {
  return [
    {
      id: 'pb-contra-b4', kind: 'contradiction', state: 'open',
      text_md: '样例补货单 B 第 4 节按**区域统一配额**执行[^1]，规则 §9.2 要求按适用标准比较[^2]',
      anchors: [{ n: 1, anchor: ANC.b4, label: '样例 B §4' }, { n: 2, anchor: ANC.r92, label: '规则 §9.2' }],
    },
    {
      id: 'pb-miss-84', kind: 'missing', state: 'open',
      text_md: '02 规则 §8.2 之后编号直接跳到 §8.4[^1]，可能缺 §8.3 条款',
      anchors: [{ n: 1, anchor: ANC.r84, label: '规则 §8.4' }],
    },
  ];
}

const avgPct = (items) => (items.length ? Math.round(items.reduce((n, x) => n + x.read_pct, 0) / items.length) : 0);

function makeMap(kind) {
  if (kind === 'empty') {
    return { total: 0, read_pct: 0, analysis_mode: 'local', web_available: false, items: [], problems: [], verification: { total: 0, passed: 0, unverifiable: 0, pending: 0, checked_at: null }, can_generate_blueprint: false };
  }
  const reading = kind === 'reading';
  const items = reading ? readingItems() : doneItems();
  return {
    total: items.length,
    read_pct: reading ? 72 : 100,
    analysis_mode: 'local',
    web_available: false,
    items,
    problems: problems().map((p) => (reading ? p : { ...p, state: 'promoted' })),
    verification: reading
      ? { total: 46, passed: 41, unverifiable: 2, pending: 3, checked_at: new Date(Date.now() - 2 * MIN).toISOString() }
      : { total: 46, passed: 46, unverifiable: 0, pending: 0, checked_at: atDaysAgo(0, 9, 30) },
    can_generate_blueprint: !reading,
  };
}

function materialMapCard(map) {
  return {
    card_id: MAP_CARD, card_type: 'material_map', eyebrow: '', title: '资料地图', subtitle: '每份读到哪、用来做什么、有没有问题',
    status: null, plugin: null, open: { kind: 'dialog', target: 'material_map' },
    total: map.total, read_pct: map.read_pct, items: map.items.slice(0, 8),
    unverifiable: map.verification.unverifiable,
    problems: {
      contradiction: map.problems.filter((p) => p.kind === 'contradiction' && p.state === 'open').length,
      missing: map.problems.filter((p) => p.kind === 'missing' && p.state === 'open').length,
    },
  };
}

// ---------- 候选（R-08） ----------
const cand = (id, kind, name, anchor, anchor_label) => ({ id, kind, name, anchor, anchor_label });
function candidatePool() {
  const older = [
    cand('cd-01', 'object', '门店', ANC.s2, '01 §2'),
    cand('cd-02', 'term', '品类标准上限', ANC.r52, '02 §5.2'),
    cand('cd-03', 'rule', '必填项缺失时退回店长补全', ANC.r51, '02 §5.1'),
    cand('cd-04', 'object', '适用标准', ANC.r61, '02 §6.1'),
    cand('cd-05', 'term', '日均销量', ANC.store, '05 门店'),
    cand('cd-06', 'object', '门店等级', ANC.store, '05 门店'),
    cand('cd-07', 'rule', '同一到货批次、同一品类算一批', ANC.r72, '02 §7.2'),
    cand('cd-08', 'term', '安全库存', ANC.r6, '02 §6'),
  ];
  const stream = [
    cand('cd-09', 'object', '补货单', ANC.s2, '01 §2'),
    cand('cd-10', 'object', '门店特殊需求', ANC.r41, '02 §4.1'),
    cand('cd-11', 'rule', '门店申请严于标准时按门店执行', ANC.r92, '02 §9.2'),
    cand('cd-12', 'term', '到货周期：每日 / 隔日 / 每周', ANC.cat, '06 品类'),
    cand('cd-13', 'object', '商品品类', ANC.catB2, '06 品类'),
    cand('cd-14', 'rule', '临时加量须经区域审批', ANC.r114, '02 §11.4'),
    cand('cd-15', 'term', '要货方 ≈ 门店（待确认）', ANC.r31, '02 §3.1'),
    cand('cd-16', 'object', '补货台账', ANC.r121, '02 §12.1'),
    cand('cd-17', 'rule', '临期商品按四级分级', ANC.r102, '02 §10.2'),
    cand('cd-18', 'object', '安全库存标准', ANC.r6, '02 §6'),
  ];
  return { now: [...older, ...stream.slice(0, 3)], queue: stream.slice(3) };
}

// ---------- 核验（R-09） ----------
function verificationItems(reading) {
  const v = (subject, kind, anchor, anchor_label, status, reason = null) => ({ subject, kind, anchor, anchor_label, status, reason });
  const head = [
    v('R-07 陈列抽检按批次', 'rule', ANC.r73, '02 §7.3', 'unverifiable', 'R-07 引用的 §7.3 在原文里找不到这句话'),
    v('术语：到货周期', 'term', ANC.catS2, '06 Sheet2!A1', 'unverifiable', '「到货周期」所在的 Sheet 还没读'),
    v('R-11 临时加量须审批', 'rule', ANC.r114, '02 §11.4', 'pending'),
    v('对象：补货台账', 'object', ANC.r121, '02 §12.1', 'pending'),
    v('术语：临期损耗', 'term', ANC.r102, '02 §10.2', 'pending'),
    v('R-09 门店申请取严', 'rule', ANC.r92, '02 §9.2', 'passed'),
    v('对象：商品品类', 'object', ANC.catB2, '06 Sheet1!B2', 'passed'),
  ];
  const used = new Set(['补货台账', '商品品类', 'R-09 门店申请取严', 'R-11 临时加量须审批']);
  const kindName = { object: '对象：', operation: '操作：', script: '脚本：' };
  const rest = ENTRIES.filter((x) => !used.has(x.name)).map((x) => v(`${kindName[x.kind] || ''}${x.name}`, x.kind === 'metric' || x.kind === 'script' || x.kind === 'operation' ? 'item' : x.kind, ANC.r6, '02 §6', 'passed'));
  const terms = ['补货单号', '申请量', '标准上限', '区域配额', '到货批次', '陈列面', '保质期', '下架', '门店编码', '配送中心', '冷链', '促销备货'];
  terms.forEach((t) => rest.push(v(`术语：${t}`, 'term', ANC.r31, '02 §3.1', 'passed')));
  const items = [...head, ...rest].slice(0, 46);
  if (reading) return items;
  return items.map((x) => ({ ...x, status: 'passed', reason: null }));
}

// ---------- 建模蓝图（BP-01） ----------
function blueprintOf(confirmed = false) {
  return {
    version_no: 2,
    version_label: confirmed ? 'v2' : '草案 v2',
    state: confirmed ? 'confirmed' : 'draft',
    goal_md: '店长一句话说明门店、品类、库存和到货周期，智能体查基础数据，按补货规则生成补货单，并列出与标准不一致的地方',
    goal_anchor: null,
    goal_anchor_label: null,
    // F8 起不分段：草稿里的分段并成一段「全部」
    slices: [{ id: 'all', name: '全部', core_flow: '', scale: { objects: 15, rules: 11 }, depends_on: [], order: 1, order_label: '', excluded: false }],
    terms: {
      aligned: ['商品品类', '到货周期', '适用标准', '超量项', '区域审批', '补货台账', '安全库存', '陈列量', '临期损耗', '日均销量', '门店特殊需求', '审批提示', '下单'],
      ambiguous: [
        { id: 't-store', variants: ['要货方', '门店'], chosen: '门店' },
        { id: 't-expiry', variants: ['临期', '近效期'], chosen: null },
      ],
    },
    questions: [
      { id: 'bq-ledger', title: '补货台账要不要建成一个对象？', background: '02 §12.1 要求登记保存 1 年', options: [{ id: 'A', label: '要，作为对象' }, { id: 'B', label: '只作为输出报表' }], answer: null },
      { id: 'bq-83', title: '02 规则 §8.3 是不是缺了？', background: '编号从 8.2 跳到 8.4', options: [{ id: 'A', label: '原文就没有' }, { id: 'B', label: '我补传完整版' }], answer: null },
      { id: 'bq-b4', title: '样例补货单 B 用区域统一配额，算规则吗？', background: '与 §9.2 不一致', options: [{ id: 'A', label: '算，作为门店特例' }, { id: 'B', label: '不算，以 §9.2 为准' }], answer: null },
    ],
    research: { verified: true, report_id: RESEARCH_REPORT },
    evidence_hint: {
      sources: [{ id: DB_SOURCE.id, label: 'MySQL', environment: 'production' }],
      text: '资料分析之后会出现「系统取证」节点：采集一次只读快照（生产环境要你确认），表结构和外键会作为对象和关系的候选，每条都带证据锚点。',
    },
    revision: 'bp-r2',
    updated_at: atDaysAgo(0, 9, 33),
  };
}

function blueprintCard(bp) {
  return {
    card_id: BP_CARD, card_type: 'blueprint', eyebrow: '', title: `建模蓝图 · ${bp.version_label}`,
    subtitle: '',
    status: null, plugin: null,
    // F8 起蓝图不用确认：后端恒给空
    confirm_options: [],
    open: { kind: 'dialog', target: 'blueprint' },
    version_label: bp.version_label, state: bp.state, goal_md: bp.goal_md, goal_anchor: null, goal_anchor_label: null,
    goal_question: null, slices: bp.slices,
    terms: { aligned: bp.terms.aligned.length, ambiguous: bp.terms.ambiguous.length },
    questions_pending: bp.questions.filter((q) => !q.answer).length,
    research_verified: !!bp.research?.verified,
  };
}

function researchSummaryCard() {
  return {
    card_id: 'card-research-summary', card_type: 'research_summary', eyebrow: '', title: '资料分析完成', subtitle: '',
    status: { key: 'confirmed', label: '已核验' }, plugin: null, open: { kind: 'page', target: 'reports', ref: { report_id: RESEARCH_REPORT } },
    files: 7, coverage_pct: 100, verified_anchors: 46, problems_carried: 2, verified: true, report_id: RESEARCH_REPORT,
  };
}

function uploadCard() {
  return {
    card_id: 'card-upload', card_type: 'upload_prompt', eyebrow: '', title: '先把资料给我', subtitle: '规则文档、样例单据、表结构、术语表都可以；现在没有，也可以先聊目标',
    status: null, plugin: null, open: null,
    accept: ['.md', '.markdown', '.txt', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.csv', '.drawio'],
    max_bytes: 20 * 1024 * 1024, sources_available: 2, actions: ['upload', 'connect_system', 'import_package'],
  };
}

function purposeQuestion(cid) {
  return {
    question_id: 'q-purpose-05', kind: 'decision', conversation_id: cid, exec_id: null,
    step_label: '资料用途', eyebrow: '需要你确认 · 资料用途', title: '「05-门店清单.xlsx」是数据表吗？',
    background_md: '有表头、每行一个门店，看起来是一张数据表，不是术语表。', anchors: [],
    options: [
      { id: 'A', key: 'A', label: '对，按数据 Schema 用', consequence: '发布时用来核对对象和数据表的绑定', recommended: false, preview: null, custom: false },
      { id: 'B', key: 'B', label: '改成术语表', consequence: '只用来对齐叫法，不做数据绑定', recommended: false, preview: null, custom: false },
    ],
    multi: false, allow_custom: false, custom_placeholder: '', state: 'open', answer: null, inbox_item_id: 'decision:q-purpose-05', affected: [],
  };
}

// ---------- 建议条（FL-02） ----------
const sg = (id, label, icon, action, extra = {}) => ({ id, label, icon, primary: false, disabled: false, reason: '', priority: 3, action, ...extra });
export function materialsSuggestions() {
  return {
    mode: 'normal',
    primary: sg('sg-blueprint', '生成建模蓝图', 'layers', { type: 'send', text: '生成建模蓝图', command: '/ontology-research', args: { phase: 'blueprint' } }, { primary: true, disabled: true, reason: '读完全部资料后可用', priority: 2 }),
    secondary: [
      sg('sg-map', '资料地图', 'map', { type: 'navigate', page: 'wb-materials', params: { dialog: 'material_map' } }),
      sg('sg-interrupt', '打断', 'pause', { type: 'interrupt' }),
    ],
    all_commands: true,
  };
}
export function emptySuggestions() {
  return {
    mode: 'normal', primary: null,
    secondary: [
      sg('sg-goal', '先聊聊目标', 'target', { type: 'send', text: '先聊聊目标' }),
      sg('sg-sources', '连接业务系统', 'plug', { type: 'navigate', page: 'sources' }),
    ],
    all_commands: true,
  };
}
// F8：资料分析之后不停，主推下一步「提取对象」；蓝图里的问题不挡建模，次选里去回答
export function blueprintSuggestions() {
  return {
    mode: 'normal',
    primary: sg('core.model:object', '提取对象', 'cube', { type: 'send', text: '', command: '/ontology-extract-object', args: {} }, { primary: true, priority: 4 }),
    secondary: [
      sg('sg-blueprint', '建模蓝图', 'layers', { type: 'navigate', page: 'wb-blueprint', params: { dialog: 'blueprint' } }),
    ],
    all_commands: true,
  };
}

// ---------- 对话历史 ----------
function evOf(pid, cid) {
  let seq = 3000;
  return (type, data, ts) => ({ id: String(++seq), type, ts, project_id: pid, conversation_id: cid, data });
}

const conversation = (createdAt) => [{
  id: CID, title: '资料分析', title_manual: false, created_at: createdAt, updated_at: new Date(Date.now() - MIN).toISOString(),
  live: true, state: 'idle', kind: 'chat', run_id: null,
}];

function welcome(e, { name, goal, at }) {
  return [
    e('notice', { kind: 'project_created', text: `${stamp(at)} · 项目「${name}」已创建`, version_label: null, changeset_id: null, align: 'center' }, at),
    e('message.assistant', {
      message_id: 'm-welcome',
      text_md: `你好，我是建模助手。目标我记下了：**${goal}**。\n\n先把业务资料给我。我会通读全文、给每条结论标出处，读完画一张资料地图给你看，再一起把业务切成几段来建。`,
      anchors: [], entry_links: [], continued: false,
    }, at),
    e('card', uploadCard(), at),
  ];
}

const projectOf = (pid) => projectCards().find((c) => c.id === pid);

function emptyHistory(pid, at) {
  const e = evOf(pid, CID);
  const c = projectOf(pid);
  return [...welcome(e, { name: c.name, goal: c.goal, at }), e('suggestions.updated', emptySuggestions(), at)];
}

function materialsHistory() {
  const e = evOf(MAIN_PID, CID);
  const t0 = atDaysAgo(0, 9, 12);
  const t1 = atDaysAgo(0, 9, 14);
  const c = projectOf(MAIN_PID);
  return [
    ...welcome(e, { name: c.name, goal: c.goal, at: t0 }),
    e('message.user', {
      message_id: 'm-u-files', text: '这是补货单编制的全部资料，先看看', references: [], origin: null, delivery: 'immediate',
      attachments: MATERIALS.map((m, i) => ({ upload_id: `up-m${i + 1}`, filename: m.name, size: 180000 + i * 37000, kind: attKind(kindOf(m.name)) })),
    }, t1),
    e('progress', {
      group_id: 'pg-read', title: '', desc: '', state: 'running', started_at: t1, duration_ms: null, collapsed: false,
      steps: [
        { id: 'r1', text: '识别了 7 份资料的用途，其中 1 份要你确认', meta: '按表格结构判定 · 4 秒', state: 'done', duration_ms: 4000 },
        { id: 'r2', text: '读完场景说明和两份样例补货单，覆盖 100%', meta: '1 分 12 秒', state: 'done', duration_ms: 72000 },
        { id: 'r3', text: '读完门店清单 3 个工作表，找到 12 个字段', meta: '9 秒', state: 'done', duration_ms: 9000 },
        { id: 'r4', text: '正在读补货规则第 10 章「临期损耗」', meta: '78% · 预计还要 1 分钟', state: 'running', duration_ms: null },
        { id: 'r5', text: '接下来：读商品陈列说明，然后整理统一叫法和待定问题', meta: '', state: 'dot', duration_ms: null },
      ],
    }, t1),
    e('card', materialMapCard(makeMap('reading')), t1),
    e('question.asked', purposeQuestion(CID), t1),
    e('suggestions.updated', materialsSuggestions(), t1),
  ];
}


function blueprintHistory() {
  const e = evOf(MAIN_PID, CID);
  const t0 = atDaysAgo(0, 9, 31);
  const t1 = atDaysAgo(0, 9, 33);
  return [
    e('notice', { kind: 'research_done', text: `${stamp(t0)} · 资料分析完成`, version_label: null, changeset_id: null, align: 'center' }, t0),
    e('card', researchSummaryCard(), t0),
    e('message.user', { message_id: 'm-u-bp', text: '可以，按这个出蓝图吧', attachments: [], references: [], origin: null, delivery: 'immediate' }, t1),
    e('progress', {
      group_id: 'pg-bp', title: '', desc: '', state: 'done', started_at: t1, duration_ms: 23000, collapsed: false,
      steps: [
        { id: 'b1', text: '对齐了 13 个叫法，2 个有歧义', meta: '「门店 / 要货方」「临期 / 近效期」', state: 'done', duration_ms: 21000 },
        { id: 'b2', text: '记下 3 个待你回答的问题', meta: '不挡建模', state: 'done', duration_ms: 2000 },
      ],
    }, t1),
    e('message.assistant', {
      message_id: 'm-a-bp',
      text_md: '蓝图里 2 个叫法有歧义、3 个问题待你回答，都不挡建模：没答之前按资料原文建，答了再按你的口径改。接下来提取对象。',
      anchors: [], entry_links: [], continued: true,
    }, t1),
    e('card', blueprintCard(blueprintOf()), t1),
    e('suggestions.updated', blueprintSuggestions(), t1),
  ];
}

// ---------- 流程（FL-01） ----------
function stageFlow(base, research, bpState = 'none', modelReady = false) {
  const nodes = base.nodes.map((n) => {
    if (n.id === 'core.research') return { ...n, ...research };
    if (n.id === 'evo.evolution') return n;
    const reset = { ...n, state: 'not_ready', display_state: 'not_ready', progress: null, flags: { ...n.flags, needs_review: false }, actions: [] };
    if (n.id === 'trace.trace') return { ...reset, note: '场景走查 · 建模之后才有场景' };
    if (n.id === 'core.update') return { ...reset, note: '资料有修订时再用' };
    if (n.section === 'optional') return { ...reset, actions: [{ kind: 'skip', label: '跳过' }] };
    if (n.id === 'core.model' && modelReady) return { ...reset, state: 'ready', display_state: 'ready' };
    return reset;
  });
  // F8 起不分段：后端恒给一段「全部」；还没有蓝图时 blueprint.state=none，有草稿时 draft（不用确认，不挡后面的步骤）
  const all = [{ id: 'all', name: '全部', core_flow: '', group: 'todo', stages: Object.fromEntries(base.slice_stages.map((x) => [x.id, 'not_ready'])), note: '', no_rules: false }];
  return { ...base, flow_hash: `${base.flow_hash.slice(0, 6)}m${bpState}`, nodes, slices: all, blueprint: { state: bpState }, notices: [], suggestions: null };
}
const emptyResearch = { state: 'ready', display_state: 'pending', progress: { done: 0, total: 0, label: '等资料' }, actions: [] };
export const stages = {
  materials: {
    conversations: () => conversation(atDaysAgo(0, 9, 12)),
    history: materialsHistory,
    flow: (base) => stageFlow(base, { state: 'running', display_state: 'running', progress: { done: 5, total: 7, label: '读资料 5/7' }, actions: [] }),
  },
  'materials-empty': {
    conversations: () => conversation(atDaysAgo(0, 9, 12)),
    history: () => emptyHistory(MAIN_PID, atDaysAgo(0, 9, 12)),
    flow: (base) => stageFlow(base, emptyResearch),
  },
  'estate-empty': {
    conversations: () => conversation(atDaysAgo(3, 9, 12)),
    history: () => emptyHistory(ESTATE_PID, atDaysAgo(3, 9, 12)),
    flow: (base) => stageFlow(base, emptyResearch),
  },
  blueprint: {
    conversations: () => conversation(atDaysAgo(0, 9, 12)),
    history: blueprintHistory,
    // F8：资料分析之后不停，资料分析已确认，建模可开始
    flow: (base) => stageFlow(base, { state: 'confirmed', display_state: 'confirmed', progress: null, actions: [] }, 'draft', true),
  },
};

export const projectStages = { [ESTATE_PID]: 'estate-empty' };

// 给测试用：阶段的初始数据
export const mockData = { makeMap, blueprintOf, blueprintCard, materialMapCard, candidatePool, verificationItems, CID };

// ---------- 路由 ----------
export function register(ctx) {
  const { on, json, err, uid, convsOf, flowOf, stageOf, emitConv, emitProj, runReply } = ctx;
  Object.assign(ANCHOR_RESOLUTIONS, Object.fromEntries(MORE_RESOLUTIONS.map((r) => [r.anchor, r])));
  const store = new Map();

  function st(pid) {
    if (!store.has(pid)) {
      const stage = stageOf(pid);
      const reading = stage === 'materials';
      const empty = !stage || /empty$/.test(stage);
      const pool = candidatePool();
      const map = makeMap(reading ? 'reading' : empty ? 'empty' : 'done');
      store.set(pid, {
        map,
        // 资料区的文件清单（resources-data.js）：资料地图里的几份用途、进度以资料地图为准，增删改两边同步
        files: catalogFor(stage, { cid: convsOf(pid)[0]?.id || null, map }),
        candidates: empty ? [] : reading ? pool.now : [...pool.now, ...pool.queue],
        queue: reading ? pool.queue : [],
        timer: null,
        verification: empty ? [] : verificationItems(reading),
        blueprint: empty || reading ? null : blueprintOf(stage !== 'blueprint'),
        cardShown: !empty,
        startTimer: null,
      });
    }
    return store.get(pid);
  }
  const cidOf = (pid) => convsOf(pid)[0]?.id;
  const nowIso = () => new Date().toISOString();
  const openProblems = (s) => s.map.problems.filter((p) => p.state === 'open').length;
  const mapChanged = (pid, items) => {
    const s = st(pid);
    emitProj(pid, 'materials.updated', { items, read_pct: s.map.read_pct, problems_count: openProblems(s) });
    if (s.cardShown) emitConv(pid, cidOf(pid), 'card.updated', materialMapCard(s.map));
  };
  const task = (pid, origin, title) => ({
    task_id: `task-${uid()}`, origin, title, state: 'running', percent: null, step_label: null,
    conversation_id: cidOf(pid), message_id: `m-${uid()}`, exec_id: null, started_at: nowIso(), finished_at: null, result: null,
  });
  // 页面发来的命令：对话里出现一条带来源的用户消息，助手接着做。
  // track：资料区按 task_id 跟进度条，经项目流 command.progress 推送开始与结束（契约 EP-10）
  const command = (pid, origin, text, { track = false } = {}) => {
    const t = task(pid, origin, text);
    emitConv(pid, t.conversation_id, 'message.user', { message_id: t.message_id, text, attachments: [], references: [], origin: { kind: 'page', ...origin, task_id: t.task_id }, delivery: 'immediate' });
    runReply(pid, t.conversation_id, text);
    if (track && ctx.putTask) {
      ctx.putTask(pid, t);
      setTimeout(() => ctx.putTask(pid, { ...t, state: 'done', percent: 100, finished_at: nowIso(), result: { summary: '已重读受影响的部分', refs: [] } }), 2600);
    }
    return t;
  };
  const setResearch = (pid, patch) => {
    const f = flowOf(pid);
    const n = f.nodes.find((x) => x.id === 'core.research');
    Object.assign(n, patch);
    emitProj(pid, 'flow.updated', { flow_hash: f.flow_hash, nodes: [{ id: n.id, state: n.state, display_state: n.display_state, flags: n.flags, progress: n.progress }], slices: [], suggestions_changed: true });
  };

  // 还没传资料时上传：这一批传完后开始读（资料地图卡出现在对话里）
  function startReading(pid) {
    const s = st(pid);
    const cid = cidOf(pid);
    const n = s.map.items.length;
    s.cardShown = true;
    emitConv(pid, cid, 'progress', {
      group_id: `pg-read-${uid()}`, title: '', desc: '', state: 'running', started_at: nowIso(), duration_ms: null, collapsed: false,
      steps: [
        { id: 'r1', text: `收到 ${n} 份资料，识别了用途`, meta: '按文件结构判定 · 2 秒', state: 'done', duration_ms: 2000 },
        { id: 'r2', text: `正在读「${s.map.items[0].name}」`, meta: '0%', state: 'running', duration_ms: null },
      ],
    });
    s.map.items[0].status = 'reading';
    s.map.items[0].position = '刚开始读';
    emitConv(pid, cid, 'card', materialMapCard(s.map));
    emitConv(pid, cid, 'suggestions.updated', materialsSuggestions());
    setResearch(pid, { state: 'running', display_state: 'running', progress: { done: 0, total: n, label: `读资料 0/${n}` } });
  }

  // 资料区（routes-resources.js 的 R-01、R-02、R-04、R-05）与资料分析共用这份状态：
  // state(pid).files 是资料区的文件清单，state(pid).map 是资料地图；上传、删除时由资料区调 added / removed 同步资料地图
  function materialAdded(pid, { path, name, purpose, purpose_auto }) {
    const s = st(pid);
    const existing = s.map.items.find((x) => x.path === path);
    const item = mapItem({ path, name, purpose }, { purpose_auto, queue_reason: s.map.items.length ? '读完前面的资料后开始' : null });
    if (existing) Object.assign(existing, item); else s.map.items.push(item);
    s.map.total = s.map.items.length;
    s.map.read_pct = avgPct(s.map.items);
    if (s.cardShown) {
      mapChanged(pid, [item]);
      const n = flowOf(pid).nodes.find((x) => x.id === 'core.research');
      if (n?.state === 'running' && n.progress) {
        const total = s.map.items.length;
        setResearch(pid, { progress: { ...n.progress, total, label: `读资料 ${n.progress.done}/${total}` } });
      }
    } else {
      clearTimeout(s.startTimer);
      s.startTimer = setTimeout(() => startReading(pid), 1200);
    }
    return item;
  }
  function materialRemoved(pid, path) {
    const s = st(pid);
    if (!s.map.items.some((x) => x.path === path)) return;
    s.map.items = s.map.items.filter((x) => x.path !== path);
    s.map.total = s.map.items.length;
    s.map.read_pct = avgPct(s.map.items);
    mapChanged(pid, []);
  }
  ctx.materials = { state: st, added: materialAdded, removed: materialRemoved };

  // R-03 改资料用途（资料地图弹窗、资料区都用）：清单和资料地图一起改；分析开始过就重读受影响的部分
  on('PATCH', '/projects/([^/]+)/resources/purpose', ({ m, body }) => {
    const pid = m[1];
    const s = st(pid);
    const f = s.files.find((x) => x.path === body.path);
    const it = s.map.items.find((x) => x.path === body.path);
    if (!f && !it) return err(404, 'file_not_found', '资料不存在');
    if (!PURPOSE_VALUES.includes(body.purpose)) return err(400, 'invalid_argument', '用途不对', { fields: { purpose: 'invalid' } });
    if (f) Object.assign(f, { purpose: body.purpose, purpose_auto: false, revision: `r-${uid()}`, ...(body.reading_mode ? { reading_mode: body.reading_mode } : {}) });
    if (it) {
      it.purpose = body.purpose;
      it.purpose_auto = false;
      mapChanged(pid, [it]);
    }
    const name = (f || it).name;
    const started = s.map.items.some((x) => x.read_pct > 0);
    const reread = started ? command(pid, { page: 'wb-materials', label: '资料地图' }, `「${name}」改了用途，重读受影响的部分`, { track: true }) : null;
    const { commit, version_label } = webCommit(ctx, pid, [body.path]);
    const material = f ? materialView(f, it) : { path: it.path, name: it.name, purpose: it.purpose, purpose_auto: false, kind: it.kind };
    return json(200, { material, commit, version_label, reread_task: reread });
  });

  // R-06 资料原文预览：块与出处高亮见 resources-data.js；表格、drawio 走 F-05、F-04
  on('GET', '/projects/([^/]+)/resources/preview', ({ m, query }) => {
    const path = query.get('path') || '';
    const around = query.get('around') || null;
    const s = st(m[1]);
    const f = s.files.find((x) => x.path === path);
    const it = s.map.items.find((x) => x.path === path);
    if (!f && !it) return err(404, 'file_not_found', '资料不存在', { path });
    return json(200, {
      path, format: (path.split('.').pop() || '').toLowerCase(),
      coverage: previewCoverage(f, it),
      blocks: previewBlocks(path, around, f), next_cursor: null,
    });
  });

  // R-07 资料地图
  on('GET', '/projects/([^/]+)/materials/map', ({ m }) => json(200, st(m[1]).map));

  // R-08 候选：读资料中，打开后每 1.7 秒冒出一条新的（项目流 materials.candidate）
  on('GET', '/projects/([^/]+)/materials/candidates', ({ m, query }) => {
    const pid = m[1];
    const s = st(pid);
    const after = query.get('after');
    const i = after ? s.candidates.findIndex((c) => c.id === after) : -1;
    const limit = Number(query.get('limit') || 50);
    const items = s.candidates.slice(i + 1).slice(-limit).map((c, k) => ({ ...c, created_at: new Date(Date.now() - (s.candidates.length - k) * 40000).toISOString() }));
    if (s.queue.length && !s.timer) {
      s.timer = setInterval(() => {
        const c = s.queue.shift();
        if (!c) { clearInterval(s.timer); s.timer = null; return; }
        const full = { ...c, created_at: nowIso() };
        s.candidates.push(full);
        emitProj(pid, 'materials.candidate', { candidate: full, total: s.candidates.length });
      }, 1700);
    }
    return json(200, { total: s.candidates.length, items });
  });

  // R-09 核验
  on('GET', '/projects/([^/]+)/materials/verification', ({ m }) => {
    const items = st(m[1]).verification;
    return json(200, {
      checked_at: new Date(Date.now() - 2 * MIN).toISOString(), total: items.length,
      passed: items.filter((x) => x.status === 'passed').length, problems: items.filter((x) => x.status === 'unverifiable').length, items,
    });
  });

  // R-10 资料问题记为待答问题
  on('POST', '/projects/([^/]+)/materials/problems/([^/]+)/promote', ({ m, body }) => {
    const pid = m[1];
    const s = st(pid);
    const p = s.map.problems.find((x) => x.id === m[2]);
    if (!p) return err(404, 'not_found', '问题不存在');
    if (!['blueprint_question', 'ask_me'].includes(body.as)) return err(400, 'invalid_argument', '去向不对', { fields: { as: 'invalid' } });
    p.state = 'promoted';
    mapChanged(pid, []);
    const qid = `bq-${p.id}`;
    return json(200, { question_id: qid, inbox_item_id: body.as === 'ask_me' ? `decision:${qid}` : null });
  });

  // BP-01 建模蓝图
  on('GET', '/projects/([^/]+)/blueprint', ({ m }) => {
    const bp = st(m[1]).blueprint;
    return bp ? json(200, bp) : err(404, 'blueprint_not_found', '还没有建模蓝图');
  });

  // BP-02 保存叫法与回答（F8 起不分段，前端不再发 slices）
  on('PUT', '/projects/([^/]+)/blueprint', ({ m, body }) => {
    const pid = m[1];
    const s = st(pid);
    const bp = s.blueprint;
    if (!bp) return err(404, 'blueprint_not_found', '还没有建模蓝图');
    if (body.base_revision !== bp.revision) return err(409, 'revision_conflict', '蓝图已被改过', { current_revision: bp.revision });
    (body.term_choices || []).forEach((t) => { const x = bp.terms.ambiguous.find((y) => y.id === t.term_id); if (x) x.chosen = t.chosen; });
    (body.answers || []).forEach((a) => { const q = bp.questions.find((y) => y.id === a.question_id); if (q) q.answer = { option_id: a.option_id, custom_text: a.custom_text }; });
    if (body.mode === 'adjust') {
      bp.version_no += 1;
      bp.version_label = bp.state === 'confirmed' ? `v${bp.version_no}` : `草案 v${bp.version_no}`;
    }
    bp.revision = `bp-r${uid()}`;
    bp.updated_at = nowIso();
    emitConv(pid, cidOf(pid), 'card.updated', blueprintCard(bp));
    return json(200, bp);
  });

  // BP-03 确认蓝图（F8 起界面不再调，留给 F8 之前缓存的卡片）：L2 往对话发「开始提取对象」；L4 返回自主构建预览
  on('POST', '/projects/([^/]+)/blueprint/confirm', ({ m, body }) => {
    const pid = m[1];
    const s = st(pid);
    const bp = s.blueprint;
    if (!bp) return err(404, 'blueprint_not_found', '还没有建模蓝图');
    if (body.base_revision && body.base_revision !== bp.revision) return err(409, 'revision_conflict', '蓝图已被改过', { current_revision: bp.revision });
    if (bp.state !== 'confirmed') {
      bp.state = 'confirmed';
      bp.version_label = `v${bp.version_no}`;
      bp.revision = `bp-r${uid()}`;
      emitConv(pid, cidOf(pid), 'card.updated', blueprintCard(bp));
      setResearch(pid, { state: 'confirmed', display_state: 'confirmed', progress: null, actions: [] });
      const f = flowOf(pid);
      const model = f.nodes.find((x) => x.id === 'core.model');
      Object.assign(model, { state: 'ready', display_state: 'ready', progress: null });
      emitProj(pid, 'flow.updated', { flow_hash: f.flow_hash, nodes: [{ id: model.id, state: model.state, display_state: model.display_state, flags: model.flags, progress: model.progress }], slices: [], suggestions_changed: true });
    }
    if ((body.next || 'L2') === 'L2') {
      const t = command(pid, { page: 'wb-blueprint', label: '建模蓝图' }, '确认蓝图，开始提取对象');
      emitConv(pid, cidOf(pid), 'suggestions.updated', { mode: 'normal', primary: null, secondary: [], all_commands: true });
      return json(200, { blueprint: bp, command_task: t });
    }
    return json(200, {
      blueprint: bp,
      autobuild_preview: {
        preview_token: `pv-${uid()}`, expires_at: new Date(Date.now() + 10 * MIN).toISOString(),
        slices: bp.slices.map((x) => ({ id: x.id, name: x.name, current: '未开始', included: !x.excluded })),
        production: `不采集生产库（${DB_SOURCE.id} 没授权给自主构建）`,
        below_threshold_text: '把握低于 0.80 时按保守假设先做，记进决策台账，结束后集中复核',
        warnings: [`蓝图里还有 ${bp.questions.filter((q) => !q.answer).length} 个待答问题，会按策略决定并记账`],
        queued: false, queue_position: null,
      },
    });
  });
}
