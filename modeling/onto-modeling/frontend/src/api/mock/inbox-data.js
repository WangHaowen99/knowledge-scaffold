// mock：待处理（IN-01～IN-04）的事项与详情。主示例项目的 18 项对应设计稿 inbox 的 G2Q（合成场景）；
// 其中自主构建复核（需复核、挂起）由运行 #3 的台账实时算出，处理一项计数就少一项。
import { ANCHORS, MAIN_PID, QUESTION_ID, REFS, decisionQuestion } from './fixtures.js';
import { AB_ANCHORS as A, BASE_VERSION, RUN_ID, RUN_NO, THRESHOLD } from './run3.js';
import { entryRef } from './scenario.js';

const MIN = 60 * 1000;
const ago = (ms) => new Date(Date.now() - ms).toISOString();
const todayOrYesterdayAt = (h, m) => {
  const d = new Date();
  d.setHours(h, m, 0, 0);
  if (d.getTime() > Date.now()) d.setDate(d.getDate() - 1);
  return d.toISOString();
};
const yesterday = () => { const d = new Date(Date.now() - 24 * 60 * MIN); d.setHours(17, 40, 0, 0); return d.toISOString(); };

export const KIND_LABEL = {
  decision: '决策', evidence_conflict: '证据冲突', change_list: '变更清单', review: '需复核', suspension: '挂起', stale: '过期',
  resumable: '续跑', feedback: '外部反馈', report: '报告待处理', changeset_conflict: '合入冲突',
  format_migration: '格式待迁移', notification: '通知',
};
const ACTIONS = {
  decision: ['answer'], evidence_conflict: ['arbitrate'], change_list: ['accept_points', 'rewrite_point', 'execute'], review: ['accept', 'rejudge', 'revert_slice'],
  suspension: ['answer', 'use_generic'], stale: ['refresh', 'keep_stale'], resumable: ['resume', 'abandon'], feedback: ['send_to_evolution'],
};
const imp = (...pairs) => pairs.map(([label, text]) => ({ label, items: text.split('、') }));
const item = (id, kind, title, extra) => ({
  id, kind, kind_label: KIND_LABEL[kind], title, meta: '', slice: null, run: null, source: 'assistant', confidence: null, high_confidence: false,
  pack: null, impact: [], created_at: new Date().toISOString(), actions: [...(ACTIONS[kind] || []), 'dismiss'], batchable: true, ...extra,
});
const sl = (id, name) => ({ id, name });
const opt = (key, label, consequence = '', extra = {}) => ({ id: key, key, label, consequence, recommended: false, preview: null, custom: false, ...extra });
const custom = (key) => opt(key, '其他，我来说', '', { custom: true });
const question = (id, title, background_md, anchors, options, extra = {}) => ({
  question_id: id, kind: 'decision', conversation_id: 'c7a1e2f03b44', exec_id: null, step_label: '', eyebrow: '', title, background_md, anchors, options,
  multi: false, allow_custom: options.some((o) => o.custom), custom_placeholder: '说说你的方向…', state: 'open', answer: null, inbox_item_id: `decision:${id}`, affected: [], ...extra,
});

// ---------- 主示例项目：不随运行变化的事项 ----------
function mainStatic() {
  const q1 = decisionQuestion();
  const head = [
    [item(`decision:${QUESTION_ID}`, 'decision', '超量与审批提示：补货标准从哪里来？', {
      slice: sl('s5', '超量与审批提示'), meta: '4 个选项 · 推荐 A', created_at: ago(18 * MIN),
      impact: imp(['操作', '超量与审批提示'], ['对象', '安全库存标准、陈列量标准'], ['场景', '2 个待生成']),
    }), { ...q1, options: q1.options.map((o) => ({ ...o, preview: null })) }],
    [item('evidence_conflict:cf-display', 'evidence_conflict', '陈列量上限：修订通知写 150 箱，ERP 快照是 140', {
      slice: sl('s3', '陈列量核定'), meta: '修订通知 vs ERP 系统', source: 'materials', created_at: ago(26 * MIN),
      impact: imp(['对象', '陈列量标准'], ['操作', '陈列量核定'], ['场景', '场景 3、6、9']),
    }), {
      question: question('cf-display', '陈列量上限以哪边为准？', '', [], [
        opt('A', '以修订通知为准：150 箱', '修订通知是最新的书面规定，资料优先级更高；数据库可能还没同步', { recommended: true }),
        opt('B', '以数据库为准：140', '和业务系统保持一致，修订通知当作待落地'),
        opt('C', '两个都保留，按到货周期区分', '需要确认 140 是否只适用于隔日到货'),
        custom('D'),
      ]),
      sides: [
        { label: '09-修订通知.docx', type_label: '发布日期', date: '10-20', value: '≤150 箱', note: '§2：乳制品区域配额上限，按区域统一配额 ≤150 箱 执行', highlight: '按区域统一配额 ≤150 箱', anchor: A.n2, anchor_label: '09 修订通知 §2' },
        { label: 'ERP 系统 · 补货标准表', type_label: '系统元数据', value: '140.00', note: '「标准上限」字段，牛奶 / 乳制品 / 陈列；10-30 采集', anchor: A.db, anchor_label: 'ERP 补货标准表 · 标准上限' },
      ],
      writes_to: entryRef('display_qty_std'),
    }],
    [item('change_list:cl-09', 'change_list', '09 修订通知：5 个变更点待确认', {
      slice: sl('s3', '陈列量核定 等 3 段'), meta: '影响 6 个产物 · 4 个场景', source: 'materials', created_at: ago(32 * MIN),
      impact: imp(['对象', '陈列量标准、检查项目'], ['操作', '陈列量核定、临期损耗编制'], ['规则', 'R-06、R-11'], ['场景', '4 个']),
    }), changeList()],
  ];
  const tail = [
    [item('stale:scenario-s03', 'stale', '场景 3「门店更严」的预期已过期', {
      slice: sl('s3', '陈列量核定'), meta: '随 09 修订通知连带', pack: 'trace', created_at: ago(32 * MIN), impact: imp(['场景', '场景 3'], ['上游', '陈列量标准 v0.12']),
    }), stale('对象「陈列量标准」· 版本 v0.12 · 来自 09 修订通知', '牛奶 / 乳制品 / 上限 120 箱', '牛奶 / 乳制品 / 上限 150 箱（日均销量 ≥80 箱）',
      '门店申请 96 箱严于标准 120 → 按门店执行', '标准变为 150，结论不变，但预期里的标准值要更新', '需要重新推演一次，约 20 秒', 20000)],
    [item('stale:scenario-s06', 'stale', '场景 6「临时加量」缺少 6.3 新分支', {
      slice: sl('s3', '陈列量核定'), meta: '随 09 修订通知连带', pack: 'trace', created_at: ago(32 * MIN), impact: imp(['场景', '场景 6'], ['上游', 'R-11 临时加量须审批']),
    }), stale('规则「R-11 临时加量须审批」· 版本 v0.12 · 来自 09 修订通知', '临时加量：店长确认后可以下单', '临时加量：必须区域审批，审批通过前不得下单',
      '临时加量 5 箱 → 店长确认后下单', '修订通知新增 6.3，场景缺这条分支', '需要补一条分支并重新推演，约 30 秒', 30000)],
    [item('stale:operation-display', 'stale', '操作「陈列量核定」引用的标准变了', {
      slice: sl('s3', '陈列量核定'), meta: '上游：陈列量标准 v0.12', created_at: ago(32 * MIN), impact: imp(['操作', '陈列量核定']),
    }), stale('对象「陈列量标准」· 版本 v0.12 · 来自 09 修订通知', '牛奶 / 乳制品 / 上限 120 箱', '牛奶 / 乳制品 / 上限 150 箱（日均销量 ≥80 箱）',
      '按品类标准上限 120 箱核定陈列量', '标准变为 150，步骤 2 的引用要更新', '重新抽取步骤 2，约 1 分钟', 60000)],
    [item('resumable:ex-0412', 'resumable', '操作提取在步骤 3/4 中断，可以续跑', {
      slice: sl('s5', '超量与审批提示'), meta: '服务重启时中断', source: 'restart', created_at: ago(62 * MIN), impact: imp(['操作', '超量与审批提示']),
    }), {
      id: 'ex-0412', conversation_id: 'c7a1e2f03b44', engine_instance: 'claude-1', node: { id: 'core.model', version: 'core@1.4.2', title: '建模' },
      slice: sl('s5', '超量与审批提示'), run_id: null, command_label: '操作提取', phase: { index: 3, total: 4, label: '写入即查（步骤 3 / 4）' },
      state: 'interrupted', input_fingerprint: 'f1a2c3', started_at: ago(70 * MIN), heartbeat_at: ago(62 * MIN), ended_at: null,
      interrupted: { at: ago(62 * MIN), reason: 'restart', reason_text: '服务重启，这一步没有正常结束' },
      saved: { files: 2, version_label: BASE_VERSION }, resumable: true, not_resumable_reason: null,
    }],
    [item('feedback:fb-1101-01', 'feedback', '外部反馈：评测服务回传 4 个失分用例', {
      slice: sl('release', '知识包 v0.11'), meta: '交给反馈演进处理', source: 'external', pack: 'evo', created_at: todayOrYesterdayAt(9, 10),
      impact: imp(['操作', '超量与审批提示、陈列量核定'], ['规则', 'R-09']),
    }), {
      feedback_id: 'fb-1101-01', path: 'feedback/20261101T091000Z-fb-1101-01.json', source: '评测服务', release_version: 'v0.11', item_count: 4,
      verdicts: { fail: 3, partial: 1, error: 0 }, received_at: todayOrYesterdayAt(9, 10), received_by: 'svc-eval', commit: 'fb11a0c2',
      items: [
        { question: '牛奶日均销量 60 箱、隔日到货，门店申请 ≤96 箱', expected: '按门店执行', actual: '生成了审批提示', loss_reason: '超量方向判断反了（步骤 3）' },
        { question: '酸奶临期抽检', expected: '保质期剩余 ≤3 天下架', actual: '未给出', loss_reason: '临期损耗标准缺这个商品' },
        { question: '门店指定 2024 版陈列标准', expected: '以门店为准', actual: '用了 2019 版', loss_reason: '缺少 6.3 条' },
        { question: '临时加量 5 箱', expected: '需区域审批', actual: '直接下单', loss_reason: 'R-11 没有挂到操作' },
      ],
    }],
    [item('decision:q-merge-r09', 'decision', '安全库存测算：两条取严规则要不要合并？', {
      slice: sl('s2', '安全库存测算'), meta: '2 个选项', created_at: ago(2 * 60 * MIN), impact: imp(['规则', 'R-07、R-09']),
    }), question('q-merge-r09', '两条取严规则要不要合并？', 'R-07「安全库存取严」和 R-09「门店申请取严」判断条件一样，只是适用的品类不同。', [], [
      opt('A', '合并为 R-09，按品类列出适用范围', '规则少一条；安全库存测算的 3 个场景不受影响', { recommended: true }),
      opt('B', '保持两条', '两条规则的适用范围需要互斥说明'),
    ])],
    [item('evidence_conflict:cf-std', 'evidence_conflict', '牛奶的适用标准：门店清单和品类表不一致', {
      slice: sl('s1', '补货申请与门店需求'), meta: '05-门店清单 vs 06-商品品类表', source: 'materials', created_at: yesterday(),
      impact: imp(['对象', '商品品类、门店特殊需求']),
    }), {
      question: question('cf-std', '牛奶的适用标准以哪边为准？', '', [], [
        opt('A', '以门店清单为准（2024 版）', '门店指定的标准优先，品类表需要更新', { recommended: true }),
        opt('B', '以品类表为准（2019 版）'),
        custom('C'),
      ]),
      sides: [
        { label: '05-门店清单.xlsx', type_label: '资料', value: '2024 版', note: '「A 门店」对牛奶指定的适用标准', anchor: A.stores, anchor_label: '05-门店清单 · 门店表 F7' },
        { label: '06-商品品类表.xlsx', type_label: '资料', value: '2019 版', note: '品类表「适用标准」列，牛奶一行', anchor: A.cats, anchor_label: '06-商品品类表 · 品类表 D12' },
      ],
      writes_to: entryRef('goods_category'),
    }],
  ];
  return { head, tail };
}

function stale(title, del, add, expected_before, now, action, eta_ms) {
  return {
    upstream_diff: { title, lines: [{ kind: 'del', text: del }, { kind: 'add', text: add }] },
    impact: { expected_before, now, action, eta_ms },
    siblings: 3,
  };
}

function changeList() {
  const P = (n, condition, value, anchor, anchor_label, confidence, impact, confidence_note = null) => ({
    id: `cp-${n}`, n, condition, value, scope: '', anchor, anchor_label, confidence, confidence_note, state: 'pending', rewritten_text: null, impact,
  });
  const sc = (k, title) => ({ type: 'trace.scenario', key: k, title });
  return {
    id: 'cl-09', source: { path: 'resource/others/09-修订通知.docx', label: '09 修订通知' },
    points: [
      P(1, '乳制品陈列上限', '120 → 150 箱', A.n2, '09 §2', 'high', [entryRef('display_qty_std'), sc('s-03', '场景 3')]),
      P(2, '适用范围增加「日均销量 ≥80 箱」', '', A.n2, '09 §2', 'high', [entryRef('display_qty_check')]),
      P(3, '原 6.2 条「临时加量」改为必须区域审批', '', A.n4, '09 §4', 'medium', [entryRef('r11_temp_increase'), sc('s-06', '场景 6')]),
      P(4, '临期检查频次由每次到货改为每批', '', A.n5, '09 §5', 'low', [entryRef('check_item')], '修订通知只写了「每批」，没说批的口径'),
      P(5, '新增 6.3：门店指定标准时以门店为准', '', A.n6, '09 §6', 'high', [entryRef('r09_store_stricter'), REFS.op]),
    ],
    state: 'editing', changeset_id: null, impact_summary: { artifacts: 6, scenarios: 4, duration_ms: 1800 }, revision: 'r3',
  };
}

// ---------- 运行 #3 留下的复核事项（随台账变化） ----------
const REVIEW_LOOK = {
  'led-1542': ['临期分级口径：自主构建采用了四级临期分级', imp(['对象', '临期损耗标准'], ['场景', '2 个'])],
  'led-1545': ['检查项目是否包含冷链温度记录：自主构建采用了「包含」', imp(['对象', '检查项目'], ['操作', '临期损耗编制'])],
  'led-1520': ['安全库存：促销备货是否单列一张表，自主构建采用了「不单列」', imp(['对象', '安全库存标准'], ['场景', '1 个'])],
  'led-1626': ['超量：审批提示挂到补货台账，自主构建采用了「按补货单挂」', imp(['对象', '审批提示、补货台账'], ['操作', '超量与审批提示'])],
};
const REVIEW_ORDER = ['led-1542', 'led-1545', 'led-1520', 'led-1626'];
const SUSPENSION_LOOK = {
  'sp-1547': ['修订通知没写批的口径', imp(['规则', '检查频次（未写）'], ['场景', '1 个未通过'])],
  'sp-1624': ['资料 §11.4 提到「区域审批」，但没有对应的对象', imp(['对象', '审批提示'], ['场景', '1 个未通过'])],
  'sp-1521': ['读不出文字，需要补一份文本', imp(['资料', '07-门店C特殊需求.pdf'])],
};
const SUSPENSION_ORDER = ['sp-1547', 'sp-1624', 'sp-1521'];
const SHORT = { s1: '补货申请', s2: '安全库存', s3: '陈列量', s4: '临期', s5: '超量', 'core.consistency': '全局一致性' };
const SLICE_VERSION = { s2: 'v0.14', s3: 'v0.15', s4: 'v0.17', s5: 'v0.19' };

export function runItems(store) {
  const run = (at) => ({ run_id: RUN_ID, run_no: RUN_NO, at });
  const out = [];
  const low = store.pendingLow();
  REVIEW_ORDER.map((id) => low.find((l) => l.id === id)).filter(Boolean).concat(low.filter((l) => !REVIEW_LOOK[l.id])).forEach((l) => {
    const [title, impact] = REVIEW_LOOK[l.id] || [l.summary, imp(['分段', l.slice.name])];
    out.push([item(`review:${l.id}`, 'review', title, {
      slice: l.slice, run: run(l.at), source: 'autobuild', confidence: l.confidence, meta: `置信度 ${l.confidence.toFixed(2)} · 记为假设`,
      created_at: l.at, impact, batchable: false,
    }), { ...l, base_version_label: BASE_VERSION, slice_version_label: SLICE_VERSION[l.slice.id] || null }]);
  });
  const high = store.pendingHigh();
  if (high.length) {
    const slices = [...new Set(high.map((l) => l.slice.id))];
    const short = slices.filter((x) => x !== 'core.consistency').map((x) => SHORT[x]);
    out.push([item(`review:${RUN_ID}:high`, 'review', `高置信的 ${high.length} 个自动决定，可以一起接受`, {
      slice: { id: null, name: `${short.length} 段` }, run: run(null), source: 'autobuild', high_confidence: true, confidence: Math.min(...high.map((l) => l.confidence)),
      meta: `置信度都 ≥${THRESHOLD.toFixed(2)}`, created_at: high[high.length - 1].at, actions: ['accept', 'dismiss'],
      impact: imp(['分段', short.join('、')], ['产物', `${high.length} 处标「需复核」`]),
    }), { group: 'high_confidence', run_id: RUN_ID, threshold: THRESHOLD, entries: high }]);
  }
  const open = store.openSuspensions();
  SUSPENSION_ORDER.map((id) => open.find((x) => x.id === id)).filter(Boolean).forEach((x) => {
    const [meta, impact] = SUSPENSION_LOOK[x.id];
    out.push([item(`suspension:${x.id}`, 'suspension', x.title, {
      slice: x.slice, run: run(x.at), source: 'autobuild', meta, created_at: x.at, impact,
      actions: x.kind === 'unreadable_material' ? ['use_generic', 'dismiss'] : ['answer', 'use_generic', 'dismiss'],
    }), x]);
  });
  return out;
}

// 人工节点（B5c，FL-05 出的决策卡）：source=plugin、pack 为节点所属包、只能选选项（没有「其他，我来说」）
// ?mock=inbox:human 时放在主示例项目最前面
export function humanNodeItem() {
  return [item('decision:hn-signoff', 'decision', '门店签收口径：到货差异由谁确认？', {
    source: 'plugin', pack: 'trace', meta: '3 个选项', batchable: false, actions: ['answer', 'dismiss'], created_at: ago(6 * MIN),
    impact: imp(['操作', '门店签收']),
  }), question('hn-signoff', '到货差异由谁确认？', '人工节点「门店签收」要一个结论，写进签收口径记录。', [], [
    opt('A', '门店店长确认', '差异在门店当场处理'),
    opt('B', '区域督导确认', '差异汇总到区域再处理'),
    opt('C', '两级都要确认', '店长先确认，督导复核'),
  ], { inbox_item_id: 'decision:hn-signoff' })];
}

export function mainItems(store) {
  const { head, tail } = mainStatic();
  return { head, tail, run: () => (store ? runItems(store) : []) };
}

// ---------- 其他项目 ----------
// 外卖骑手排班求解：运行 #2 跑完，留下 2 个需复核、2 个挂起（与项目卡「待处理 4」一致）
export const RIDER_PID = 'rider-shifts-52c9d0f3';
export const ESTATE_PID = 'estate-repair-c4d2b915';
const RIDER_RUN = { run_id: 'run-2', run_no: 2 };
export function riderItems() {
  const end = Date.now() - 60 * MIN;
  const at = (min) => new Date(end - min * MIN).toISOString();
  const led = (id, slice, q, chosen, c, options, reason) => ({
    id, run_id: 'run-2', at: at(40), slice, question: q, chosen, options, confidence: c, threshold: THRESHOLD, assumption: true,
    anchors: [{ n: 1, anchor: 'file:resource/others/02-排班规则.docx#p:3.2', label: '排班规则 §3.2' }], affected: [], review_state: 'pending', rejudged_to: null,
    reason_text: reason, summary: chosen, base_version_label: 'v0.7', slice_version_label: 'v0.9',
  });
  const run = (min) => ({ ...RIDER_RUN, at: at(min) });
  return [
    [item('review:r2-led-01', 'review', '班次模板：午高峰班次按 4 小时切分，自主构建采用了「4 小时」', {
      slice: sl('t3', '班次模板'), run: run(70), source: 'autobuild', confidence: 0.74, meta: '置信度 0.74 · 记为假设', created_at: at(70), batchable: false,
      impact: imp(['对象', '班次模板'], ['场景', '2 个']),
    }), led('r2-led-01', sl('t3', '班次模板'), '午高峰班次按几小时切分', '4 小时', 0.74, [
      opt('A', '按 4 小时切分', '自主构建采用的选项（保守）', { recommended: true }), opt('B', '按 3 小时切分', '改判后 2 个场景过期'),
    ], '低于阈值，按策略采用了保守选项并记为假设，继续往下跑。')],
    [item('review:r2-led-02', 'review', '排班求解：连续上班天数上限，自主构建采用了「6 天」', {
      slice: sl('t5', '排班求解'), run: run(30), source: 'autobuild', confidence: 0.71, meta: '置信度 0.71 · 记为假设', created_at: at(30), batchable: false,
      impact: imp(['规则', '连续上班天数'], ['操作', '排班求解']),
    }), led('r2-led-02', sl('t5', '排班求解'), '连续上班天数上限取几天', '6 天', 0.71, [
      opt('A', '6 天', '自主构建采用的选项（保守）', { recommended: true }), opt('B', '5 天', '求解约束更紧，可能出现无解'),
    ], '低于阈值，按策略采用了保守选项并记为假设，继续往下跑。')],
    [item('suspension:r2-sp-01', 'suspension', '雨天订单系数从哪里取？', {
      slice: sl('t1', '订单预测读取'), run: run(100), source: 'autobuild', meta: '资料里没有雨天系数', created_at: at(100), impact: imp(['对象', '订单预测'], ['场景', '1 个未通过']),
    }), {
      id: 'r2-sp-01', run_id: 'run-2', at: at(100), slice: sl('t1', '订单预测读取'), title: '雨天订单系数从哪里取？', reason: '排班规则提到雨天要加人，但没写订单量按多少系数放大。',
      handled_as: '跳过这一项，先做下一步', kind: 'question', options: [opt('A', '固定 1.3 倍'), opt('B', '按站点历史雨天数据算'), custom('C')], allow_custom: true, actions: [], state: 'open',
    }],
    [item('suspension:r2-sp-02', 'suspension', '站点 C 的骑手可用表只有截图', {
      slice: sl('t4', '骑手可用性'), run: run(80), source: 'autobuild', meta: '读不出文字，需要补一份文本', created_at: at(80), impact: imp(['资料', '站点C可用表.png']),
    }), {
      id: 'r2-sp-02', run_id: 'run-2', at: at(80), slice: sl('t4', '骑手可用性'), title: '站点 C 的骑手可用表只有截图', reason: '站点C可用表.png 是截图，读不出文字；站点 C 只能按通用可用时段排。',
      handled_as: '跳过这一项，先按通用要求排', kind: 'unreadable_material', options: [], allow_custom: false, actions: ['upload_text', 'use_generic'], state: 'open',
    }],
  ];
}
export function riderSummary() {
  const end = Date.now() - 60 * MIN;
  return {
    run_id: 'run-2', run_no: 2, started_at: new Date(end - 112 * MIN).toISOString(), ended_at: new Date(end).toISOString(), state: 'completed',
    cost: { amount: 64, currency: 'CNY' }, decisions: 13, high_confidence: 11, needs_look: 2, suspended: 2, assumptions: 2,
  };
}
export function riderReport() {
  const end = Date.now() - 60 * MIN;
  const s = (slice_id, name, result, result_text, produced_text, passed, total, version_label) => ({ slice_id, name, result, result_text, produced_text, scenarios: { passed, total }, version_label, revertible: result !== 'skipped' });
  return {
    run_id: 'run-2', run_no: 2, state: 'completed', started_at: new Date(end - 112 * MIN).toISOString(), ended_at: new Date(end).toISOString(), recovered_restarts: 0, base_version_label: 'v0.7',
    metrics: {
      duration_ms: 112 * MIN, budget_duration_ms: 120 * MIN, slices: { done: 5, total: 6, partial: [] }, produced: { objects: 6, operations: 4, rules: 8 },
      checks: { errors: 0, warnings: 2 }, scenarios: { passed: 11, total: 12, suspended_failures: 1 }, decisions: { total: 13, high_confidence: 11, needs_look: 2 },
      suspended: 2, cost: { amount: 64, currency: 'CNY' }, tokens: 3800000, budget: { amount: 100, currency: 'CNY' },
    },
    slices: [
      s('t1', '订单预测读取', 'partial', '部分完成 · 挂起 1 项', '+2 对象 +1 操作', 1, 2, 'v0.8'),
      s('t3', '班次模板', 'done', '完成', '+1 对象 +2 规则', 3, 3, 'v0.8'),
      s('t4', '骑手可用性', 'done', '完成 · 挂起 1 项', '+1 对象 +2 规则', 2, 2, 'v0.9'),
      s('t5', '排班求解', 'done', '完成', '+2 对象 +3 操作 +4 规则', 5, 5, 'v0.9'),
      s('t6', '结果校验', 'skipped', '未开始', '—', 0, 0, 'v0.7'),
    ],
    note: '产出已生效并标「需复核」；复核完之前，发布检查会提醒你',
  };
}

// 物业报修工单处理：资料分析阶段，2 个资料用途待确认（与项目卡「待处理 2」一致）
export function estateItems() {
  const purpose = (id, file, created) => [item(`decision:${id}`, 'decision', `资料用途：「${file}」当作哪一类资料？`, {
    slice: null, meta: '3 个选项 · 推荐 A', created_at: ago(created), impact: imp(['资料', file]),
  }), question(id, `「${file}」当作哪一类资料？`, '资料分析没法从文件名和内容判断它的用途，确认后会按这个用途读。', [], [
    opt('A', '业务依据', '抽取对象、操作和规则时引用它', { recommended: true }), opt('B', '建模目标依据', '用来确定建模范围'), opt('C', '数据结构', '只读表头和字段口径'),
  ])];
  return [purpose('q-purpose-01', '维修价目表.xlsx', 3 * 24 * 60 * MIN), purpose('q-purpose-02', '报修记录样例.docx', 3 * 24 * 60 * MIN + 5 * MIN)];
}

export { MAIN_PID, ANCHORS };
