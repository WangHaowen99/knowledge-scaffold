// mock 主示例项目「门店补货单编制」的业务设定（全部合成）。
// F2 各页的 mock 数据都从这里取名称、Key、切片和资料，保证 Studio、报告、待处理、自主构建、资料分析讲的是同一件事。
//
// 业务：店长一句话说明门店、品类、库存和到货周期，智能体查基础数据，按补货规则生成补货单；
// 门店申请量严于（不超过）品类标准时按门店执行，超出标准时生成审批提示，挂到补货台账，交区域审批。
// 数值口径：品类标准上限 120 箱；门店申请 96 箱；09 修订通知把乳制品区域配额上限改为 150 箱（日均销量 ≥80 箱时适用），
// ERP 快照里还是 140；样例补货单 B 按区域统一配额 ≤100 箱。

// ---------- 资料 ----------
// purpose：goal 建模目标依据、evidence 通用业务依据、schema 数据结构
export const MATERIALS = [
  { path: 'resource/others/01-场景说明.docx', name: '01-场景说明.docx', purpose: 'goal' },
  { path: 'resource/others/02-门店补货规则.docx', name: '02-门店补货规则.docx', purpose: 'goal' },
  { path: 'resource/others/03-样例补货单-A门店.docx', name: '03-样例补货单-A门店.docx', purpose: 'evidence' },
  { path: 'resource/others/03-样例补货单-B.docx', name: '03-样例补货单-B.docx', purpose: 'evidence' },
  { path: 'resource/others/04-商品陈列说明.pdf', name: '04-商品陈列说明.pdf', purpose: 'evidence' },
  { path: 'resource/schema/05-门店清单.xlsx', name: '05-门店清单.xlsx', purpose: 'schema' },
  { path: 'resource/schema/06-商品品类表.xlsx', name: '06-商品品类表.xlsx', purpose: 'schema' },
];
// 后来才有的资料：增量更新的来源、挂起问题里的扫描件
export const LATE_MATERIALS = [
  { path: 'resource/others/09-修订通知.docx', name: '09-修订通知.docx', purpose: 'evidence' },
  { path: 'resource/others/07-门店C特殊需求.pdf', name: '07-门店C特殊需求.pdf', purpose: 'evidence', scanned: true },
];

// ---------- 业务系统数据源（证据） ----------
export const DB_SOURCE = { id: 'erp-prod', label: '业务系统 ERP', short: 'ERP', table: 'replenish_std', table_label: '补货标准表' };

// ---------- 切片（与 fixtures.flow() 的 slices 一致） ----------
export const SLICES = [
  { id: 's1', name: '补货申请与门店需求', core_flow: '受理三道校验' },
  { id: 's2', name: '安全库存测算', core_flow: '按类取标准再取严' },
  { id: 's3', name: '陈列量核定', core_flow: '到货周期 × 陈列上限' },
  { id: 's4', name: '临期与损耗检查', core_flow: '临期损耗限值' },
  { id: 's5', name: '超量与审批提示', core_flow: '超量清单与审批提示' },
];

// ---------- 知识包条目 ----------
// kind：object / operation / rule / metric / script；流程图是图示包的产物 diagram.flowchart
const e = (kind, key, name, slice, extra = {}) => ({ kind, id: `${kind}:${key}`, key, name, slice, ...extra });
export const ENTRIES = [
  e('object', 'replenish_order', '补货单', 's1'),
  e('object', 'store', '门店', 's1'),
  e('object', 'store_request', '门店特殊需求', 's1'),
  e('object', 'replenish_ledger', '补货台账', 's1'),
  e('object', 'goods_category', '商品品类', 's2'),
  e('object', 'safety_stock_std', '安全库存标准', 's2'),
  e('object', 'applicable_std', '适用标准', 's2'),
  e('object', 'display_qty_std', '陈列量标准', 's3'),
  e('object', 'delivery_cycle', '到货周期', 's3'),
  e('object', 'expiry_loss_std', '临期损耗标准', 's4'),
  e('object', 'check_item', '检查项目', 's4'),
  e('object', 'overage_list', '超量清单', 's5'),
  e('object', 'approval_hint', '审批提示', 's5'),
  e('object', 'store_level', '门店等级', null, { orphan: true }),
  e('operation', 'replenish_main', '补货编制主流程', null),
  e('operation', 'intake_checks', '受理三道校验', 's1'),
  e('operation', 'safety_stock_calc', '安全库存测算', 's2'),
  e('operation', 'display_qty_check', '陈列量核定', 's3'),
  e('operation', 'expiry_check', '临期损耗编制', 's4'),
  e('operation', 'overage_and_hint', '超量与审批提示', 's5'),
  e('operation', 'render_order', '生成补货单文本', null),
  e('rule', 'r01_intake_scope', 'R-01 受理范围', 's1'),
  e('rule', 'r02_required', 'R-02 必填项', 's1'),
  e('rule', 'r05_by_category', 'R-05 按类取标准', 's2'),
  e('rule', 'r07_display_max', 'R-07 陈列上限', 's3'),
  e('rule', 'r08_expiry_grade', 'R-08 临期分级', 's4'),
  e('rule', 'r09_store_stricter', 'R-09 门店申请取严', 's5'),
  e('rule', 'r11_temp_increase', 'R-11 临时加量须审批', 's5'),
  e('rule', 'r12_region_quota', 'R-12 区域配额≤150箱', 's3'),
  e('metric', 'm01_overage_rate', 'M-01 超量率', 's5'),
  e('metric', 'm02_first_pass', 'M-02 一次通过率', null),
  e('script', 'build_replenish_order', 'build_replenish_order.py', null),
];
export const entry = (key) => ENTRIES.find((x) => x.key === key);
export const entryRef = (key) => {
  const x = entry(key);
  return { kind: x.kind, id: x.id, key: x.key, name: x.name };
};

// 流程图（diagram.flowchart 产物）
export const FLOWCHARTS = [
  { type: 'diagram.flowchart', key: 'overage-flow', title: '超量处理流程', operation: 'overage_and_hint', state: 'draft', summary: '8 个节点、1 个判断' },
  { type: 'diagram.flowchart', key: 'replenish-main-flow', title: '门店补货主流程', operation: 'replenish_main', state: 'confirmed', summary: '10 个节点、2 个判断' },
];

// Studio 设计稿选中的条目：陈列量标准（对应写入范围锁里的条目）
export const FOCUS_ENTRY = 'display_qty_std';
