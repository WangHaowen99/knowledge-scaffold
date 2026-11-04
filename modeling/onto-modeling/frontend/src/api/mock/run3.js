// mock：主示例项目的自主构建运行 #3（设计稿 l4-run 与 inbox 共用的同一次运行，合成场景）。
// 运行范围是未完成的 4 段：安全库存测算、陈列量核定、临期与损耗检查、超量与审批提示；运行前版本 v0.12。
// running：停在 15:48「临期与损耗检查 › 提取规则」；completed：17:02 全部分段完成。
import { ANCHORS, MAIN_PID } from './fixtures.js';
import { entryRef } from './scenario.js';

const MIN = 60 * 1000;
export const todayAt = (h, m) => {
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.toISOString();
};
const hm = (s) => todayAt(...s.split(':').map(Number));
const cny = (amount) => ({ amount, currency: 'CNY' });

export const RUN_ID = 'run-3';
export const RUN_NO = 3;
export const RUN_CONV_ID = 'ab3c0e91f2d4';
export const BASE_VERSION = 'v0.12';
export const THRESHOLD = 0.8;
export const NOW_AT = '15:48';

const SL = {
  s1: { id: 's1', name: '补货申请与门店需求' },
  s2: { id: 's2', name: '安全库存测算' },
  s3: { id: 's3', name: '陈列量核定' },
  s4: { id: 's4', name: '临期与损耗检查' },
  s5: { id: 's5', name: '超量与审批提示' },
  gc: { id: 'core.consistency', name: '全局一致性检查' },
};
export const RUN_SLICES = SL;

// ---------- 出处（合成原文） ----------
const F02 = 'file:resource/others/02-门店补货规则.docx';
const F03A = 'file:resource/others/03-样例补货单-A门店.docx';
const F09 = 'file:resource/others/09-修订通知.docx';
export const AB_ANCHORS = {
  r73: `${F02}#p:7.3`,
  r75: `${F02}#p:7.5`,
  r8: `${F02}#p:8`,
  r81: `${F02}#p:8.1`,
  r92: ANCHORS.e92,
  r114: ANCHORS.e114,
  a5: ANCHORS.ea,
  a6: `${F03A}#p:6`,
  b4: ANCHORS.eb,
  n2: `${F09}#p:2`,
  n4: `${F09}#p:4`,
  n5: `${F09}#p:5`,
  n6: `${F09}#p:6`,
  db: ANCHORS.edb,
  stores: 'file:resource/schema/05-门店清单.xlsx#cell:门店表!F7',
  cats: 'file:resource/schema/06-商品品类表.xlsx#cell:品类表!D12',
};
const A = AB_ANCHORS;

function res(anchor, source_label, location_label, marked) {
  let text = '';
  const highlights = [];
  marked.split(/(<mark>.*?<\/mark>)/).forEach((part) => {
    const m = part.match(/^<mark>(.*)<\/mark>$/);
    if (m) {
      highlights.push({ start: text.length, end: text.length + m[1].length });
      text += m[1];
    } else text += part;
  });
  return { anchor, scheme: anchor.split(':')[0], resolved: true, source_label, location_label, text, highlights, confidence: null, snapshot_id: null, page: 'resources' };
}

// 补进 EV-29 的解析表（index.js 的 anchors/resolve 读同一个对象）
export const AB_RESOLUTIONS = {
  [A.r73]: res(A.r73, '02-门店补货规则.docx', '§7.3 取严', '同一商品在多个品类出现时，<mark>取最严</mark>。'),
  [A.r75]: res(A.r75, '02-门店补货规则.docx', '§7.5 促销备货', '促销备货<mark>可在安全库存表中注明</mark>。'),
  [A.r8]: res(A.r8, '02-门店补货规则.docx', '§8 临期检查', '临期抽检按<mark>「先到先出」</mark>核对，同一到货批次、同一品类算一批。'),
  [A.r81]: res(A.r81, '02-门店补货规则.docx', '§8.1 临期分级', '临期商品按<mark>四级临期分级</mark>评定。'),
  [A.a6]: res(A.a6, '03-样例补货单-A门店.docx', '第 6 节', '检查项目：安全库存、陈列量、临期、<mark>冷链温度记录</mark>。'),
  [A.n2]: res(A.n2, '09-修订通知.docx', '§2', '乳制品区域配额上限，<mark>按区域统一配额 ≤150 箱</mark> 执行（日均销量 ≥80 箱时适用）。'),
  [A.n4]: res(A.n4, '09-修订通知.docx', '§4', '原 6.2 条「临时加量」<mark>改为必须区域审批</mark>。'),
  [A.n5]: res(A.n5, '09-修订通知.docx', '§5', '临期检查频次由每次到货<mark>改为每批</mark>。'),
  [A.n6]: res(A.n6, '09-修订通知.docx', '§6', '新增 6.3：<mark>门店指定标准时以门店为准</mark>。'),
  [A.stores]: res(A.stores, '05-门店清单.xlsx', '门店表 F7', '「A 门店」对牛奶指定的适用标准：<mark>乳制品陈列标准 2024 版</mark>'),
  [A.cats]: res(A.cats, '06-商品品类表.xlsx', '品类表 D12', '牛奶一行，「适用标准」列：<mark>乳制品陈列标准 2019 版</mark>'),
};

// ---------- 决策台账（AB-10） ----------
const opt = (key, label, consequence = '', extra = {}) => ({ id: key, key, label, consequence, recommended: false, preview: null, custom: false, ...extra });
const adopted = (key, label, consequence) => opt(key, label, consequence, { recommended: true });
const L = (id, at, slice, question, chosen, confidence, anchor, label, summary, options, extra = {}) => ({
  id, run_id: RUN_ID, at: hm(at), slice: SL[slice], question, chosen, options,
  confidence, threshold: THRESHOLD, assumption: confidence < THRESHOLD,
  anchors: [{ n: 1, anchor, label }], affected: [], review_state: 'pending', rejudged_to: null,
  reason_text: confidence < THRESHOLD ? '低于阈值，按策略采用了保守选项并记为假设，继续往下跑。' : '达到阈值，按推荐选项采用。',
  summary, ...extra,
});

export function ledger() {
  return [
    L('led-1515', '15:15', 's2', '两条取严规则判断条件一样，只是适用的品类不同，要不要合并', '合并为 R-09，按品类列出适用范围', 0.91, A.r73, '补货规则 §7.3', '安全库存：两条取严规则合并为 R-09',
      [adopted('A', '合并为 R-09，按品类列出适用范围', '规则少一条；安全库存测算的 3 个场景不受影响'), opt('B', '保持两条，分别写适用范围', '两条规则的适用范围需要互斥说明')]),
    L('led-1518', '15:18', 's2', '促销备货的安全库存按什么取', '按门店申请取严', 0.88, A.a5, '样例补货单 A §5', '安全库存：促销备货按门店申请取严',
      [adopted('A', '按门店申请取严', '和样例补货单 A 的做法一致'), opt('B', '按品类标准取', '门店申请更严时不采用')]),
    L('led-1520', '15:20', 's2', '促销备货是否单列一张表', '不单列，在安全库存标准里注明', 0.74, A.r75, '补货规则 §7.5', '安全库存：促销备货不单列',
      [adopted('A', '不单列，在安全库存标准里注明', '自主构建采用的选项（保守）'), opt('B', '单列为对象「促销备货标准」', '新增 1 个对象，安全库存测算的 2 个场景要重做')],
      { reason_text: '低于阈值，采用了不新增对象的选项并记为假设。', affected: [entryRef('safety_stock_std')] }),
    L('led-1522', '15:22', 's2', '样例补货单 B 的酸奶范围按什么取', '按区域统一配额', 0.84, A.b4, '样例补货单 B §4', '安全库存：样例补货单 B 的酸奶范围按区域统一配额',
      [adopted('A', '按区域统一配额', '和样例补货单 B 一致'), opt('B', '按品类标准', '样例补货单 B 的场景要改预期')]),
    L('led-1529', '15:29', 's3', '乳制品陈列上限：修订通知写 150 箱，ERP 快照是 140', '150（修订通知优先）', 0.86, A.n2, '09 修订通知 §2', '陈列量：乳制品上限取 150（修订通知优先）',
      [adopted('A', '150（修订通知优先）', '资料优先级里修订通知排第一'), opt('B', '140（以 ERP 为准）', '修订通知当作待落地')]),
    L('led-1531', '15:31', 's3', '「日均销量 ≥80 箱」是不是新上限的适用条件', '是，作为新上限的适用条件', 0.93, A.n2, '09 修订通知 §2', '陈列量：「日均销量 ≥80 箱」作为新上限的适用条件',
      [adopted('A', '是，作为新上限的适用条件', ''), opt('B', '不是，新上限对所有门店适用', '')]),
    L('led-1533', '15:33', 's3', '场景 3 的预期标准值要不要更新', '更新为 150', 0.95, A.n2, '09 修订通知 §2', '陈列量：场景 3 的预期标准值更新为 150',
      [adopted('A', '更新为 150', ''), opt('B', '保持 120', '场景 3 会一直过期')]),
    L('led-1542', '15:42', 's4', '临期商品的分级口径有两种说法：补货规则写按四级临期分级，样例补货单 A 用区域三级分级', '按四级临期分级（保守选项）', 0.72, A.r81, '补货规则 §8.1', '临期：分级口径按四级临期分级',
      [adopted('A', '按四级临期分级', '自主构建采用的选项（保守）'), opt('B', '按区域三级分级', '改判后 2 个场景过期，助手重做约 3 分钟'), opt('C', '按门店指定', '需要门店特殊需求里有分级口径')],
      { affected: [entryRef('expiry_loss_std')] }),
    L('led-1545', '15:45', 's4', '检查项目是否包含冷链温度记录', '包含', 0.78, A.a6, '样例补货单 A §6', '临期：检查项目包含冷链温度记录',
      [opt('A', '不包含', '只按规则列出的四项'), adopted('B', '包含冷链温度记录', '自主构建采用的选项')],
      { reason_text: '低于阈值，采用了覆盖面更大的选项并记为假设。', affected: [entryRef('check_item'), entryRef('expiry_check')] }),
    L('led-1550', '15:50', 's4', '临期抽检按什么核对', '按「先到先出」核对', 0.83, A.r8, '补货规则 §8', '临期：临期抽检按「先到先出」核对',
      [adopted('A', '按「先到先出」核对', ''), opt('B', '按保质期剩余天数核对', '')]),
    L('led-1612', '16:12', 's5', '超量比较用的标准上限从哪里取', '按适用标准逐类取', 0.88, A.r92, '补货规则 §9.2', '超量：标准上限按适用标准逐类取',
      [adopted('A', '按适用标准逐类取', ''), opt('B', '一律用区域统一配额', '')]),
    L('led-1620', '16:20', 's5', '同一商品在多个品类出现时取哪个', '取最严', 0.9, A.r92, '补货规则 §9.2', '超量：同一商品多类出现时取最严',
      [adopted('A', '取最严', ''), opt('B', '取第一个出现的品类', '')]),
    L('led-1626', '16:26', 's5', '审批提示挂到哪里', '按补货单挂到补货台账（保守）', 0.69, A.r114, '补货规则 §11.4', '超量：审批提示按补货单挂到补货台账',
      [adopted('A', '按补货单挂到补货台账', '自主构建采用的选项（保守）'), opt('B', '挂到新对象「区域审批单」', '新增 1 个对象；和挂起的「审批提示交给谁」一起定')],
      { reason_text: '低于阈值，采用了不新增对象的选项并记为假设。', affected: [entryRef('approval_hint'), entryRef('replenish_ledger')] }),
    L('led-1631', '16:31', 's5', '超出标准的项怎么处理', '一律生成审批提示', 0.92, A.r114, '补货规则 §11.4', '超量：超出标准一律生成审批提示',
      [adopted('A', '一律生成审批提示', ''), opt('B', '超出 10% 以内不提示', '')]),
    L('led-1640', '16:40', 'gc', '「要货方」和「门店」是不是同一个说法', '统一叫「门店」', 0.97, A.r92, '补货规则 §9.2', '全局一致性：「要货方」统一叫「门店」',
      [adopted('A', '统一叫「门店」', ''), opt('B', '保留两种叫法', '')]),
    L('led-1644', '16:44', 'gc', '商品品类的关系是推断的还是声明的', '改为声明', 0.85, A.db, 'ERP 补货标准表', '全局一致性：商品品类的关系改为声明',
      [adopted('A', '改为声明', ''), opt('B', '保持推断', '')]),
    L('led-1652', '16:52', 'gc', 'R-07 缺适用品类', '补上适用品类', 0.81, A.r73, '补货规则 §7.3', '全局一致性：R-07 补上适用品类',
      [adopted('A', '补上适用品类', ''), opt('B', '先不补', '')]),
  ];
}

// ---------- 挂起的问题（AB-14） ----------
const S = (id, at, slice, title, reason, extra) => ({
  id, run_id: RUN_ID, at: hm(at), slice: SL[slice], title, reason, handled_as: '跳过这一项，先做下一步',
  kind: 'question', options: [], allow_custom: true, actions: [], state: 'open', ...extra,
});
export function suspensions() {
  return [
    S('sp-1521', '15:21', 's1', '门店 C 的特殊需求只有扫描件', '07-门店C特殊需求.pdf 是扫描件，读不出文字；门店 C 的补货单只能按通用要求编。', {
      kind: 'unreadable_material', allow_custom: false, actions: ['upload_text', 'use_generic'], handled_as: '跳过这一项，先按通用要求编',
      material: { path: 'resource/others/07-门店C特殊需求.pdf', name: '07-门店C特殊需求.pdf' },
    }),
    S('sp-1547', '15:47', 's4', '临期的「每批」怎么算？', '修订通知只写了检查频次「每批」，没说批按什么划分。这条规则先没写，相关的 1 个场景没通过。', {
      options: [opt('A', '同一到货批次、同一品类算一批', '补货规则 §8 的检查批常用口径'), opt('B', '同一到货批次算一批'), opt('C', '其他，我来说', '', { custom: true })],
    }),
    S('sp-1624', '16:24', 's5', '审批提示交给谁？', '补货规则 §11.4 要求「提交区域审批」，但资料里没有审批由谁做、结果记在哪。', {
      options: [opt('A', '新增对象「区域审批单」，审批提示挂在上面'), opt('B', '交给补货台账，由店长跟进'), opt('C', '其他，我来说', '', { custom: true })],
    }),
  ];
}

// ---------- 运行（AB-05）与报告（AB-16） ----------
export function policy() {
  return {
    scope: { mode: 'all_pending', slice_ids: ['s2', 's3', 's4', 's5'], refresh_stale_first: true, generate_scenarios: true, include_optional: false, until_node: null },
    production_collection: [{ source_id: 'erp-prod', allowed: false, window: null, scope: [] }],
    advanced: { confidence_threshold: THRESHOLD, below_threshold: 'conservative_assumption', must_suspend_kinds: [], source_priority: ['09 修订通知', '补货规则', '样例补货单', '数据库'], max_fix_rounds: 2 },
    boundary_text: '自主构建不会发布、不会删除已确认内容、不会改资料区、不会在未授权时采集生产库',
  };
}

const ROW = (slice, cells, result) => ({ slice_id: slice.id, slice_name: slice.name, cells: Object.fromEntries(['object', 'operation', 'rule', 'check', 'scenario'].map((k, i) => [k, cells[i]])), result });
const C = 'confirmed';
const AU = 'auto';
const Q = 'queued';

// state：running | pausing | paused | stopping | converting | completed | stopped | converted（budget_exhausted 只在老运行里）
export const ENDED = new Set(['completed', 'stopped', 'converted', 'budget_exhausted', 'failed']);
// 这个状态下运行已经走到哪个时刻（台账、挂起只看这之前的）
export function cutoffOf(state) {
  if (state === 'completed') return null;
  return hm(ENDED.has(state) ? '15:56' : NOW_AT);
}
export const upTo = (state) => (x) => { const c = cutoffOf(state); return !c || x.at <= c; };

export function runOf(state = 'running', { led = ledger().filter(upTo(state)), sus = suspensions().filter(upTo(state)), acknowledged = false } = {}) {
  const ended = ENDED.has(state);
  const done = state === 'completed';
  const matrix = [
    ROW(SL.s1, [C, C, 'skipped', C, C], '运行前已完成'),
    ROW(SL.s2, [C, C, AU, AU, AU], '完成 · 1 项需复核'),
    ROW(SL.s3, [C, AU, AU, AU, AU], '已刷新过期 3 项'),
    done ? ROW(SL.s4, [AU, AU, AU, AU, AU], '部分完成 · 挂起 2 项')
      : ended ? ROW(SL.s4, [AU, AU, AU, AU, AU], '部分完成 · 挂起 1 项')
        : ROW(SL.s4, [AU, AU, 'running', Q, Q], '进行中'),
    done ? ROW(SL.s5, [AU, AU, AU, AU, AU], '完成 · 挂起 1 项') : ROW(SL.s5, [Q, Q, Q, Q, Q], ended ? '未开始' : '排队'),
  ];
  return {
    run_id: RUN_ID,
    run_no: RUN_NO,
    state,
    current: ended ? null : { slice_id: 's4', slice_name: SL.s4.name, step_label: '提取规则' },
    elapsed_ms: done ? 112 * MIN : ended ? 46 * MIN : 38 * MIN,
    est_total_ms: ended ? null : 84 * MIN,
    tokens: { used: done ? 5100000 : 2100000 },
    cost: { used: cny(done ? 86 : 31) },
    slices: { done: done ? 4 : ended ? 3 : 2, total: 4 },
    decisions: led.length,
    suspended: sus.length,
    needs_review: led.length,
    started_at: hm('15:10'),
    ended_at: done ? hm('17:02') : ended ? hm('15:56') : null,
    acknowledged: ended && acknowledged,
    pause_reason: null,
    conversation_id: RUN_CONV_ID,
    policy: policy(),
    checks: { errors: 0, warnings: 3 },
    scenarios: done ? { passed: 16, total: 18 } : { passed: 14, total: 15 },
    fix_rounds: { used: 1, limit: 2 },
    matrix,
    tail_text: '跑完全部分段后：全局一致性检查 → 合规检查（发布前，留给你）',
    scope_lock_text: '正在做的这一段，写入范围内的条目在 Studio 和插件页里只读，到这一段自动合入为止；其他条目照常编辑，保存即生效',
    recovered_from_checkpoint: done ? 1 : 0,
    report_available: ended,
    base_version_label: BASE_VERSION,
  };
}

export function reportOf(state = 'completed') {
  const done = state === 'completed';
  const slice = (s, result, result_text, produced_text, passed, total, version_label) => ({
    slice_id: s.id, name: s.name, result, result_text, produced_text, scenarios: { passed, total }, version_label, revertible: result !== 'skipped',
  });
  return {
    run_id: RUN_ID,
    run_no: RUN_NO,
    state,
    started_at: hm('15:10'),
    ended_at: done ? hm('17:02') : hm('15:56'),
    recovered_restarts: done ? 1 : 0,
    base_version_label: BASE_VERSION,
    metrics: {
      duration_ms: done ? 112 * MIN : 46 * MIN,
      slices: { done: done ? 5 : 4, total: 5, partial: [SL.s4.name] },
      produced: done ? { objects: 5, operations: 2, rules: 7 } : { objects: 3, operations: 1, rules: 4 },
      checks: { errors: 0, warnings: 3 },
      scenarios: done ? { passed: 16, total: 18, suspended_failures: 2 } : { passed: 13, total: 14, suspended_failures: 1 },
      decisions: done ? { total: 17, high_confidence: 13, needs_look: 4 } : { total: 10, high_confidence: 7, needs_look: 3 },
      suspended: done ? 3 : 2,
      cost: cny(done ? 86 : 35),
      tokens: done ? 5100000 : 2400000,
    },
    slices: [
      slice(SL.s2, 'done', '完成', '+1 规则', 4, 4, 'v0.14'),
      slice(SL.s3, 'done', '完成 · 刷新过期 3 项', '改 2 操作 · +1 规则', 6, 6, 'v0.15'),
      slice(SL.s4, 'partial', done ? '部分完成 · 挂起 2 项' : '部分完成 · 挂起 1 项', '+3 对象 +1 操作 +3 规则', 3, 4, 'v0.17'),
      done ? slice(SL.s5, 'done', '完成', '+2 对象 +1 操作 +2 规则', 3, 4, 'v0.19') : slice(SL.s5, 'skipped', '未开始', '—', 0, 0, BASE_VERSION),
    ],
    note: '产出已生效并标「需复核」；复核完之前，发布检查会提醒你',
  };
}

// 写入范围锁：正在做的这一段（autobuild_slice）
export function sliceLock(conversation_id = RUN_CONV_ID) {
  return {
    state: 'autobuild_slice', changeset_id: 'cs-ab3-s4', conversation_id, run_id: RUN_ID, slice: SL.s4, node: { id: 'core.model', version: 'core@1.4.2' },
    entries: ['expiry_loss_std', 'check_item', 'expiry_check', 'r08_expiry_grade'].map(entryRef), artifacts: [], paths: [],
    reason_text: '自主构建正在做「临期与损耗检查」这一段', since: hm('15:36'),
  };
}

// ---------- 对话卡片（契约 4.7） ----------
const decisionCard = (l) => ({
  type: 'auto_decision',
  card: {
    card_id: `ab3-ad-${l.id}`, card_type: 'auto_decision', eyebrow: '', title: l.question, subtitle: '', status: null, plugin: null, open: { kind: 'page', target: 'inbox' },
    run_id: RUN_ID, ledger_id: l.id, at: l.at, question: l.question, chosen: l.chosen, anchors: l.anchors,
    confidence: l.confidence, threshold: l.threshold, assumption: l.assumption, review_state: l.review_state, recorded: true,
  },
});
const suspensionCard = (s, title, reason) => ({
  type: 'suspension',
  card: { card_id: `ab3-sp-${s.id}`, card_type: 'suspension', eyebrow: '', title, subtitle: '', status: null, plugin: null, open: { kind: 'page', target: 'inbox' }, run_id: RUN_ID, suspension_id: s.id, reason, inbox_item_id: `suspension:${s.id}` },
});
const step = (id, text, meta, state = 'done') => ({ id, text, meta, state, duration_ms: null });

export function sliceCard(sid, state) {
  const led = Object.fromEntries(ledger().map((l) => [l.id, l]));
  const sus = Object.fromEntries(suspensions().map((s) => [s.id, s]));
  const base = (slice, extra) => ({
    card_id: `ab3-${slice.id}`, card_type: 'slice_progress', eyebrow: '', title: slice.name, subtitle: '', status: null, plugin: null,
    open: null, run_id: RUN_ID, slice, decision_ids: [], suspension_ids: [], timeline: [], version_label: null, ...extra,
  });
  if (sid === 's2') {
    return base(SL.s2, {
      state: 'done', summary: '自主完成 · +1 规则 · 场景 4/4 · 自动决定 4 项', version_label: 'v0.14',
      decision_ids: ['led-1515', 'led-1518', 'led-1520', 'led-1522'], suspension_ids: ['sp-1521'],
      timeline: [step('a', '补齐 2 条规则，写入即查 0 个错误', '15:10 – 15:24'), decisionCard(led['led-1515']), decisionCard(led['led-1520'])],
    });
  }
  if (sid === 's3') {
    return base(SL.s3, {
      state: 'done', summary: '先刷新 09 修订通知带来的 3 项过期 · 改 2 个操作 · +1 规则 · 场景 6/6', version_label: 'v0.15',
      decision_ids: ['led-1529', 'led-1531', 'led-1533'],
      timeline: [
        step('a', '刷新过期的 3 项：陈列量标准、操作「陈列量核定」、场景 3', '15:24 – 15:31'),
        step('b', '补规则「日均销量 ≥80 箱适用新上限」，场景 6/6 通过', '15:36'),
        decisionCard(led['led-1529']),
      ],
    });
  }
  if (sid === 's4') {
    const running = state === 'running' || state === 'paused';
    return base(SL.s4, {
      state: state === 'paused' ? 'running' : state,
      summary: running ? '正在做 · 规则 2 / 3' : '部分完成 · 挂起 2 项 · +3 对象 +1 操作 +3 规则 · 场景 3/4',
      version_label: running ? null : 'v0.17',
      decision_ids: running ? ['led-1542', 'led-1545'] : ['led-1542', 'led-1545', 'led-1550'],
      suspension_ids: ['sp-1547'],
      timeline: [
        step('a', '读完补货规则第 8 章和样例补货单 A 第 6 节', '15:36 · 覆盖 100%'),
        step('b', '抽出对象「临期损耗标准」「检查项目」，操作「临期损耗编制」，步骤都带出处', '15:40'),
        step('c', '写入即查：0 个错误，1 个提醒（分级口径来源单一）', '15:41'),
        decisionCard(led['led-1542']),
        decisionCard(led['led-1545']),
        step('d', '从样例补货单 A 生成 2 个验收场景，推演 2/2 通过', '15:46 · 保存版本 v0.16'),
        suspensionCard(sus['sp-1547'], '临期的「每批」怎么算', '09 修订通知只写了检查频次「每批」，没说批的口径。这条规则先不写，等你回答后补上。'),
        state === 'paused' ? step('e', '提取临期与损耗检查的规则 · 第 2 / 3 条', '15:48 · 已暂停，续跑后接着做', 'interrupted')
          : running ? step('e', '提取临期与损耗检查的规则 · 第 2 / 3 条', '15:48 · 进行中', 'running')
            : step('e', '提取临期与损耗检查的规则 · 写了 2 条，第 3 条因挂起没写', '15:48 – 15:55'),
      ],
    });
  }
  if (sid === 's5') {
    if (state === 'queued') return base(SL.s5, { state: 'queued', summary: '排队中 · 下一段' });
    if (state === 'skipped') return base(SL.s5, { state: 'queued', summary: '没有开始 · 运行已停下' });
    return base(SL.s5, {
      state: 'done', summary: '自主完成 · +2 对象 +1 操作 +2 规则 · 场景 3/4', version_label: 'v0.19',
      decision_ids: ['led-1612', 'led-1620', 'led-1626', 'led-1631'], suspension_ids: ['sp-1624'],
      timeline: [
        step('a', '抽出对象「超量清单」「审批提示」，操作「超量与审批提示」', '16:02 – 16:18'),
        suspensionCard(sus['sp-1624'], '审批提示交给谁', '补货规则 §11.4 要求提交区域审批，但资料里没有审批由谁做。先按补货单挂到补货台账。'),
        decisionCard(led['led-1626']),
        step('b', '生成 4 个验收场景，推演 3/4 通过，1 个未通过已挂起', '16:36'),
      ],
    });
  }
  return base(SL.gc, {
    state: 'done', summary: '0 个错误 · 3 个提醒 · 修复 1 轮', decision_ids: ['led-1640', 'led-1644', 'led-1652'],
    timeline: [step('a', '全局一致性检查：0 个错误，3 个提醒', '16:38 – 16:58'), step('b', '修复 1 轮：统一「门店」叫法、补上 R-07 适用品类', '16:58')],
  });
}

export function startCard() {
  return {
    card_id: 'ab3-start', card_type: 'autobuild_start', eyebrow: '', title: '自主构建 · 运行 #3', subtitle: '', status: null, plugin: null,
    open: { kind: 'page', target: 'proj-settings' }, run_id: RUN_ID, run_no: RUN_NO,
    chips: [{ label: '范围', value: '未完成的 4 段' }, { label: '生产库', value: '不采集' }, { label: '拿不准时', value: '先按最稳妥的做法往下做，做完列出来给你复核' }],
  };
}

export function reportCard(state = 'completed') {
  const r = reportOf(state);
  return {
    card_id: 'ab3-report', card_type: 'run_report', eyebrow: '', title: `运行 #${RUN_NO} 报告`, subtitle: '', status: null, plugin: null,
    open: { kind: 'page', target: 'inbox', params: { filter: 'autobuild_review' } },
    ...r,
    review_count: r.metrics.decisions.total,
  };
}

export function suggestions(state) {
  const details = { id: 'sg-ab-details', label: '运行详情', icon: 'list', primary: false, disabled: false, reason: '', priority: 2, action: { type: 'autobuild', op: 'details' } };
  const ended = ['completed', 'stopped', 'converted'].includes(state);
  const n = state === 'completed' ? 17 : 10;
  const second = ended
    ? { id: 'sg-ab-review', label: `去复核 ${n} 项`, icon: 'inbox', primary: false, disabled: false, reason: '', priority: 2, action: { type: 'navigate', page: 'inbox', params: { filter: 'autobuild_review' } } }
    : { id: 'sg-ab-suspended', label: '挂起的问题 2', icon: 'pause', primary: false, disabled: false, reason: '', priority: 2, action: { type: 'navigate', page: 'inbox', params: { filter: 'autobuild_review' } } };
  return { mode: 'autobuild_controls', primary: null, secondary: [details, second], all_commands: true };
}

// 运行对话的历史（S-06）：running 停在 15:48；completed 在后面接上运行结束、报告卡
export function history(state = 'running') {
  let seq = 3000;
  const e = (type, data, at) => ({ id: String(++seq), type, ts: hm(at), project_id: MAIN_PID, conversation_id: RUN_CONV_ID, data });
  const done = state === 'completed';
  const out = [
    e('day.marker', { label: '今天' }, '15:08'),
    e('notice', { kind: 'merged', text: '15:08 · 分段「补货申请与门店需求」已存成版本 v0.12', version_label: BASE_VERSION, changeset_id: 'cs-29', align: 'center' }, '15:08'),
    e('message.user', { message_id: 'm-u-ab3', text: '剩下的你自己做完', attachments: [], references: [], origin: null, delivery: 'immediate' }, '15:09'),
    e('message.assistant', { message_id: 'm-a-ab3', text_md: '好，转为自主构建，按「项目设置 › 构建方式」里的策略跑：', anchors: [], entry_links: [], continued: false }, '15:10'),
    e('card', startCard(), '15:10'),
    e('card', sliceCard('s2'), '15:10'),
    e('card', sliceCard('s3'), '15:24'),
    e('card', sliceCard('s4', done ? 'partial' : 'running'), '15:36'),
    e('card', sliceCard('s5', done ? 'done' : 'queued'), '15:48'),
  ];
  if (done) {
    out.push(
      e('card', sliceCard('gc'), '16:38'),
      e('notice', { kind: 'info', text: '17:02 · 运行 #3 结束：全部分段完成', version_label: 'v0.19', changeset_id: null, align: 'center' }, '17:02'),
      e('message.assistant', { message_id: 'm-a-ab3-end', text_md: '5 段都跑完了。高置信的 13 个决定可以一键接受，另外 4 项低于阈值、记为假设，3 个问题挂起等你回答。', anchors: [], entry_links: [], continued: false }, '17:02'),
      e('card', reportCard('completed'), '17:02'),
    );
  }
  out.push(e('suggestions.updated', suggestions(state), done ? '17:02' : '15:48'));
  return out;
}

export function runConversation(state = 'running') {
  return {
    id: RUN_CONV_ID, title: '自主构建 · 运行 #3', title_manual: false, created_at: hm('15:09'), updated_at: new Date(Date.now() - MIN).toISOString(),
    live: true, state: state === 'running' ? 'running' : 'idle', kind: 'autobuild', run_id: RUN_ID,
  };
}

// ---------- 一次运行的可变状态：台账复核、挂起回答、按段撤回（AB-11～AB-17 与待处理共用） ----------
export function createRunStore(initial) {
  const s = { state: initial, ledger: ledger(), suspensions: suspensions(), reverted: new Map(), version: 19, acknowledged: false };
  const visibleLedger = () => s.ledger.filter(upTo(s.state)).filter((l) => !s.reverted.has(l.slice.id));
  const visibleSuspensions = () => s.suspensions.filter(upTo(s.state)).filter((x) => !s.reverted.has(x.slice.id));
  const store = {
    get state() { return s.state; },
    set state(v) { s.state = v; },
    get acknowledged() { return s.acknowledged; },
    set acknowledged(v) { s.acknowledged = v; },
    ledger: visibleLedger,
    suspensions: visibleSuspensions,
    run() {
      const r = runOf(s.state, { led: visibleLedger(), sus: visibleSuspensions(), acknowledged: s.acknowledged });
      r.needs_review = visibleLedger().filter((l) => l.review_state === 'pending').length;
      r.suspended = visibleSuspensions().filter((x) => x.state === 'open').length;
      return r;
    },
    report() {
      const r = reportOf(s.state === 'completed' ? 'completed' : 'stopped');
      r.slices = r.slices.map((x) => (s.reverted.has(x.slice_id)
        ? { ...x, result_text: `已回退到运行前 · ${s.reverted.get(x.slice_id)}`, revertible: false, reverted: true }
        : x));
      return r;
    },
    entry: (id) => s.ledger.find((l) => l.id === id),
    suspension: (id) => s.suspensions.find((x) => x.id === id),
    pendingHigh: () => visibleLedger().filter((l) => l.review_state === 'pending' && l.confidence >= THRESHOLD),
    pendingLow: () => visibleLedger().filter((l) => l.review_state === 'pending' && l.confidence < THRESHOLD),
    openSuspensions: () => visibleSuspensions().filter((x) => x.state === 'open'),
    accept(id) {
      const l = store.entry(id);
      if (l) l.review_state = 'accepted';
      return l;
    },
    rejudge(id, { option_id, custom_text } = {}) {
      const l = store.entry(id);
      if (!l) return null;
      const o = l.options.find((x) => x.id === option_id);
      Object.assign(l, { review_state: 'rejudged', rejudged_to: custom_text || o?.label || null });
      return l;
    },
    acceptHigh() {
      const list = store.pendingHigh();
      list.forEach((l) => { l.review_state = 'accepted'; });
      return list.length;
    },
    answer(id, body) {
      const x = store.suspension(id);
      if (x) x.state = body?.action === 'defer_to_next_run' ? 'deferred' : 'answered';
      return x;
    },
    revert(sliceId) {
      s.version += 1;
      s.reverted.set(sliceId, BASE_VERSION);
      return { commit: `ab3r${s.version}`, version_label: `v0.${s.version}`, reverted_to_version_label: BASE_VERSION };
    },
    isReverted: (sliceId) => s.reverted.has(sliceId),
  };
  return store;
}
