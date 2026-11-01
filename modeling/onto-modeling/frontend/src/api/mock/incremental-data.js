// mock：增量更新（设计稿 incremental）那条对话「09 修订通知」的数据，合成场景。
// 变更清单就是待处理里的 cl-09（inbox-data.js，只读）：条数、把握、出处、接受状态都以它为准；
// 这里只补「条件 / 取值 / 范围」三列（inbox 的数据把整句写在 condition 里）和清单所在的对话。
import { REFS } from './fixtures.js';
import { entryRef } from './scenario.js';

const MIN = 60 * 1000;
export const CL_ID = 'cl-09';
export const INC_CID = '0e9c4d7a21b5';
export const FEEDBACK_ID = 'fb-1101-01';
export const SOURCE_PATH = 'resource/others/09-修订通知.docx';
export const CARD = {
  list: 'card-inc-list', impact: 'card-inc-impact', feedback: 'card-inc-feedback', regression: 'card-inc-regression', round: 'card-inc-round',
};

const two = (n) => String(n).padStart(2, '0');
const stamp = (iso) => {
  const d = new Date(iso);
  return `${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
};
const ago = (min) => new Date(Date.now() - min * MIN).toISOString();

export function incConversation(live) {
  return {
    id: INC_CID, title: '09 修订通知', title_manual: false, created_at: ago(40), updated_at: ago(29),
    live, state: 'idle', kind: 'chat', run_id: null,
  };
}

// ---------- 变更点：inbox 的 cl-09 补三列 ----------
const FIELDS = {
  'cp-1': { condition: '乳制品 · 陈列上限', value: '≤150 箱（原 ≤120 箱）', scope: '陈列量核定' },
  'cp-2': { condition: '日均销量 ≥80 箱的门店', value: '新上限只对这类门店适用', scope: '陈列量核定' },
  'cp-3': { condition: '临时加量（原 6.2 条）', value: '改为必须区域审批，审批通过前不得下单', scope: '超量与审批提示' },
  'cp-4': { condition: '临期检查频次', value: '每次到货 → 每批', scope: '临期与损耗检查' },
  'cp-5': { condition: '门店指定了适用标准', value: '新增 6.3：以门店指定的为准', scope: '补货申请与门店需求' },
};

// INC-01 的 ChangeList：inbox 那份原样，没改过的条目补上三列；带上清单所在的对话（契约改动见报告）
export function overlay(detail) {
  return {
    ...detail,
    conversation_id: INC_CID,
    points: detail.points.map((p) => (FIELDS[p.id] && !p.scope ? { ...p, ...FIELDS[p.id] } : p)),
  };
}

// ---------- 影响面（INC-05） ----------
const sc = (key, title) => ({ type: 'trace.scenario', key, title });
export const SCENARIOS = {
  affected: [sc('s-03', '场景 3 门店更严（陈列量）'), sc('s-04', '场景 4 乳制品隔日到货 ≤150 箱'), sc('s-06', '场景 6 临时加量缺必须审批的分支'), sc('s-08', '场景 8 临期检查按批')],
  guard: [sc('s-01', '场景 1 门店更严（每日到货小批量）'), sc('s-07', '场景 7 商品不在受理范围')],
};
const STALE = [
  { target: entryRef('display_qty_std'), note: null },
  { target: entryRef('display_qty_check'), note: null },
  { target: entryRef('r11_temp_increase'), note: '原 6.2 条' },
  { target: REFS.op, note: '新增 6.3' },
  { target: entryRef('r09_store_stricter'), note: null },
  { target: entryRef('check_item'), note: '检查频次' },
];
const PHASES = [['按变更点定位产物', 0.22], ['沿引用算下游', 0.56], ['挑回归场景', 0.22]];

export function impactOf(ms = 1800) {
  return {
    duration_ms: ms,
    phases: PHASES.map(([label, f]) => ({ label, duration_ms: Math.round(ms * f / 100) * 100 })),
    stale: STALE,
    regression: SCENARIOS,
    semantic_scope_text: `只对这 ${STALE.length} 个产物做语义判断，其余不动`,
  };
}

// ---------- 卡片 ----------
const entryLinks = () => STALE.map((s) => s.target);

export function listCard(cl) {
  return {
    card_id: CARD.list, card_type: 'change_list', eyebrow: '', title: '变更点清单', subtitle: '', status: null, plugin: null,
    open: { kind: 'dialog', target: 'change_list', ref: { change_list_id: cl.id } },
    change_list_id: cl.id, source: cl.source, count: cl.points.length,
    low_confidence: cl.points.filter((p) => p.confidence === 'low').length,
    rows: cl.points.slice(0, 10), state: cl.state,
  };
}

export function impactCard(cl, impact, executed = false) {
  return {
    card_id: CARD.impact, card_type: 'impact', eyebrow: '', title: '影响面', subtitle: '', status: null, plugin: null, open: null,
    ...impact, change_list_id: cl.id, executed, entry_links: entryLinks(),
  };
}

// 和待处理、资料区「外部反馈」里的是同一份（fb-1101-01，评测服务回传的 4 条失分用例）
const FEEDBACK_ITEMS = [
  { case_id: 'c-01', question: '牛奶日均销量 60 箱、隔日到货，门店申请 ≤96 箱', loss_reason: '超量方向判断反了（步骤 3）', artifacts: [REFS.op], related: null },
  { case_id: 'c-02', question: '酸奶临期抽检', loss_reason: '临期损耗标准缺这个商品', artifacts: [entryRef('expiry_loss_std')], related: null },
  { case_id: 'c-03', question: '门店指定 2024 版陈列标准', loss_reason: '用了 2019 版，缺少 6.3 条', artifacts: [entryRef('applicable_std')], related: '同变更点 5' },
  { case_id: 'c-04', question: '临时加量 5 箱', loss_reason: '直接下单，R-11 没有挂到操作', artifacts: [entryRef('r11_temp_increase'), REFS.op], related: '同变更点 3' },
];

export function feedbackCard() {
  return {
    card_id: CARD.feedback, card_type: 'external_feedback', eyebrow: '', title: `外部反馈 · ${FEEDBACK_ITEMS.length} 条失分用例`, subtitle: '',
    status: null, plugin: { name: 'evolution', label: '反馈演进' }, open: null,
    feedback_id: FEEDBACK_ID, source: { service: '评测服务', run: 'eval-1101-0910' }, items: FEEDBACK_ITEMS,
    entry_links: [REFS.op, entryRef('expiry_loss_std'), entryRef('applicable_std'), entryRef('r11_temp_increase')],
  };
}

const FAIL_REASON = '临时加量 5 箱时预期要区域审批，实际仍按店长确认直接下单：操作 [[超量与审批提示]] 步骤 4 还没引用改写后的 R-11。';

export function regressionCard(fixed = false) {
  const rows = [
    ...SCENARIOS.affected.map((s) => ({ scenario: s, type: 'affected', result: s.key === 's-06' && !fixed ? 'fail' : 'pass', reason: s.key === 's-06' && !fixed ? FAIL_REASON : null })),
    ...SCENARIOS.guard.map((s) => ({ scenario: s, type: 'guard', result: 'pass', reason: null })),
  ];
  return {
    card_id: CARD.regression, card_type: 'regression', eyebrow: '', title: '回归结果', subtitle: '', status: null, plugin: null, open: null,
    change_list_id: CL_ID, passed: rows.filter((r) => r.result === 'pass').length, total: rows.length, rows, entry_links: [REFS.op],
  };
}

const item = (change, target, text) => ({ change, target, text, comparable: change !== 'test' });
export function roundCard({ csId, nextVersion, state = 'pending_confirm', mergedVersion = null, fixed = false }) {
  const n = SCENARIOS.affected.length + SCENARIOS.guard.length;
  const op = [
    item('mod', entryRef('display_qty_check'), '操作 [[陈列量核定]]（按新上限核定）'),
    item('mod', REFS.op, '操作 [[超量与审批提示]]（步骤 2、3 引用改写后的 R-11）'),
  ];
  if (fixed) op.push(item('mod', REFS.op, '操作 [[超量与审批提示]] 的步骤 4（引用改写后的 R-11）'));
  return {
    card_id: CARD.round, card_type: 'round_changes', eyebrow: '', title: '这一轮改了什么', subtitle: '本次增量用时 9 分钟 · 确认后保存为一个版本',
    status: null, plugin: null, open: null,
    changeset_id: csId, change_list_id: CL_ID, checks: { errors: 0, warnings: 1 },
    groups: [
      { kind: 'object', label: '对象', items: [
        item('mod', entryRef('display_qty_std'), '对象 [[陈列量标准]]（乳制品上限 120 → 150 箱，日均销量 ≥80 箱时适用）'),
        item('mod', entryRef('check_item'), '对象 [[检查项目]]（临期检查频次改为每批）'),
      ] },
      { kind: 'rule', label: '规则', items: [
        item('mod', entryRef('r11_temp_increase'), '规则 [[R-11 临时加量须审批]]（改为必须区域审批）'),
        item('mod', entryRef('r09_store_stricter'), '规则 [[R-09 门店申请取严]]（门店指定标准时以门店为准）'),
      ] },
      { kind: 'operation', label: '操作', items: op },
      { kind: 'scenario', label: '场景', items: [item('test', SCENARIOS.affected[0], `场景 ${SCENARIOS.affected.length} 个，回归 ${fixed ? n : n - 1} / ${n} 通过`)] },
    ],
    lock_note: '确认生效前，这些条目在 Studio 和插件页里只读；撤回这一轮也会解除',
    next_version_label: nextVersion, state, merged_version_label: mergedVersion, remaining_failures: fixed ? 0 : 1,
    entry_links: entryLinks(),
  };
}

// CS-03：这一轮的实体差异
const d = (kind, text) => ({ kind, text });
const DIFFS = [
  [entryRef('display_qty_std'), 'mod', [d('header', '陈列上限 · 乳制品'), d('del', '上限 ≤120 箱'), d('add', '上限 ≤150 箱（日均销量 ≥80 箱时适用）'), d('note', '出处：09 修订通知 §2')]],
  [entryRef('check_item'), 'mod', [d('header', '检查频次'), d('del', '每次到货检查一次'), d('add', '每批检查一次（同一到货批次、同一品类算一批）'), d('note', '出处：09 修订通知 §5')]],
  [entryRef('r11_temp_increase'), 'mod', [d('header', '规则正文'), d('del', '临时加量：店长确认后可以下单'), d('add', '临时加量：必须区域审批，审批通过前不得下单'), d('note', '出处：09 修订通知 §4')]],
  [entryRef('r09_store_stricter'), 'mod', [d('header', '规则正文'), d('ctx', '门店申请严于标准时，按门店执行'), d('add', '门店指定了适用标准时，以门店指定的为准（6.3）'), d('note', '出处：09 修订通知 §6')]],
  [entryRef('display_qty_check'), 'mod', [d('header', '步骤 2'), d('del', '按品类标准上限 120 箱核定'), d('add', '按 [[陈列量标准]] 的新上限核定')]],
  [REFS.op, 'mod', [d('header', '步骤 2、3'), d('del', '临时加量由店长确认'), d('add', '临时加量引用 [[R-11 临时加量须审批]]，生成审批提示')]],
];
export function diffItems(target) {
  return DIFFS.filter(([t]) => !target || t.id === target || `${t.type}/${t.key}` === target)
    .map(([t, change, lines]) => ({ target: t, change, sections: [{ title: '', lines }] }));
}

// ---------- 建议条（FL-02） ----------
const sg = (id, label, icon, action, extra = {}) => ({ id, label, icon, primary: false, disabled: false, reason: '', priority: 3, action, ...extra });
const inbox = (others) => sg('sg-inbox', `其他待处理 ${others}`, 'inbox', { type: 'navigate', page: 'inbox' });
const fixAction = (text) => ({ type: 'send', text, command: '/ontology-update', args: { change_list_id: CL_ID, fix_scenarios: ['s-06'] } });

// 执行前：确认清单并执行（INC-04）、逐条修改清单（弹窗）、其他待处理
export function preSuggestions(others) {
  return {
    mode: 'normal',
    primary: sg('sg-inc-run', '确认清单并执行', 'play', { type: 'execute_change_list', change_list_id: CL_ID }, { primary: true, priority: 2 }),
    secondary: [
      sg('sg-inc-edit', '逐条修改清单', 'edit', { type: 'navigate', page: 'incremental', params: { dialog: 'change_list', change_list_id: CL_ID } }),
      inbox(others),
    ],
    all_commands: true,
  };
}

// 执行后、确认前：确认生效、修复不通过的场景、撤回这一轮
export function postSuggestions(csId, fixed = false) {
  return {
    mode: 'pending_confirm',
    primary: sg('sg-merge', '确认生效', 'merge', { type: 'merge_changeset', changeset_id: csId }, { primary: true, priority: 2 }),
    secondary: [
      fixed
        ? sg('sg-see', '查看改动', 'eye', { type: 'show_changes', changeset_id: csId })
        : sg('sg-inc-fix', '修复不通过的场景', 'sparkle', fixAction('修复不通过的场景 6')),
      sg('sg-discard', '撤回这一轮', 'undo', { type: 'discard_changeset', changeset_id: csId }),
    ],
    all_commands: true,
  };
}

// 确认生效后：修复留下的场景、看这个版本、其他待处理
export function mergedSuggestions(version, others, fixed = false) {
  const see = sg('sg-version', `看版本 ${version}`, 'history', { type: 'navigate', page: 'versions', params: { v: version } });
  return {
    mode: 'normal',
    primary: fixed ? { ...see, primary: true } : sg('sg-inc-fix', '修复场景 6', 'sparkle', fixAction('修复场景 6'), { primary: true }),
    secondary: fixed ? [inbox(others)] : [see, inbox(others)],
    all_commands: true,
  };
}

// ---------- 对话历史 ----------
// 执行时对话里那条用户消息：「确认清单并执行（第 4 条已改写：…）」
export function executeText(cl) {
  const rw = cl.points.filter((p) => p.state === 'rewritten');
  if (!rw.length) return '确认清单并执行';
  return `确认清单并执行（${rw.map((p) => `第 ${p.n} 条已改写：${p.rewritten_text || [p.condition, p.value, p.scope].filter(Boolean).join(' ')}`).join('；')}）`;
}

const runSteps = (done) => [
  { id: 'x1', text: '按清单修改 6 个产物，资料分析报告已加入 09-修订通知', meta: done > 0 ? '2 分 40 秒' : '进行中', state: done > 0 ? 'done' : 'running', duration_ms: done > 0 ? 160000 : null },
  { id: 'x2', text: '只查改动：6 个产物 0 个错误、1 个提醒', meta: done > 1 ? '5 秒' : done === 1 ? '进行中' : '', state: done > 1 ? 'done' : done === 1 ? 'running' : 'dot', duration_ms: done > 1 ? 5000 : null },
  { id: 'x3', text: '自动回归 6 个场景：5 个通过，1 个不通过', meta: done > 2 ? '46 秒' : done === 2 ? '进行中' : '', state: done > 2 ? 'done' : done === 2 ? 'running' : 'dot', duration_ms: done > 2 ? 46000 : null },
];
export function runProgress(done) {
  const steps = runSteps(done);
  if (done < 3) steps[2] = { ...steps[2], text: '自动回归 6 个场景' };
  return { group_id: 'pg-inc-run', title: '', desc: '', state: done > 2 ? 'done' : 'running', started_at: new Date().toISOString(), duration_ms: done > 2 ? 211000 : null, collapsed: false, steps };
}

/**
 * 「09 修订通知」这条对话的历史。phase：editing（等你确认清单，设计稿默认态）、executed（执行完、待确认生效）、merged（已确认生效）。
 */
export function incHistory({ pid, cl, impact, others, phase = 'editing', csId = null, version = null }) {
  let seq = 4000;
  const t = { merged: ago(41), user: ago(40), plan: ago(32), fb: ago(29), exec: ago(12), done: ago(9) };
  const e = (type, data, ts) => ({ id: String(++seq), type, ts, project_id: pid, conversation_id: INC_CID, data });
  const executed = phase !== 'editing';
  const out = [
    e('day.marker', { label: '今天' }, t.merged),
    e('notice', { kind: 'merged', text: `${stamp(t.merged)} · 切片「补货申请与门店需求」已确认生效，保存为版本 v0.12`, version_label: 'v0.12', changeset_id: 'cs-29', align: 'center' }, t.merged),
    e('message.user', {
      message_id: 'm-inc-u1', text: '修订通知到了，按这个更新', references: [], origin: null, delivery: 'immediate',
      attachments: [{ upload_id: 'up-09', filename: '09-修订通知.docx', size: 46000, kind: 'doc' }],
    }, t.user),
    e('message.assistant', { message_id: 'm-inc-a1', text_md: '收到。这是对已有资料的修订，我按增量更新来：先把变更拆成能逐条核对的清单，再算出影响面，你确认后只改受影响的部分。', anchors: [], entry_links: [], continued: false }, t.plan),
    e('progress', {
      group_id: 'pg-inc-plan', title: '', desc: '', state: executed ? 'done' : 'waiting', started_at: t.user, duration_ms: null, collapsed: false,
      steps: [
        { id: 'p1', text: `读完 09-修订通知，拆出 ${cl.points.length} 处变更点，每条都带出处`, meta: '8 秒', state: 'done', duration_ms: 8000 },
        { id: 'p2', text: `确定性影响分析：${impact.stale.length} 个产物、${impact.regression.affected.length + impact.regression.guard.length} 个回归场景`, meta: '1.8 秒', state: 'done', duration_ms: 1800 },
        executed
          ? { id: 'p3', text: '变更点清单已确认', meta: '确认后才动手修改', state: 'done', duration_ms: null }
          : { id: 'p3', text: '等你确认变更点清单，第 4 条原文只写了「每批」，把握低', meta: '确认前不会改任何内容', state: 'waiting', duration_ms: null },
      ],
    }, t.plan),
    e('card', listCard(cl), t.plan),
    e('card', impactCard(cl, impact, executed), t.plan),
    e('notice', { kind: 'info', text: `${stamp(t.fb)} · 评测服务回传反馈`, version_label: null, changeset_id: null, align: 'center' }, t.fb),
    e('message.assistant', { message_id: 'm-inc-a2', text_md: '评测服务刚回传 4 条失分用例。第 3、4 条和变更点 5、3 是同一件事，这次执行后会一起回归；第 1、2 条要单独分析。', anchors: [], entry_links: [], continued: false }, t.fb),
    e('card', feedbackCard(), t.fb),
  ];
  if (!executed) {
    out.push(e('suggestions.updated', preSuggestions(others), t.fb));
    return out;
  }
  out.push(
    e('message.user', { message_id: 'm-inc-u2', text: executeText(cl), attachments: [], references: [], origin: null, delivery: 'immediate' }, t.exec),
    e('progress', { ...runProgress(3), started_at: t.exec }, t.exec),
    e('card', regressionCard(), t.done),
    e('card', roundCard({ csId, nextVersion: version, state: phase === 'merged' ? 'merged' : 'pending_confirm', mergedVersion: phase === 'merged' ? version : null }), t.done),
  );
  if (phase === 'merged') {
    out.push(
      e('notice', { kind: 'merged', text: `这一轮已确认生效 · 版本 ${version}`, version_label: version, changeset_id: csId, align: 'center' }, t.done),
      e('suggestions.updated', mergedSuggestions(version, others), t.done),
    );
  } else out.push(e('suggestions.updated', postSuggestions(csId), t.done));
  return out;
}

