// 说明页的示例事件与卡片（合成数据，版式取自设计稿第五版 chat-states 页）
let seq = 0;
const ev = (type, data) => ({ id: String(++seq), type, ts: '2026-11-01T06:06:00.000Z', data });

export const CONVERSATIONS = [
  { id: 'c1', title: '超量与审批提示', time: '刚刚' },
  { id: 'c2', title: '陈列量核定返工', time: '2 小时前' },
  { id: 'c3', title: '资料分析答疑', time: '昨天' },
  { id: 'c4', title: '默认会话', time: '10-28' },
];

export const COMMANDS = [
  ['建模', [['引导式建模', '从头带你走一遍'], ['资料分析', '读全资料，提出分段'], ['提取对象', '从资料里抽出业务对象', 1], ['提取操作', '抽出业务流程和步骤'], ['规则与指标', '抽出判断规则和统计口径'], ['质量检查', '规则检查 + 语义检查'], ['增量更新', '资料变了，只改受影响的部分']]],
  ['可选能力', [['场景验证', '用样例推演，看结论对不对'], ['生成示例数据', '按场景需要生成'], ['画流程图', '创建或打磨业务流程图'], ['扫描脚本候选', '找出适合写成脚本的操作']]],
];

// 第 3 项：运行中，质量检查派出 3 个子任务
export const RUNNING_EVENTS = [
  ev('progress', {
    group_id: 'g3', title: '', desc: '', state: 'running', collapsed: false,
    steps: [
      { id: 'a', text: '读完陈列量核定相关的 4 份资料', meta: '覆盖 100% · 22 秒', state: 'done' },
      { id: 'b', text: '质量检查：同时派出 3 个子任务', meta: '进行中', state: 'running' },
    ],
  }),
  ev('subagent.updated', { task_id: 't1', group_id: 'g3', title: '规则检查', status: 'done', progress_text: '', summary: '0 个错误' }),
  ev('subagent.updated', { task_id: 't2', group_id: 'g3', title: '语义检查', status: 'running', progress_text: '第 6 / 11 个操作', summary: null }),
  ev('subagent.updated', { task_id: 't3', group_id: 'g3', title: '覆盖检查', status: 'started', progress_text: '等待子任务反馈', summary: null }),
];

export const RESUME_CARD = {
  card_id: 'r1', card_type: 'resume', exec_id: 'ex-0412', title: '上次没做完：操作提取在步骤 3 / 4 中断',
  reason: '服务在 14:32 重启', position: { step: 3, total: 4, label: '写入即查（步骤 3 / 4）' },
  saved: { files: 2, version_label: 'v0.12' }, resumable: true, not_resumable_reason: null,
};

export function permissionItem() {
  return {
    kind: 'permission', request_id: 'p1', title: '助手请求执行一个操作',
    description: '在脚本验证沙箱里试跑 4 个脚本，确认消费端能按约定调用它们',
    details_text: '脚本 4 个 · 沙箱禁止出网 · 只读挂载知识包', timeout_s: 600,
    expires_at: new Date(Date.now() + (9 * 60 + 41) * 1000).toISOString(), resolved: null,
  };
}

export const BRANCH_Q = {
  question_id: 'qb', kind: 'branch', title: '走查分支选择', scenario: { type: 'trace.scenario', key: 's-3', title: '场景 3 · 门店更严' },
  context_text: '推演到判断「申请量是否严于标准」：门店申请牛奶 96 箱，标准上限 120 箱。走哪条？',
  options: [{ id: 'o1', key: '1', label: '严于标准 → 按门店执行' }, { id: 'o2', key: '2', label: '超出标准 → 生成审批提示' }],
};

export const EXPERT_Q = {
  question_id: 'qe', kind: 'expert', eyebrow: '请专家确认', title: '临期商品「保质期剩余 ≤3 天下架」是所有品类通用，还是按品类区分？',
  options: [{ id: 'all', key: 'A', label: '所有品类通用' }, { id: 'by', key: 'B', label: '按品类区分' }, { id: 'dismiss', key: 'C', label: '不需要问这个', dismiss: true }],
};

const ent = (kind, name) => ({ kind, id: `${kind}:${name}`, key: name, name });
export const ROUND_DETAIL = {
  card_id: 'rd', card_type: 'round_changes', changeset_id: 'cs-31', state: 'pending_confirm',
  detail_groups: [
    { kind: 'object', label: '对象', items: [{ target: ent('object', '商品品类'), detail: '修改 · 新增属性「适用标准」' }] },
    { kind: 'operation', label: '操作', items: [{ target: ent('operation', '超量与审批提示'), detail: '修改步骤 2' }] },
    { kind: 'rule', label: '规则', items: [{ target: ent('rule', 'R-09 门店申请取严'), detail: '新增' }] },
    { kind: 'scenario', label: '场景', items: [
      { target: { type: 'trace.scenario', key: 's1', title: '门店更严 → 按门店执行' }, detail: '新增 · 推演通过' },
      { target: { type: 'trace.scenario', key: 's2', title: '门店超量 → 生成审批提示' }, detail: '新增 · 推演通过' },
    ] },
  ],
};

export const CONTEXT = {
  used: 156000, window: 200000, pct: 78, compact_at_pct: 80,
  breakdown: { rules: 18000, materials: 64000, conversation: 52000, tool_results: 22000 },
  last_call: { input: 18400, output: 1200, estimated: true },
};
export const USAGE = { input_tokens: 3200000, output_tokens: 410000, cost: { amount: 126, currency: 'CNY' } };

export const COMPACT_EVENTS = [
  { id: '1', type: 'context.compacted', ts: '2026-11-01T06:20:00.000Z', data: { trigger: 'auto', pre_tokens: 156000, post_tokens: 58000, freed_tokens: 98000, kept: { decisions: 12, artifacts: true, todos: true }, at: new Date(new Date().setHours(14, 20, 0, 0)).toISOString() } },
];

export const CONFIG_PENDING = { changes: [{ kind: 'assembly', summary: '建模能力有更新' }], effective: 'next_message', detail_link: { page: 'plugin-detail', pack: 'core' } };
export const RECONNECTED = { reason: 'config_changed', changes: [], context_kept: true, duration_ms: 800 };

export const ERROR_ITEM = { error_id: 'err-7f3a', message: '模型服务暂时不可用', retries: 2, retryable: true, actions: ['retry', 'redirect'] };

export const PACK_CARD = {
  card_id: 'pk', card_type: 'pack_recommendation', pack: { name: 'provider-db', version: '0.3.0', label: '业务数据库' },
  category_label: '数据接入', description: '只读采集 MySQL / PostgreSQL，凭据只放在连接器网关，助手拿不到', reason: '',
};

export const ATTACH_GROUP = {
  group_id: 'ga', title: '', state: 'waiting',
  steps: [
    { id: 'a', text: '抽出 1 个操作「超量与审批提示」', meta: '41 秒', state: 'done' },
    { id: 'b', text: '写入即查：0 个错误', meta: '3 秒', state: 'done' },
    { id: 'c', text: '等你补充：审批提示交给谁？', meta: '', state: 'waiting' },
  ],
};

export const SUG_STRIPS = [
  ['刚被打断', '第 4 条', [['继续「生成验收场景」', 'redo', 1], ['放弃这一步', 'x']]],
  ['有过期的步骤', '第 7 条 · 上游「对象」改过', [['刷新「补货 · 操作」', 'refresh', 1], ['刷新「补货 · 规则」', 'refresh']]],
  ['主线下一步', '第 9 条 · 对象已确认', [['提取操作「补货」', 'flow', 1]]],
];

export const WALK_Q = {
  question_id: 'qw', kind: 'walkthrough', title: '交互走查 · 门店申请量超出标准',
  operation: { kind: 'operation', id: 'operation:overage', key: 'overage', name: '超量与审批提示' },
  inputs: [{ name: '门店', value: '示例门店' }, { name: '品类', value: '乳制品' }, { name: '商品', value: '牛奶' }, { name: '到货周期', value: '2 天' }, { name: '申请量', value: '150 箱' }],
  steps: [
    { n: 1, text: '读取申请量：牛奶 150 箱', source: '来自输入（显式传入）' },
    { n: 2, text: '按适用标准取上限：乳制品 ≤120 箱', source: '查 [[陈列量标准]] · 示例数据' },
  ],
  prompt: '申请量 150 超出标准 120，走哪条分支？',
  options: [
    { id: 'A', key: 'A', label: '生成审批提示，审批通过前不下单', consequence: '按补货规则 §11.4，推荐', recommended: true },
    { id: 'B', key: 'B', label: '按门店执行', consequence: '和 §9.2「超出标准须审批」矛盾' },
    { id: 'C', key: 'C', label: '停下来，报告资料矛盾', consequence: '把这个场景标为待确认' },
  ],
};
