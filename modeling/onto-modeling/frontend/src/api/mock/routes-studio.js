// 3.7 Studio 条目（ST-01～ST-10）、原文编辑用到的文件接口（F-02、F-03）
// 只有主示例项目「门店补货单编制」有条目，其余项目是空知识包。条目、分段、资料名称都来自 scenario.js。
//
// mock 参数（?mock=…，和其他参数用逗号拼）：
//   submit:fail     ST-08 提交返回 rejected（未通过：「陈列上限」缺英文名 + 「生效日期」缺证据警告）
//   submit:review   ST-08 提交返回 needs_review（超出确定性检查范围，转交助手复核，复核命令发到工作台对话）
//   （缺省）         ST-08 返回 committed（检查通过，保存为下一个版本）
//   raw:big         F-02 读原文返回 truncated（大文件，前 512KB 只读）
//   raw:changed     陈列量标准的草稿基于旧版本，文件已被助手改过（2 分钟前，v0.13）：草稿标 stale、编辑时不自动载入，
//                   存旧底稿、提交返回 409（draft_state=stale）；原文按旧版本号保存返回 409
//   draft:none      陈列量标准没有未提交的草稿（缺省有一份：新增属性「区域配额上限」、修改描述）
//   scope:running / scope:pending 由 index.js 处理（写入范围锁两态）
// F-02 / F-03 在这里登记（先登记的先匹配）：不是条目文件的路径查 ctx.db.files（Map：path → {content, revision, last_change?}），
// 别的分组要 mock 文本文件时往里放，不用另登记同一路由。
import { isLocked } from '../../state/readonly.js';
import * as S from './scenario.js';

const MIN = 60 * 1000;
const ago = (ms) => new Date(Date.now() - ms).toISOString();

// ---------- 关系（声明 / 提及） ----------
// [源, 目标, relation, 标签]；声明在一级标题「关联对象」下（实线）
const DECL = [
  ['replenish_order', 'store', 'object_relation', '关联门店'],
  ['replenish_order', 'store_request', 'object_relation', '关联特殊需求'],
  ['intake_checks', 'replenish_order', 'operation_input_object', '读取补货单'],
  ['intake_checks', 'store_request', 'operation_input_object', '读取特殊需求'],
  ['intake_checks', 'r01_intake_scope', 'uses_rule', '引用'],
  ['r02_required', 'replenish_order', 'applies_to_object', '约束'],
  ['intake_checks', 'replenish_ledger', 'operation_output_object', '写入台账'],
  ['safety_stock_calc', 'goods_category', 'operation_input_object', '按品类查'],
  ['safety_stock_calc', 'safety_stock_std', 'operation_input_object', '读取安全库存标准'],
  ['safety_stock_std', 'applicable_std', 'object_relation', '按标准查'],
  ['safety_stock_calc', 'r05_by_category', 'uses_rule', '引用'],
  ['safety_stock_calc', 'r09_store_stricter', 'uses_rule', '引用'],
  ['display_qty_std', 'goods_category', 'object_relation', '按品类查'],
  ['display_qty_std', 'delivery_cycle', 'object_relation', '按到货周期查'],
  ['display_qty_check', 'display_qty_std', 'operation_input_object', '读取陈列量标准'],
  ['display_qty_check', 'delivery_cycle', 'operation_input_object', '读取到货周期'],
  ['r07_display_max', 'display_qty_std', 'applies_to_object', '约束'],
  ['r12_region_quota', 'display_qty_std', 'applies_to_object', '约束'],
  ['r09_store_stricter', 'display_qty_std', 'applies_to_object', '约束'],
  ['expiry_check', 'expiry_loss_std', 'operation_input_object', '读取临期损耗标准'],
  ['expiry_check', 'check_item', 'operation_input_object', '读取检查项目'],
  ['r08_expiry_grade', 'expiry_loss_std', 'applies_to_object', '约束'],
  ['overage_and_hint', 'overage_list', 'operation_output_object', '输出超量清单'],
  ['overage_and_hint', 'approval_hint', 'operation_output_object', '输出审批提示'],
  ['overage_and_hint', 'goods_category', 'operation_support_object', '按品类汇总'],
  ['r11_temp_increase', 'approval_hint', 'applies_to_object', '约束'],
  ['m01_overage_rate', 'overage_list', 'uses_object', '统计'],
  ['m02_first_pass', 'replenish_main', 'references', '统计'],
  ['replenish_main', 'intake_checks', 'calls_operation', '调用'],
  ['replenish_main', 'safety_stock_calc', 'calls_operation', '调用'],
  ['replenish_main', 'display_qty_check', 'calls_operation', '调用'],
  ['replenish_main', 'expiry_check', 'calls_operation', '调用'],
  ['replenish_main', 'overage_and_hint', 'calls_operation', '调用'],
  ['render_order', 'overage_list', 'operation_input_object', '读取超量清单'],
  ['render_order', 'replenish_order', 'operation_output_object', '生成补货单'],
  ['render_order', 'build_replenish_order', 'uses_script', '调用脚本'],
  ['build_replenish_order', 'replenish_order', 'script_uses_object', '读写'],
];
// 正文里的提及（虚线，不算关系）：[源, 目标, 在哪儿提到]
const MENTION = [
  ['display_qty_std', 'delivery_cycle', '在描述中提到'],
  ['display_qty_std', 'check_item', '在说明中提到'],
  ['display_qty_std', 'goods_category', '在示例中提到'],
  ['display_qty_std', 'overage_list', '在规则 R-11 中提到'],
  ['approval_hint', 'replenish_ledger', '在说明中提到'],
  ['store_request', 'r09_store_stricter', '在说明中提到'],
  ['expiry_loss_std', 'goods_category', '在示例中提到'],
  ['overage_list', 'replenish_order', '在描述中提到'],
];
// 被引用时的说法（「陈列量核定 · 被读取」）
const PASSIVE = {
  operation_input_object: '被读取', operation_output_object: '由它输出', operation_support_object: '被参考',
  applies_to_object: '约束', object_relation: '被关联', calls_operation: '被调用', uses_rule: '被引用',
  uses_object: '被统计', references: '被统计', uses_script: '被调用', script_uses_object: '被读写',
};

const DESC = {
  replenish_order: '店长提交的一张补货单：门店、商品、申请量和到货周期，按补货规则逐项核定后下单。',
  store: '提交补货申请的门店，带门店等级和所属区域。',
  store_request: '门店对某个品类提出的特殊需求，比如申请量上限。',
  replenish_ledger: '记录每张补货单的审批状态和下单结果。',
  goods_category: '商品所属品类，决定适用的补货标准。',
  safety_stock_std: '按品类确定的安全库存下限，门店申请严于标准时按门店执行。',
  applicable_std: '品类陈列与补货适用的标准及版本。',
  display_qty_std: '按商品品类和到货周期确定的陈列量上下限。',
  delivery_cycle: '商品的到货周期：每日、隔日或每周。',
  expiry_loss_std: '临期商品的损耗限值，保质期剩余 3 天以内下架。',
  check_item: '到货时要核对的检查项目及频次。',
  overage_list: '申请量超出标准的商品清单，注明标准值与申请值。',
  approval_hint: '超出标准时生成的审批提示，挂到补货台账，交区域审批。',
  store_level: '门店等级，目前没有被其他条目引用。',
  history_order: '历史补货单，目前没有被其他条目引用。',
  replenish_main: '补货编制的主流程：受理、测算、核定、检查，最后生成补货单文本。',
  intake_checks: '受理补货申请时的三道校验：受理范围、必填项、门店状态。',
  safety_stock_calc: '按品类取安全库存标准，再和门店申请取严。',
  display_qty_check: '按商品品类和到货周期查陈列量标准，核定陈列量。',
  expiry_check: '按临期分级核对损耗，生成临期处理条款。',
  overage_and_hint: '列出超量商品，生成审批提示并挂到补货台账。',
  render_order: '把核定结果拼成补货单文本。',
  build_replenish_order: '生成补货单的脚本。',
};

// 对象的属性：[属性, 英文名, 类型]；证据缺省是表格资料的表头（门店类对象取门店清单，其余取商品品类表）
const FIELDS = {
  replenish_order: [['门店', 'store_code', '文本'], ['商品', 'goods_code', '文本'], ['申请量', 'request_qty', '数值 · 箱'], ['到货周期', 'delivery_cycle', '枚举']],
  store: [['门店编号', 'store_code', '文本'], ['门店名称', 'store_name', '文本'], ['门店等级', 'level_code', '枚举']],
  store_request: [['门店', 'store_code', '文本'], ['品类', 'category_code', '文本'], ['申请上限', 'max_qty', '数值 · 箱']],
  replenish_ledger: [['补货单号', 'order_no', '文本'], ['审批状态', 'approval_state', '枚举']],
  goods_category: [['品类编码', 'category_code', '文本'], ['品类名称', 'category_name', '文本']],
  safety_stock_std: [['品类', 'category_code', '文本'], ['安全库存', 'safety_qty', '数值 · 箱']],
  applicable_std: [['标准编号', 'std_code', '文本'], ['版本', 'std_version', '文本']],
  delivery_cycle: [['周期', 'cycle_code', '枚举']],
  expiry_loss_std: [['品类', 'category_code', '文本'], ['剩余天数', 'remain_days', '数值 · 天']],
  check_item: [['检查项目', 'item_code', '文本'], ['频次', 'frequency', '枚举']],
  overage_list: [['商品', 'goods_code', '文本'], ['标准值', 'std_qty', '数值 · 箱'], ['申请值', 'request_qty', '数值 · 箱']],
  approval_hint: [['补货单号', 'order_no', '文本'], ['提示内容', 'hint_text', '文本']],
  store_level: [['等级', 'level_code', '枚举']],
  history_order: [['补货单号', 'order_no', '文本']],
};
const SCHEMA_SHEET = { store: ['05-门店清单.xlsx', '门店'], store_level: ['05-门店清单.xlsx', '门店'], store_request: ['05-门店清单.xlsx', '门店'] };
const TABLE = {
  replenish_order: ['erp.replenish_order', '补货单表'], store: ['erp.store', '门店表'], store_request: ['erp.store_request', '门店需求表'],
  replenish_ledger: ['erp.replenish_ledger', '补货台账表'], goods_category: ['erp.goods_category', '商品品类表'],
  safety_stock_std: ['erp.replenish_std', '补货标准表'], applicable_std: ['erp.std_catalog', '标准目录表'],
  display_qty_std: ['erp.replenish_std', '补货标准表'], delivery_cycle: ['erp.delivery_cycle', '到货周期表'],
  expiry_loss_std: ['erp.expiry_std', '临期标准表'], check_item: ['erp.check_item', '检查项目表'],
};

// 陈列量标准（设计稿选中的条目）手写：属性带证据，覆盖资料原文 / 专家确认 / 缺证据
const AN = {
  cat: 'file:resource/schema/06-商品品类表.xlsx#sheet:品类!B2',
  cycle: 'file:resource/schema/06-商品品类表.xlsx#sheet:品类!D2',
  max: 'file:resource/schema/06-商品品类表.xlsx#sheet:品类!E2',
  std: 'file:resource/schema/06-商品品类表.xlsx#sheet:品类!C2',
  r73: 'file:resource/others/02-门店补货规则.docx#p:7.3',
  show3: 'file:resource/others/04-商品陈列说明.pdf#p:3',
  rev2: 'file:resource/others/09-修订通知.docx#p:2',
};
const ev = (anchor, label, scheme, confidence = null) => ({ anchor, label, scheme, confidence, expert_confirmed: false, missing: false });
const EV_NONE = { anchor: null, label: '', scheme: null, confidence: null, expert_confirmed: false, missing: true };
const EV_EXPERT = { anchor: null, label: '专家确认', scheme: null, confidence: null, expert_confirmed: true, missing: false };
const FOCUS_FIELDS = [
  { name: '商品品类', column: 'category_code', type: '文本', evidence: ev(AN.cat, '商品品类表 品类 B2', 'file') },
  { name: '到货周期', column: 'delivery_cycle', type: '枚举', evidence: ev(AN.cycle, '商品品类表 品类 D2', 'file') },
  { name: '陈列上限', column: 'display_max', type: '数值 · 箱', evidence: ev(AN.r73, '补货规则 §7.3', 'file') },
  { name: '陈列下限', column: 'display_min', type: '数值 · 箱', evidence: ev(AN.show3, '商品陈列说明 第 3 节', 'file') },
  { name: '适用标准', column: 'std_code', type: '文本', evidence: ev(AN.std, '商品品类表 品类 C2', 'file') },
  { name: '检查方法', column: 'check_method', type: '文本', evidence: EV_EXPERT },
  { name: '生效日期', column: 'effective_from', type: '日期', evidence: EV_NONE },
];
// 证据侧栏（ST-06）：比属性多一条品类表的标准上限（陈列上限同时有品类表和补货规则两处出处）
const FOCUS_EVIDENCE = [
  [AN.max, 'file', '商品品类表 品类 E2', null, ['陈列上限']],
  [AN.cycle, 'file', '商品品类表 品类 D2', null, ['到货周期']],
  [AN.std, 'file', '商品品类表 品类 C2', null, ['适用标准']],
  [AN.r73, 'file', '补货规则 §7.3', null, ['陈列上限']],
  [AN.show3, 'file', '商品陈列说明 第 3 节', null, ['陈列下限']],
  [AN.cat, 'file', '商品品类表 品类 B2', null, ['商品品类']],
];

// EV-29 悬浮原文（index.js 的 /anchors/resolve 查 F.ANCHOR_RESOLUTIONS，这里把 Studio 用到的锚点补进去）
function resolution(anchor, source_label, location_label, marked, confidence = null) {
  let text = '';
  const highlights = [];
  marked.split(/(<mark>.*?<\/mark>)/).forEach((part) => {
    const m = part.match(/^<mark>(.*)<\/mark>$/);
    if (m) { highlights.push({ start: text.length, end: text.length + m[1].length }); text += m[1]; } else text += part;
  });
  const scheme = anchor.split(':')[0];
  return {
    anchor, scheme, resolved: true, source_label, location_label, text, highlights, confidence,
    snapshot_id: null, page: 'resources',
  };
}
const RESOLUTIONS = [
  resolution(AN.cat, '06-商品品类表.xlsx', '品类 B2', '牛奶一行「品类」：<mark>乳制品</mark>'),
  resolution(AN.cycle, '06-商品品类表.xlsx', '品类 D2', '牛奶一行「到货周期」：<mark>每日</mark>'),
  resolution(AN.std, '06-商品品类表.xlsx', '品类 C2', '牛奶一行「适用标准」：<mark>乳制品陈列标准 2024</mark>'),
  resolution(AN.r73, '02-门店补货规则.docx', '§7.3 陈列量', '陈列量按适用标准规定的<mark>上限</mark>执行，单位：箱。'),
  resolution(AN.show3, '04-商品陈列说明.pdf', '第 3 节', '每个品类陈列量<mark>不低于下限</mark>，下限为空时按 0 处理。'),
  resolution(AN.rev2, '09-修订通知.docx', '第 2 条', '乳制品陈列量按<mark>区域配额 ≤150 箱</mark> 执行，自 11 月 1 日起生效。'),
];

const EV_LABEL = { [AN.rev2]: '09 修订通知 第 2 条', [AN.r73]: '补货规则 §7.3', [AN.show3]: '商品陈列说明 第 3 节' };

// 默认草稿：新增属性「区域配额上限」（证据：09 修订通知 第 2 条）、修改描述
const DRAFT_FIELD = { name: '区域配额上限', column: 'region_quota_max', type: '数值 · 箱', evidence_anchor: AN.rev2, expert_confirmed: false };
const DRAFT_DESC = '按商品品类和到货周期确定的陈列量上下限，乳制品另有区域配额上限。';

// 操作流程面板（ST-05）
const MAIN_FLOW = ['intake_checks', 'safety_stock_calc', 'display_qty_check', 'overage_and_hint', 'render_order'];
const STEPS = {
  display_qty_check: [
    '按 [[商品品类]] 和 [[到货周期]] 查 [[陈列量标准]]',
    '乳制品取区域配额上限 ≤150 箱（规则 R-12）',
    '门店申请严于标准时按门店执行（规则 R-09）',
    '输出陈列量条款，交给「超量与审批提示」',
  ],
  overage_and_hint: [
    '逐个商品比较 [[补货单]] 的申请量和标准上限',
    '门店申请严于标准时按门店执行（规则 R-09）',
    '超出标准的写进 [[超量清单]]，生成 [[审批提示]]',
    '审批提示挂到 [[补货台账]]，交区域审批',
  ],
  safety_stock_calc: ['按 [[商品品类]] 查 [[安全库存标准]]', '门店申请严于标准时按门店执行（规则 R-09）', '输出安全库存条款'],
  intake_checks: ['核对受理范围（规则 R-01）', '检查必填项（规则 R-02）', '读取 [[门店特殊需求]]，写入 [[补货台账]]'],
  expiry_check: ['按 [[临期损耗标准]] 分级（规则 R-08）', '按 [[检查项目]] 核对到货批次'],
  render_order: ['读取 [[超量清单]]，拼出补货单文本', '调用 build_replenish_order.py 生成文件'],
  replenish_main: ['受理三道校验', '安全库存测算', '陈列量核定', '临期损耗编制', '超量与审批提示', '生成补货单文本'],
};

const SECTION_TITLE = { operation: '步骤', rule: '条件与结论', metric: '口径', script: '说明' };

function nextVersion(label) {
  const m = String(label || 'v0.12').match(/^v(\d+)\.(\d+)$/);
  return m ? `v${m[1]}.${Number(m[2]) + 1}` : 'v0.13';
}

export function register(ctx) {
  const { on, json, err, db, sc, uid, F, cardOf, convsOf, emitConv, emitProj, scopeOf, runReply } = ctx;
  RESOLUTIONS.forEach((r) => { F.ANCHOR_RESOLUTIONS[r.anchor] = r; });

  const isMain = (pid) => pid === F.MAIN_PID;
  const refOf = (key) => S.entryRef(key);
  const byId = new Map(S.ENTRIES.map((x) => [x.id, x]));
  const byKey = new Map(S.ENTRIES.map((x) => [x.key, x]));
  const diagramOn = () => (F.extensions().artifact_types || []).includes('diagram.flowchart');
  const pathOf = (x) => (x.kind === 'script' ? `scripts/${x.name}` : `ontology/${x.kind}/${x.name}.md`);

  // ---------- 条目仓库（主线）与草稿 ----------
  const st = db.studio || (db.studio = { entries: new Map(), drafts: new Map(), rev: 1, deleted: new Set(), rawChanged: sc.raw === 'changed', flowcharts: S.FLOWCHARTS.map((f) => ({ ...f })) });
  const live = () => S.ENTRIES.filter((x) => !st.deleted.has(x.id));

  function baseEntry(x) {
    const out = DECL.filter((d) => d[0] === x.key && byKey.has(d[1]));
    const inc = DECL.filter((d) => d[1] === x.key && byKey.has(d[0]));
    const declared = [
      ...out.map((d) => ({ target: refOf(d[1]), label: d[3] })),
      ...inc.map((d) => ({ target: refOf(d[0]), label: PASSIVE[d[2]] || d[3] })),
    ];
    const mentions = MENTION.filter((m) => m[0] === x.key).map((m) => ({ target: refOf(m[1]), where: m[2] }));
    const backlinks = inc.map((d) => ({ source: refOf(d[0]), relation: d[3] }));
    st.flowcharts.filter((f) => f.operation === x.key).forEach((f) => backlinks.push({ source: { type: f.type, key: f.key, title: f.title }, relation: '画出流程' }));
    if (x.key === 'display_qty_std') backlinks.push({ source: { type: 'trace.scenario', key: 's-04', title: '场景 4 · 乳制品隔日到货 ≤150 箱' }, relation: '输入' });
    let fields = [];
    if (x.key === 'display_qty_std') fields = FOCUS_FIELDS.map((f) => ({ ...f, evidence: { ...f.evidence } }));
    else if (x.kind === 'object') {
      const [file, sheet] = SCHEMA_SHEET[x.key] || ['06-商品品类表.xlsx', '品类'];
      const short = file.replace(/^\d+-|\.xlsx$/g, '');
      fields = (FIELDS[x.key] || []).map(([name, column, type], i) => ({
        name, column, type,
        evidence: x.orphan && i > 0 ? { ...EV_NONE } : ev(`file:resource/schema/${file}#sheet:${sheet}!${'ABCDEFGH'[i]}1`, `${short} ${sheet} ${'ABCDEFGH'[i]}1`, 'file'),
      }));
    }
    const sections = [{ level: 1, title: x.name, content_md: DESC[x.key] || '' }];
    if (STEPS[x.key]) sections.push({ level: 2, title: '步骤', content_md: STEPS[x.key].map((s, i) => `${i + 1}. ${s}`).join('\n') });
    else if (x.kind === 'rule') sections.push({ level: 2, title: '条件与结论', content_md: `${x.name.replace(/^R-\d+\s*/, '')}：满足条件时按规则执行，否则生成审批提示。` });
    else if (x.kind === 'metric') sections.push({ level: 2, title: '口径', content_md: `${x.name.replace(/^M-\d+\s*/, '')} = 统计周期内符合条件的补货单数 ÷ 补货单总数。` });
    const [table, tableLabel] = TABLE[x.key] || [];
    return {
      id: x.id, kind: x.kind, key: x.key, name: x.name, path: pathOf(x),
      description: x.key === 'display_qty_std' ? DESC.display_qty_std : (DESC[x.key] || `${x.name}的说明。`),
      status: { overage_list: 'pending', overage_and_hint: 'pending', m01_overage_rate: 'pending', display_qty_check: 'stale' }[x.key] || 'confirmed',
      needs_review: null,
      sections,
      fields,
      relations: { declared, mentions },
      backlinks,
      data_source: x.kind !== 'object' ? { kind: 'unset', value: null, label: '' }
        : table ? { kind: 'table', value: table, label: `业务系统 ERP · ${tableLabel}` } : { kind: 'none', value: null, label: '无' },
      revision: `r${x.key.length}-1`,
      raw: null,
    };
  }
  function stored(id) {
    const x = byId.get(id);
    if (!x || st.deleted.has(id)) return null;
    if (!st.entries.has(id)) st.entries.set(id, baseEntry(x));
    return st.entries.get(id);
  }
  // 缺省草稿
  if (!st.seeded) {
    st.seeded = true;
    if (sc.draft !== 'none') {
      const e = stored(S.entry(S.FOCUS_ENTRY).id);
      const form = formOf(e);
      form.fields.splice(3, 0, { ...DRAFT_FIELD });
      form.description = DRAFT_DESC;
      st.drafts.set(e.id, { author: 'zhangsan', updated_at: ago(40 * MIN), base_revision: e.revision, form, content: renderMd(e, form) });
    }
  }

  function formOf(e) {
    return {
      name: e.name, key: e.key, description: e.description,
      fields: e.fields.map((f) => ({ name: f.name, column: f.column, type: f.type, evidence_anchor: f.evidence.anchor, expert_confirmed: f.evidence.expert_confirmed })),
      relations: [...new Set(e.relations.declared.map((r) => r.target.id))].filter((id) => !e.backlinks.some((b) => b.source.id === id)),
      data_source: e.data_source.kind === 'unset' ? { kind: 'none' } : { kind: e.data_source.kind, value: e.data_source.value || undefined },
      sections: Object.fromEntries(e.sections.filter((s) => s.level >= 2).map((s) => [s.title, s.content_md])),
    };
  }
  // 服务端按元模型把表单渲染成 Markdown（与设计稿「编辑原文」里的样子一致）
  function renderMd(e, form = formOf(e)) {
    const lines = [`# ${form.name}`, '', form.description];
    if (e.kind === 'object') {
      lines.push('', '## 属性', '| 属性 | 英文名 | 类型 |', '| --- | --- | --- |', ...form.fields.map((f) => `| ${f.name} | ${f.column} | ${f.type} |`));
    }
    Object.entries(form.sections || {}).forEach(([t, c]) => lines.push('', `## ${t}`, c));
    const rel = (form.relations || []).map((id) => byId.get(id)).filter(Boolean).filter((x) => x.kind === 'object');
    if (rel.length) lines.push('', '# 关联对象', rel.map((x) => `[[${x.name}]]`).join('、'));
    if (e.kind === 'object') lines.push('', '## 数据来源', form.data_source?.kind === 'none' ? '无' : (form.data_source?.value || ''));
    return lines.join('\n');
  }
  // 证据的业务说法：资料写「简称 + 位置」；不显示原始锚点
  const evidenceFor = (anchor, fallback) => {
    if (!anchor) return { ...EV_NONE };
    const r = F.ANCHOR_RESOLUTIONS[anchor];
    const scheme = anchor.split(':')[0];
    let label = fallback?.anchor === anchor ? fallback.label : EV_LABEL[anchor];
    if (!label && r) label = `${r.source_label.replace(/\.\w+$/, '')} ${r.location_label}`;
    return { anchor, label: label || anchor.split('/').pop(), scheme, confidence: r?.confidence ?? null, expert_confirmed: false, missing: false };
  };
  function applyForm(e, form) {
    const old = new Map(e.fields.map((f) => [f.column, f]));
    e.name = form.name;
    e.description = form.description;
    e.fields = (form.fields || []).map((f) => ({
      name: f.name, column: f.column, type: f.type,
      evidence: f.expert_confirmed ? { ...EV_EXPERT } : evidenceFor(f.evidence_anchor, old.get(f.column)?.evidence),
    }));
    e.sections = [{ level: 1, title: form.name, content_md: form.description }, ...Object.entries(form.sections || {}).map(([title, content_md]) => ({ level: 2, title, content_md }))];
    if (form.data_source) {
      const v = form.data_source.value || '';
      e.data_source = form.data_source.kind === 'none' ? { kind: 'none', value: null, label: '无' } : { kind: form.data_source.kind, value: v, label: e.data_source.value === v ? e.data_source.label : v };
    }
    e.status = 'confirmed';
  }

  const lockOf = (pid, ref) => {
    const lock = scopeOf(pid);
    return isLocked(lock, ref) ? lock : null;
  };
  // 文件当前版本：raw:changed 时陈列量标准被助手改过，比条目记的版本新
  const fileRevision = (e) => (st.rawChanged && e.key === S.FOCUS_ENTRY ? `${e.revision}-a` : e.revision);
  // 草稿带 state：基于的版本已不是文件当前版本时是 stale，不算编辑中（develop !608）
  function draftOf(e) {
    const d = st.drafts.get(e.id);
    if (!d) return null;
    return { ...d, state: d.base_revision && d.base_revision !== fileRevision(e) ? 'stale' : 'active' };
  }
  const editing = (e) => draftOf(e)?.state === 'active';
  function entryView(pid, e) {
    const ref = { kind: e.kind, id: e.id, key: e.key, name: e.name };
    const draft = draftOf(e);
    const scope = scopeOf(pid);
    return {
      ...e,
      raw: undefined,
      revision: fileRevision(e),
      status: editing(e) ? 'editing' : e.status,
      backlinks: e.backlinks.map((b) => ({ ...b, locked: isLocked(scope, b.source) })),
      counts: { refs_out: e.relations.declared.length, refs_in: e.backlinks.length, fields: e.fields.length },
      head: cardOf(pid)?.head || 'a71e03c9',
      lock: lockOf(pid, ref),
      draft,
    };
  }
  function evidenceItems(e) {
    if (e.key === 'display_qty_std' && e.fields.length >= FOCUS_FIELDS.length) {
      const extra = e.fields.filter((f) => f.evidence.anchor && !FOCUS_EVIDENCE.some((x) => x[0] === f.evidence.anchor));
      return [...FOCUS_EVIDENCE.map(([anchor, scheme, title, confidence, used_for]) => ({ anchor, scheme, title, confidence, used_for })),
        ...extra.map((f) => ({ anchor: f.evidence.anchor, scheme: f.evidence.scheme, title: f.evidence.label, confidence: f.evidence.confidence, used_for: [f.name] }))];
    }
    return e.fields.filter((f) => f.evidence.anchor).map((f) => ({
      anchor: f.evidence.anchor, scheme: f.evidence.scheme, title: f.evidence.label, confidence: f.evidence.confidence, used_for: [f.name],
    }));
  }
  function row(pid, e) {
    const v = entryView(pid, e);
    return {
      id: e.id, kind: e.kind, key: e.key, name: e.name,
      refs_out: v.counts.refs_out, refs_in: v.counts.refs_in,
      status: v.status, needs_review: false, locked: !!v.lock, plugin: null,
      // 最后更新时间：按 id 摊开的合成时间（脚本取不到，写 null）
      updated_at: e.kind === 'script' ? null : ago(([...e.id].reduce((n, c) => n + c.charCodeAt(0), 0) % 97) * 37 * MIN),
    };
  }
  function flowRows(pid) {
    if (!diagramOn()) return [];
    const scope = scopeOf(pid);
    return st.flowcharts.map((f) => ({
      id: `flowchart:${f.key}`, kind: 'flowchart', key: f.key, name: f.title,
      refs_out: f.key === 'overage-flow' ? 5 : 7, refs_in: f.operation ? 1 : 0,
      status: f.state === 'draft' ? 'draft' : 'confirmed', needs_review: false,
      locked: isLocked(scope, { type: f.type, key: f.key }), plugin: { name: 'diagram', label: '图示' },
      updated_at: ago(3 * 60 * MIN),
    }));
  }
  // 条目表的顺序：按类型，再按设计稿那一屏的先后
  const ROW_ORDER = ['replenish_order', 'store', 'store_request', 'goods_category', 'safety_stock_std', 'display_qty_std', 'delivery_cycle', 'overage_list', 'approval_hint', 'replenish_ledger', 'applicable_std', 'expiry_loss_std', 'check_item', 'store_level', 'history_order',
    'replenish_main', 'intake_checks', 'safety_stock_calc', 'display_qty_check', 'overage_and_hint', 'expiry_check', 'render_order',
    'r09_store_stricter', 'r12_region_quota', 'r01_intake_scope', 'r02_required', 'r03_required_hint', 'r05_by_category', 'r07_display_max', 'r08_expiry_grade', 'r11_temp_increase',
    'm01_overage_rate', 'm02_first_pass'];
  const KIND_RANK = { object: 0, operation: 1, rule: 2, metric: 3, flowchart: 4, script: 5 };
  function allRows(pid) {
    if (!isMain(pid)) return [];
    const rank = (r) => KIND_RANK[r.kind] * 1000 + (ROW_ORDER.indexOf(r.key) + 1 || 999);
    const entries = live().map((x) => row(pid, stored(x.id))).sort((a, b) => rank(a) - rank(b));
    const sIdx = entries.findIndex((r) => r.kind === 'script');
    entries.splice(sIdx < 0 ? entries.length : sIdx, 0, ...flowRows(pid));
    return entries;
  }

  const found = (pid, id) => {
    if (!isMain(pid)) return null;
    return stored(decodeURIComponent(id));
  };

  // ---------- ST-01 统计 ----------
  on('GET', '/projects/([^/]+)/studio/stats', ({ m }) => {
    const pid = m[1];
    const rows = allRows(pid);
    const n = (k) => rows.filter((r) => r.kind === k).length;
    const trace = (F.extensions().nav || []).some((x) => x.id === 'scenarios');
    const c = cardOf(pid);
    return json(200, {
      objects: n('object'), operations: n('operation'), rules: n('rule'), metrics: n('metric'), scripts: n('script'),
      relations: isMain(pid) ? DECL.length : 0,
      flowcharts: diagramOn() ? n('flowchart') : null,
      scenarios: trace && isMain(pid) ? { total: 23, passed: 19, pass_rate_pct: 83 } : null,
      version_label: c?.version_label || 'v0.1', head: c?.head || 'a71e03c9',
    });
  });

  // ---------- ST-02 关系图 ----------
  on('GET', '/projects/([^/]+)/studio/graph', ({ m, query }) => {
    const pid = m[1];
    const withMentions = query.get('include_mentions') !== 'false';
    if (!isMain(pid)) return json(200, { head: 'a71e03c9', nodes: [], edges: [], clusters: [], chains: { rule: [], metric: [] } });
    const scope = scopeOf(pid);
    const items = live().map((x) => stored(x.id));
    const nodes = items.map((e) => ({
      id: e.id, kind: e.kind, key: e.key, label: e.name, description: e.description,
      status: editing(e) ? 'editing' : e.status, locked: isLocked(scope, { id: e.id }),
      slice_ids: byId.get(e.id).slice ? [byId.get(e.id).slice] : [],
    }));
    const idOf = (k) => byKey.get(k)?.id;
    const alive = (k) => idOf(k) && !st.deleted.has(idOf(k));
    const edges = DECL.filter((d) => alive(d[0]) && alive(d[1])).map((d, i) => ({ id: `d${i}`, source: idOf(d[0]), target: idOf(d[1]), kind: 'declared', relation: d[2], label: d[3] }));
    if (withMentions) MENTION.filter((x) => alive(x[0]) && alive(x[1])).forEach((x, i) => edges.push({ id: `m${i}`, source: idOf(x[0]), target: idOf(x[1]), kind: 'mention', relation: 'references', label: x[2] }));
    const clusters = S.SLICES.map((s) => ({ id: `cl-${s.id}`, label: s.name, slice_id: s.id, node_ids: nodes.filter((nd) => nd.slice_ids.includes(s.id)).map((nd) => nd.id) }));
    const ruleIds = nodes.filter((nd) => nd.kind === 'rule').map((nd) => nd.id);
    const ruleTargets = edges.filter((e) => e.kind === 'declared' && ruleIds.includes(e.source)).map((e) => e.target);
    const metricIds = nodes.filter((nd) => nd.kind === 'metric').map((nd) => nd.id);
    const metricTargets = edges.filter((e) => e.kind === 'declared' && metricIds.includes(e.source)).map((e) => e.target);
    return json(200, {
      head: cardOf(pid)?.head || 'a71e03c9', nodes, edges, clusters,
      chains: { rule: [...new Set([...ruleIds, ...ruleTargets])], metric: [...new Set([...metricIds, ...metricTargets, idOf('render_order')])] },
    });
  });

  // ---------- ST-03 条目表 ----------
  on('GET', '/projects/([^/]+)/studio/entries', ({ m, query }) => {
    const pid = m[1];
    const kinds = (query.get('kind') || '').split(',').filter(Boolean);
    const q = (query.get('q') || '').trim().toLowerCase();
    const status = query.get('status');
    const rows = allRows(pid).filter((r) => !q || `${r.name}\n${r.key}`.toLowerCase().includes(q));
    const n = (k) => rows.filter((r) => r.kind === k).length;
    const items = rows.filter((r) => (!kinds.length || kinds.includes(r.kind)) && (!status || r.status === status));
    return json(200, {
      items, next_cursor: null,
      counts: { all: rows.length, object: n('object'), operation: n('operation'), rule: n('rule'), metric: n('metric'), script: n('script'), flowchart: diagramOn() ? n('flowchart') : null },
      kinds: [['object', '对象'], ['operation', '操作'], ['rule', '规则'], ['metric', '指标'], ...(diagramOn() ? [['flowchart', '流程图']] : []), ['script', '脚本']]
        .map(([kind, label]) => ({ kind, label, plugin: kind === 'flowchart' ? { name: 'diagram', label: '图示' } : null })),
    });
  });

  // ---------- ST-04 条目详情 ----------
  on('GET', '/projects/([^/]+)/studio/entries/([^/]+)', ({ m }) => {
    const id = decodeURIComponent(m[2]);
    if (id.startsWith('flowchart:')) return err(410, 'endpoint_removed', '流程图走插件产物接口', { successor: 'AR-03' });
    const e = found(m[1], id);
    if (!e) return err(404, 'not_found', '条目不存在');
    return json(200, entryView(m[1], e));
  });

  // ---------- ST-05 操作流程面板 ----------
  on('GET', '/projects/([^/]+)/studio/entries/([^/]+)/flow', ({ m }) => {
    const e = found(m[1], m[2]);
    if (!e) return err(404, 'not_found', '条目不存在');
    if (e.kind !== 'operation') return err(400, 'invalid_argument', '只有操作有流程面板');
    // 业务流程：主流程上的几个操作，本操作高亮；陈列量核定前面有一个子操作「确定到货周期」
    let sub = MAIN_FLOW.includes(e.key) ? MAIN_FLOW : ['intake_checks', 'safety_stock_calc', 'expiry_check', 'overage_and_hint', 'render_order'];
    if (e.key === 'display_qty_check') sub = ['intake_checks', '#cycle', 'display_qty_check', 'overage_and_hint', 'render_order'];
    const steps = sub.map((k) => {
      if (k === '#cycle') return { label: '确定到货周期', owner_label: '陈列量核定 · 子操作', entry: refOf('delivery_cycle'), hit: false };
      const x = byKey.get(k);
      const hit = k === e.key && e.key !== 'replenish_main';
      let owner = k === 'intake_checks' ? '补货申请与门店需求' : x.slice ? S.SLICES.find((s) => s.id === x.slice)?.name : '补货编制主流程';
      if (hit) owner = k === 'display_qty_check' ? '引用「陈列量标准」' : `${owner} · 本操作`;
      return { label: k === 'intake_checks' ? '受理补货申请' : x.name, owner_label: owner, entry: refOf(k), hit };
    });
    return json(200, {
      operation: refOf(e.key),
      business_flow: { steps, main_operation: refOf('replenish_main'), sub_operations: 4, referenced_by_scenarios: e.key === 'display_qty_check' ? 3 : 2 },
      steps: (STEPS[e.key] || []).map((text_md, i) => ({ index: i + 1, text_md })),
      diagrams: st.flowcharts.filter((f) => f.operation === e.key).map((f) => ({ type: f.type, key: f.key, title: f.title })),
    });
  });

  // ---------- ST-06 证据侧栏 ----------
  on('GET', '/projects/([^/]+)/studio/entries/([^/]+)/evidence', ({ m }) => {
    const e = found(m[1], m[2]);
    if (!e) return err(404, 'not_found', '条目不存在');
    const items = evidenceItems(e).map((x) => ({
      ...x,
      resolved: F.ANCHOR_RESOLUTIONS[x.anchor] || { anchor: x.anchor, scheme: x.scheme, resolved: true, source_label: x.anchor.startsWith('file:') ? x.anchor.slice(5).split('#')[0].split('/').pop() : x.title, location_label: x.title.split(' ').slice(1).join(' '), text: '', highlights: [], confidence: x.confidence, snapshot_id: null, page: 'resources' },
    }));
    return json(200, {
      snapshot_basis: [],
      items,
    });
  });

  // ---------- ST-07 草稿 ----------
  on('GET', '/projects/([^/]+)/studio/entries/([^/]+)/draft', ({ m }) => {
    const e = found(m[1], m[2]);
    if (!e) return err(404, 'not_found', '条目不存在');
    return json(200, { draft: draftOf(e) });
  });
  on('PUT', '/projects/([^/]+)/studio/entries/([^/]+)/draft', ({ m, body }) => {
    const e = found(m[1], m[2]);
    if (!e) return err(404, 'not_found', '条目不存在');
    if (!body.form && body.content == null) return err(400, 'invalid_argument', 'form 和 content 必选其一', { fields: { form: 'required' } });
    if (body.base_revision && body.base_revision !== fileRevision(e)) return err(409, 'revision_conflict', '正式条目已更新，请刷新后基于最新内容重新编辑', { draft_state: 'stale' });
    const draft = {
      author: 'zhangsan', updated_at: new Date().toISOString(), base_revision: body.base_revision || fileRevision(e),
      form: body.form || null, content: body.content != null ? body.content : renderMd(e, body.form),
    };
    st.drafts.set(e.id, draft);
    return json(200, { saved_at: draft.updated_at, draft: draftOf(e) });
  });
  on('DELETE', '/projects/([^/]+)/studio/entries/([^/]+)/draft', ({ m }) => {
    const e = found(m[1], m[2]);
    if (!e) return err(404, 'not_found', '条目不存在');
    st.drafts.delete(e.id);
    return new Response(null, { status: 204 });
  });

  function committed(pid, e, paths) {
    const c = cardOf(pid);
    const version_label = nextVersion(c?.version_label);
    const commit = `c${uid().padStart(7, '0')}`;
    if (c) { c.version_label = version_label; c.head = commit; }
    e.revision = `r${uid()}`;
    const ref = { kind: e.kind, id: e.id, key: e.key, name: e.name };
    emitProj(pid, 'workspace.changed', { commit, version_label, source: 'studio', changeset_id: null, by: 'zhangsan', entries: [ref], artifacts: [], paths });
    if (c) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(c) });
    return { commit, version_label };
  }

  // ---------- ST-08 提交 ----------
  on('POST', '/projects/([^/]+)/studio/entries/([^/]+)/submit', async ({ m, body }) => {
    const pid = m[1];
    const e = found(pid, m[2]);
    if (!e) return err(404, 'not_found', '条目不存在');
    const draft = draftOf(e);
    if (!draft) return err(404, 'not_found', '没有可提交的草稿');
    if (draft.state === 'stale') return err(409, 'revision_conflict', '草稿基于旧版本，请刷新后基于最新内容重新编辑', { draft_state: 'stale' });
    const ref = { kind: e.kind, id: e.id, key: e.key, name: e.name };
    const lock = lockOf(pid, ref);
    if (lock) return err(423, 'write_scope_locked', '在助手写入范围内', lock);
    await ctx.wait(250);
    const form = draft.form || formOf(e);
    if (sc.submit === 'fail') {
      const at = (col) => form.fields.findIndex((f) => f.column === col);
      const errors = [{ code: 'E-FIELD-COLUMN', level: 'error', message: '属性「陈列上限」缺少英文名（真实列名）', target: ref, field: `fields[${Math.max(0, at('display_max'))}].column`, hint: '必填' }];
      const warnings = [{ code: 'W-EVID-01', level: 'warning', message: '属性「生效日期」没有证据锚点也没有专家确认', target: ref, field: `fields[${Math.max(0, at('effective_from'))}].evidence` }];
      return json(200, { result: 'rejected', errors, warnings });
    }
    if (sc.submit === 'review') {
      const cid = convsOf(pid)[0].id;
      const msgId = `m-${uid()}`;
      const text = `复核 Studio 提交：${e.name}`;
      emitConv(pid, cid, 'message.user', { message_id: msgId, text, attachments: [], references: [ref], origin: { kind: 'studio', page: 'studio-table', label: 'Studio', task_id: null }, delivery: 'immediate' });
      runReply(pid, cid, text);
      const task = { task_id: `task-${uid()}`, origin: { page: 'studio-table', label: 'Studio' }, title: text, state: 'running', percent: null, step_label: null, conversation_id: cid, message_id: msgId, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null };
      st.drafts.delete(e.id);
      return json(200, {
        result: 'needs_review',
        review_points: ['「区域配额上限」只对乳制品生效，是否要拆成单独规则？', '描述里提到「另有区域配额上限」，和规则 R-12 是否重复？'],
        conversation_id: cid, command_task: task,
      });
    }
    applyForm(e, form);
    if (draft.form == null) e.raw = draft.content;
    st.drafts.delete(e.id);
    const { commit, version_label } = committed(pid, e, [e.path]);
    return json(200, { result: 'committed', entry: entryView(pid, e), revision: e.revision, commit, version_label });
  });

  // ---------- ST-09 新建 ----------
  on('POST', '/projects/([^/]+)/studio/entries', ({ m, body }) => {
    const pid = m[1];
    const name = (body.name || '').trim();
    const kind = body.kind;
    if (!['object', 'operation', 'rule', 'metric', 'flowchart'].includes(kind)) return err(400, 'invalid_argument', '类型不对', { fields: { kind: 'invalid' } });
    if (!name) return err(400, 'invalid_argument', '名称为空', { fields: { name: 'required' } });
    if (kind === 'flowchart' && !diagramOn()) return err(409, 'pack_not_enabled', '图示包没有启用', { pack: 'diagram' });
    const names = [...live().map((x) => x.name), ...st.flowcharts.map((f) => f.title)];
    if (names.includes(name)) return err(409, 'revision_conflict', '同名条目已存在', { kind: 'name', target: name });
    const key = (body.key || '').trim() || `${kind}_${uid()}`;
    if (kind === 'flowchart') {
      const f = { type: 'diagram.flowchart', key, title: name, operation: null, state: 'draft', summary: '' };
      st.flowcharts.push(f);
      committed(pid, { revision: '', kind: 'flowchart', id: `flowchart:${key}`, key, name }, [`ext/diagram/flowcharts/${key}.drawio`]);
      return json(201, { type: f.type, key, title: name, fields: {}, status: { value: 'draft', label: '草稿', tone: 'gray' }, stale: false, locked: false, updated_at: new Date().toISOString(), revision: `r${uid()}`, format: 'drawio', format_version: '1', sections: {}, table: null, content: null, refs: [], stale_reason: null, lock: null, path: `ext/diagram/flowcharts/${key}.drawio` });
    }
    const x = { kind, id: `${kind}:${key}`, key, name, slice: null };
    S.ENTRIES.push(x);
    byId.set(x.id, x);
    byKey.set(key, x);
    const e = stored(x.id);
    e.description = '';
    e.sections = [{ level: 1, title: name, content_md: '' }, ...(kind !== 'object' ? [{ level: 2, title: SECTION_TITLE[kind], content_md: '' }] : [])];
    if (kind === 'object') e.fields = [];
    e.status = 'draft';
    committed(pid, e, [e.path]);
    return json(201, entryView(pid, e));
  });

  // ---------- ST-10 删除 ----------
  on('DELETE', '/projects/([^/]+)/studio/entries/([^/]+)', ({ m }) => {
    const pid = m[1];
    const id = decodeURIComponent(m[2]);
    if (id.startsWith('flowchart:')) {
      const key = id.slice('flowchart:'.length);
      const f = st.flowcharts.find((x) => x.key === key);
      if (!f) return err(404, 'not_found', '条目不存在');
      if (isLocked(scopeOf(pid), { type: f.type, key })) return err(423, 'write_scope_locked', '在助手写入范围内', scopeOf(pid));
      st.flowcharts = st.flowcharts.filter((x) => x !== f);
      return json(200, committed(pid, { revision: '', kind: 'flowchart', id, key, name: f.title }, [`ext/diagram/flowcharts/${key}.drawio`]));
    }
    const e = found(pid, id);
    if (!e) return err(404, 'not_found', '条目不存在');
    const ref = { kind: e.kind, id: e.id, key: e.key, name: e.name };
    if (lockOf(pid, ref)) return err(423, 'write_scope_locked', '在助手写入范围内', scopeOf(pid));
    const refs = e.backlinks.filter((b) => b.source.id);
    if (refs.length) return err(409, 'entry_referenced', '条目被引用', { references: refs.map((b) => b.source) });
    st.deleted.add(e.id);
    return json(200, committed(pid, e, [e.path]));
  });

  // ---------- F-02 / F-03 原文 ----------
  const entryByPath = (pid, path) => (isMain(pid) ? live().map((x) => stored(x.id)).find((e) => e.path === path) : null);
  on('GET', '/projects/([^/]+)/files/content', ({ m, query }) => {
    const pid = m[1];
    const path = query.get('path') || '';
    if (/(^|\/)\.(git|claude|onto|ontology-state)(\/|$)/.test(path) || path.includes('..')) return err(403, 'path_forbidden', '路径越界', { path });
    const e = entryByPath(pid, path);
    if (!e) {
      const f = db.files?.get(path);
      if (!f) return err(404, 'file_not_found', '文件不存在', { path });
      return json(200, { path, content: f.content, truncated: false, size: new TextEncoder().encode(f.content).length, revision: f.revision || 'r1', last_change: f.last_change || null, lock: null });
    }
    // 脚本条目的原文是 Python，由代码生成页的 mock 放进 db.files
    const content = e.raw ?? (e.kind === 'script' ? db.files?.get(path)?.content : null) ?? renderMd(e);
    const big = sc.raw === 'big' && e.key === S.FOCUS_ENTRY;
    const changed = st.rawChanged && e.key === S.FOCUS_ENTRY;
    return json(200, {
      path, content, truncated: big, size: big ? 1258291 : new TextEncoder().encode(content).length, revision: fileRevision(e),
      last_change: changed ? { commit: 'c0ffee13', version_label: 'v0.13', source: 'assistant', at: ago(2 * MIN) } : { commit: cardOf(pid)?.head || 'a71e03c9', version_label: cardOf(pid)?.version_label || 'v0.12', source: 'assistant', at: ago(26 * 60 * MIN) },
      lock: lockOf(pid, { kind: e.kind, id: e.id, key: e.key, name: e.name }),
    });
  });
  on('PUT', '/projects/([^/]+)/files/content', ({ m, body }) => {
    const pid = m[1];
    const e = entryByPath(pid, body.path);
    if (!e) {
      const f = db.files?.get(body.path);
      if (!f) return err(404, 'file_not_found', '文件不存在', { path: body.path });
      if (body.base_revision && f.revision && body.base_revision !== f.revision) return err(409, 'revision_conflict', '版本号不一致', { target: body.path, expected: body.base_revision, actual: f.revision, kind: 'file' });
      Object.assign(f, { content: String(body.content || ''), revision: `r${uid()}` });
      const c = cardOf(pid);
      return json(200, { path: body.path, size: f.content.length, revision: f.revision, commit: c?.head || 'a71e03c9', version_label: c?.version_label || 'v0.12' });
    }
    const ref = { kind: e.kind, id: e.id, key: e.key, name: e.name };
    if (lockOf(pid, ref)) return err(423, 'write_scope_locked', '在助手写入范围内', scopeOf(pid));
    if (body.base_revision !== fileRevision(e)) {
      return err(409, 'revision_conflict', '版本号不一致', { target: body.path, expected: body.base_revision, actual: fileRevision(e), changed_by: 'assistant', changed_at: ago(2 * MIN), version_label: 'v0.13', kind: 'file' });
    }
    e.raw = String(body.content || '');
    st.drafts.delete(e.id);
    st.rawChanged = false;
    const { commit, version_label } = committed(pid, e, [e.path]);
    return json(200, { path: body.path, size: new TextEncoder().encode(e.raw).length, revision: e.revision, commit, version_label });
  });
}
