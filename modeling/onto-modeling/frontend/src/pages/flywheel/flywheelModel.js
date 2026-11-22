// 知识包数据飞轮的展示逻辑：状态与原因文案、记录筛选、操作是否可用、确认后的接口与提示。状态由后端 /flywheel 给出。

export const POLL_ACTIVE_MS = 4000;
export const POLL_IDLE_MS = 30000;
// 轮询主要读本地索引；每隔若干次向评测服务对账，通知丢失时也能补上
export const RECONCILE_EVERY = 6;
// 0 表示无限次：一直处理到评测集全部通过
export const ROUND_LIMITS = [1, 3, 5, 0];
export const roundsLabel = (limit) => (limit ? `最多 ${limit} 轮` : '无限次');
// 后台授权时长（天）；0 表示无限制
export const DELEGATION_OPTIONS = [
  { value: 1, label: '1 天' },
  { value: 3, label: '3 天' },
  { value: 7, label: '7 天' },
  { value: 30, label: '30 天' },
  { value: 0, label: '无限制' },
];

export const STATUS = {
  waiting_optimize: ['待确认优化', 'amber'],
  optimizing: ['优化执行中', 'blue'],
  waiting_publish: ['待确认发布', 'amber'],
  publishing: ['发布执行中', 'blue'],
  waiting_evaluation: ['待确认评测', 'amber'],
  evaluating: ['评测执行中', 'blue'],
  published: ['已发布', 'green'],
  passed: ['评测通过', 'green'],
  blocked: ['需处理', 'red'],
  interrupted: ['已中止', 'gray'],
  rolledback: ['已回退', 'gray'],
  restored: ['已恢复内容', 'green'],
  ignored: ['已忽略', 'gray'],
  // 项目已换到新版本，这条无法再优化或发布：自动关闭，不算待处理
  expired: ['已过期', 'gray'],
};

export const ACTIVE = ['optimizing', 'publishing', 'evaluating'];
export const OPTIMIZE_STEPS = ['保存快照', '读取评测建议', '修订知识内容', '质量与发布检查'];
export const PUBLISH_STEPS = ['冻结内容快照', '校验发布条件', '上传知识包', '确认 Hub 回执'];
// 评测在评测服务执行，这里只知道已发起、尚未收到结果，所以停在“执行评测用例”
export const EVALUATE_STEPS = ['发布到 Hub', '发起评测', '执行评测用例', '关联评测结果'];
export const EVALUATE_STEP = 2;
// 优化阶段的阻断显示在「自动化处理优化建议」卡片，其余显示在「自动化确认发布与发起评测」卡片
export const OPTIMIZE_REASONS = ['suggestions_failed', 'version_mismatch', 'round_limit', 'optimization_interrupted',
  'optimization_failed', 'assistant_unavailable', 'no_changes'];
export const RETRY_REASONS = ['hub_failed', 'publish_interrupted', 'hub_expired', 'request_conflict', 'authorization',
  'solver_unverified'];
export const RECHECK_REASONS = ['checks_failed', 'content_changed'];
export const EVALUATION_REASONS = ['evaluation_failed'];
const RESUMABLE = ['optimization_interrupted', 'optimization_failed', 'assistant_unavailable'];
const IGNORABLE = ['version_mismatch', 'suggestions_failed', 'no_changes', ...RESUMABLE];

// 运行记录的状态筛选在服务端进行（与后端 RUN_FILTERS 的分组一致），这里只提供下拉框选项
export const FILTERS = [
  { value: 'all', label: '全部状态' },
  { value: 'waiting', label: '待确认' },
  { value: 'running', label: '执行中' },
  { value: 'success', label: '已完成' },
  { value: 'blocked', label: '需处理' },
  { value: 'rolledback', label: '已回退' },
];

export const version = (value) => (value ? `v${value}` : '未发布');
// 包负责人确认交给建模的用户反馈：没有评测集，发布后不评测；可以针对旧版本
export const isFeedback = (run) => run?.origin === 'consumer_feedback';
// 评测得分是评测页面「结果概览」的严格通过率：通过的用例占全部计划用例的百分比，与评测页面一样显示整数百分比
export const rate = (value) => (value == null ? '—' : `${Math.round(Number(value))}%`);

// Hub 按补丁号递增分配版本；这里只作预估，确认后以 Hub 回执为准
export function nextVersion(value) {
  if (!/^\d+(\.\d+)*$/.test(value || '')) return '';
  const parts = value.split('.').map(Number);
  parts[parts.length - 1] += 1;
  return parts.join('.');
}

export const iteration = (snapshot) => (snapshot ? `iteration-${String(snapshot.iteration).padStart(3, '0')}` : '尚未生成');

const pad = (value) => String(value).padStart(2, '0');
export function formatAt(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
export function formatTime(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function formatDay(seconds) {
  const date = new Date(seconds * 1000);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

const UNLINKED = '项目尚未发布到 Hub，发布首个正式版本后自动登记后台授权。';
const LOGIN_MISSING = '评测服务未保存后台评测登录，新版本发布后需确认才开始评测。在任一设备退出登录或修改密码后都需重新授权。';

export const loginMissing = (state) => !!state?.settings?.publish && state.delegation?.login === 'missing'
  && state.delegation?.hub?.status !== 'unlinked';

// 开关已打开、但后台授权有问题时的一句话；一切正常时为空，界面上不显示
export function switchWarning(state, key) {
  if (!state?.settings?.[key]) return '';
  const item = state.delegation?.[key === 'optimize' ? 'evaluation' : 'hub'];
  if (item?.status === 'unlinked') return UNLINKED;
  if (item?.status === 'issue') return `后台授权未就绪，需保持页面打开才会自动执行：${item.message}`;
  if (item?.status === 'pending') return '后台授权未就绪，需保持页面打开才会自动执行。';
  return key === 'publish' && loginMissing(state) ? LOGIN_MISSING : '';
}

export function reauthorizeNotice(view) {
  if (view.delegation?.login === 'active') return '评测服务已重新保存后台评测登录，新版本发布后自动开始评测。';
  const issue = view.delegation?.evaluation?.message;
  return `后台评测登录仍未就绪${issue ? `：${issue}` : '，请重新登录后再试'}`;
}

export function delegationHint(days) {
  if (days === 0) return '不会自动到期，只有关闭开关才会销毁；修改密码不会结束授权。';
  return `离开项目后，最多继续自动执行 ${days} 天；每次打开项目重新计时。`;
}

export function delegationNotice(view, days) {
  const { label } = DELEGATION_OPTIONS.find((item) => item.value === days);
  const failed = [view.delegation?.evaluation, view.delegation?.hub].find((item) => item?.status === 'issue');
  return failed ? `后台授权有效期已改为 ${label}，但后台授权未就绪，需保持页面打开：${failed.message}`
    : `后台授权有效期已改为 ${label}。`;
}

export function toggleNotice(view, key, enabled, wasActive) {
  const label = key === 'optimize' ? '自动处理优化建议' : '自动确认发布与发起评测';
  if (!enabled) return wasActive ? '已关闭后续自动任务；当前已启动的步骤继续执行。' : '已关闭并销毁后台授权；后续事项将等待你确认。';
  const item = view?.delegation?.[key === 'optimize' ? 'evaluation' : 'hub'];
  if (item?.status === 'active' && key === 'publish' && view.delegation.login === 'missing') return `${label}已开启。${LOGIN_MISSING}`;
  if (item?.status === 'active') return `${label}已开启，已登记后台授权，页面关闭后继续执行；已有待确认事项仍需单独确认。`;
  if (item?.status === 'unlinked') return `${label}已开启。${UNLINKED}`;
  return `${label}已开启，但后台授权未就绪，需保持页面打开：${item?.message || '请稍后重试'}`;
}

// 候选内容是否连带上次发布后其他未发布的修改：后端比较优化前的内容与上次发布的内容。
// true 时提示；null 表示无法确认（上次发布或这次优化早于该检查）
export function earlierChangesNote(run, state) {
  if (run.earlier_changes !== true) return '';
  return `上次发布 ${version(state.package?.version)} 之后，优化开始前工作区里已有其他未发布的修改，`
    + '确认发布会把它们一并发布；「对比改动」只显示本次优化的改动。';
}

export function statusOf(run) {
  if (run.status === 'ignored' && run.reason === 'already_resolved') return ['最新版本已解决', 'green'];
  if (run.status === 'waiting_optimize' && run.human_hold === false) return ['等待自动执行', 'blue'];
  if (run.status === 'waiting_evaluation' && run.human_hold === false) return ['等待自动评测', 'blue'];
  return STATUS[run.status] || ['待处理', 'gray'];
}

export function reasonTitle(run) {
  return ({
    checks_failed: '发布检查未通过',
    content_changed: '候选内容已变化',
    hub_failed: 'Hub 发布失败',
    hub_expired: 'Hub 发布请求已过期',
    request_conflict: '发布请求冲突',
    publish_interrupted: '上次发布未完成',
    authorization: '需要重新授权',
    solver_unverified: '求解能力尚未验证',
    version_mismatch: '评测版本与当前版本不一致',
    suggestions_failed: '优化建议生成失败',
    round_limit: '已达到本轮自动处理上限',
    optimization_interrupted: '优化已中断',
    optimization_failed: '优化未完成',
    assistant_unavailable: '建模助手暂不可用',
    no_changes: '本次优化未修改知识内容',
    evaluation_failed: '新版本评测未完成',
    already_resolved: '最新版本已解决',
  })[run.reason] || '等待处理';
}

export function reasonText(run, { current, maxRounds } = {}) {
  const failing = run.check_groups?.failing?.length ? `未通过：${run.check_groups.failing.join('、')}。` : '';
  return ({
    checks_failed: `${failing}已保留优化结果，修复并重新检查后才能发布。`,
    content_changed: '候选内容生成后工作区又有修改，未创建新版本。请重新检查后再发布。',
    hub_failed: '未收到 Hub 成功回执。本次冻结内容和发布请求已保留，重试将继续同一发布请求。',
    hub_expired: '原发布请求已过期，重试将按候选内容创建新的发布请求。',
    request_conflict: '同一发布请求已用于其他内容，重试将创建新的发布请求。',
    publish_interrupted: '发布过程中断，尚未确认 Hub 回执。确认后重试同一发布请求。',
    authorization: 'Hub 未接受当前登录，请重新登录后重试同一发布请求。',
    solver_unverified: '当前求解契约修订尚未通过求解验证，飞轮不代为确认，未创建新版本。修订未改动求解契约时，'
      + '可先在发布页「已发布版本」对当前版本完成求解验证后重试发布；否则请在发布检查中确认后手动发布。',
    version_mismatch: `该通知基于 ${version(run.version)}，当前项目基于 ${version(current)}。请评测当前版本后再优化。`,
    suggestions_failed: '评测报告已完成，建议生成失败。重新生成成功前不会触发优化。',
    round_limit: `已连续自动处理 ${maxRounds} 轮，转为人工确认，防止无休止地优化与发布。`,
    optimization_interrupted: '建模助手本轮未正常结束，已保留执行前快照。可以恢复优化前内容，或重新确认优化。',
    optimization_failed: '建模助手本轮执行出错，已保留执行前快照。可在工作台对话查看原因后重新确认优化。',
    assistant_unavailable: '建模助手暂不可用，未修改任何知识内容。可稍后重新确认优化。',
    no_changes: isFeedback(run) ? '建模助手没有修改知识包文档，也没有确认问题已在最新版本解决；请查看演进报告后处理或忽略。'
      : '建模助手没有修改知识包文档，无需发布新版本。',
    already_resolved: '建模助手核对后没有修改知识包文档：反馈的问题在最新版本已不存在。',
  })[run.reason] || run.note || run.error || '请查看本次运行的详细记录。';
}

// 开关开着却仍要人工确认的原因；没有特别原因时为空
export function holdHint(run, maxRounds) {
  if (run.human_hold === false) return '';
  if (run.reason === 'round_limit') return `已连续自动处理 ${maxRounds} 轮，本次等待人工确认后继续。`;
  if (run.reason === 'project_busy') return '通知到达时项目已有待处理事项，本次保留为人工确认。';
  return '';
}

// 只说需要你知道的原因：图数据要确认删除、自动评测失败或缺少后台评测登录
export function evaluationHint(run, state) {
  if (run.human_hold === false) return '';
  if (run.graph_confirmation) return `${run.graph_confirmation}。确认评测时一并确认删除。`;
  if (run.error) return `自动评测未开始：${run.error}`;
  return state.settings.publish ? '评测服务没有可用的后台评测登录，需你确认后发起。' : '';
}

// 用户反馈针对旧版本时的说明：优化时先核对当前版本
export function feedbackNote(run, state) {
  if (!isFeedback(run) || run.release_id === state.package?.release_id) return '';
  return `反馈针对 ${version(run.version)}，优化时先核对当前版本 ${version(state.package?.version)} 是否仍存在这些问题，仍存在的才修改。`;
}

// 运行记录里写明新版本的评测：发起方式与结果
export function evaluationLine(run) {
  if (isFeedback(run) && run.status === 'published') return '用户反馈，发布后不评测';
  const value = run.evaluation;
  if (!value || value.status === 'waiting') return '';
  if (value.status === 'running') return value.mode === 'auto' ? '已自动发起新版本评测' : '已确认发起新版本评测';
  if (value.status === 'done') return value.passed ? '新版本评测全部通过' : `新版本评测 ${rate(value.score)}，转入下一轮`;
  if (value.status === 'skipped') return '已跳过新版本评测';
  return '新版本评测未完成';
}

// 由飞轮发布的版本经评测产生的运行：标明来自哪次运行、由谁发起
export function sourceLine(run) {
  if (!run.trigger || !run.source_id) return '';
  return run.trigger === 'flywheel' ? `${run.source_id} 发布后自动评测` : `${run.source_id} 发布后在评测页面评测`;
}

export function mode(run) {
  if (run.kind === 'rollback') return '人工回退';
  if (run.status === 'waiting_evaluation') return run.human_hold === false ? '等待自动评测' : '待人工确认';
  if (run.status === 'passed') return '无需优化';
  if (run.status === 'expired') return '无需处理';
  if (run.mode === 'auto' || run.publish_mode === 'auto') return '自动执行';
  if (run.status === 'waiting_optimize') return run.human_hold === false ? '等待自动执行' : '待人工确认';
  return '本次已确认';
}

// 记录列表分批读取：每批条数，以及重新读取已加载范围时单次请求的上限（与后端 PAGE_LIMIT 一致）
export const RECORD_PAGE = 20;
export const RECORD_PAGE_MAX = 50;
export const runKey = (run) => run.id;
export const auditKey = (item) => `${item.at}|${item.action}|${item.description}`;

// 下一批接在已加载的记录后面。两次读取之间新增的记录会把旧记录往后推，重复的按 key 去掉
export function appendRecords(items, batch, keyOf) {
  const seen = new Set(items.map(keyOf));
  return [...items, ...batch.filter((item) => !seen.has(keyOf(item)))];
}

// 轮询结果里记录是否有变化：有变化时列表按已加载的条数重新读取，没变化不发请求
export function recordsSignature(state, tab) {
  if (!state?.runs) return '';
  if (tab === 'settings') return `${state.audit.length}|${state.audit[0]?.at ?? ''}`;
  return `${state.total}|${state.runs.map((run) => [run.id, run.status, run.reason, run.step, run.human_hold,
    run.score, run.after_score, run.evaluation?.status].join(':')).join(',')}`;
}

export const isPending = (run) => ['waiting_optimize', 'waiting_publish', 'waiting_evaluation', 'blocked'].includes(run.status);
export const canRollback = (run) => !!run.snapshot
  && ['published', 'waiting_publish', 'waiting_evaluation', 'blocked'].includes(run.status);
export const canIgnore = (run) => ['waiting_optimize', 'interrupted'].includes(run.status)
  || (run.status === 'blocked' && IGNORABLE.includes(run.reason));
export const canResume = (run) => run.status === 'waiting_optimize' || run.status === 'interrupted'
  || (run.status === 'blocked' && RESUMABLE.includes(run.reason));
export const canEvaluate = (run) => run.status === 'waiting_evaluation'
  || (run.status === 'blocked' && EVALUATION_REASONS.includes(run.reason));
// 评测中的也能跳过（不再等待）：评测任务被删除或迟迟没有结果时由此离开
export const canSkipEvaluation = (run) => canEvaluate(run) || run.status === 'evaluating';
// 删除旧图数据的确认对应一次具体的提示：说明与评测服务给的确认码。提示变了（数据又有变化）要看过新的提示重新勾选
export const graphPrompt = (run) => (run?.graph_confirmation
  ? { text: run.graph_confirmation, code: run.graph_confirmation_code || '' } : null);
export const confirmsPrompt = (choice, run) => {
  const prompt = graphPrompt(run);
  return !!prompt && !!choice && choice.text === prompt.text && choice.code === prompt.code;
};

// 「评测优化」按钮的置灰原因；与后端 optimize.blocker 同口径，后端仍会再校验
export function optimizeBlock(state, run) {
  if (!run) return '运行记录不存在';
  if (!state?.can_write) return '只读评审角色不能执行评测优化';
  if (!canResume(run)) return '本次建议已处理，可在运行记录中查看结果';
  if (!isFeedback(run) && run.release_id !== state.package?.release_id) {
    return `该版本与当前建模项目版本 ${state.package?.version || '未发布'} 不一致，不能评测优化`;
  }
  if (state.active_id) return '项目正在执行任务，请等待本次任务完成';
  if (state.candidate_id && state.candidate_id !== run.id) return '请先处理当前待发布候选内容';
  if (state.assistant_busy) return '建模助手正在运行，请等本轮结束后再优化';
  return '';
}

export function publishBlock(state, run) {
  if (!state?.can_write) return '只读评审角色不能发布';
  if (state.active_id && state.active_id !== run.id) return '项目正在执行任务，请等待本次任务完成';
  return '';
}

// 「确认并评测」的置灰原因；与后端 evaluation.blocker 同口径
export function evaluateBlock(state, run) {
  if (!state?.can_write) return '只读评审角色不能发起评测';
  if (!canEvaluate(run)) return '该运行没有待评测的版本';
  if (state.active_id && state.active_id !== run.id) return '项目正在执行任务，请等待本次任务完成';
  return '';
}

// 两张卡片各自展示的事项：进行中 > 待确认 > 阻断。阻断只看最新一次评测（优化卡片）
// 或当前版本（发布卡片）：旧版本的记录在运行记录里处理
export function optimizeFocus(runs) {
  const running = runs.find((run) => run.status === 'optimizing');
  if (running) return { kind: 'running', run: running };
  const waiting = runs.filter((run) => run.status === 'waiting_optimize');
  if (waiting.length) return { kind: 'waiting', run: waiting[waiting.length - 1], more: waiting.length - 1 };
  const newest = runs.find((run) => run.kind !== 'rollback');
  if (newest?.status === 'blocked' && OPTIMIZE_REASONS.includes(newest.reason)) return { kind: 'blocked', run: newest };
  if (newest?.status === 'passed') return { kind: 'passed', run: newest };
  return { kind: 'idle', run: runs.find((run) => run.changed != null && run.kind !== 'rollback') || null };
}

// 评测阻断属于新发布的版本，其余发布阻断属于优化所基于的版本
const blockedRelease = (run) => (EVALUATION_REASONS.includes(run.reason) ? run.published_release_id
  : run.base_release_id || run.release_id);

export function publishFocus(runs, release) {
  const running = runs.find((run) => ['publishing', 'evaluating'].includes(run.status));
  if (running) return { kind: 'running', run: running };
  const ready = runs.find((run) => run.status === 'waiting_publish');
  if (ready) return { kind: 'waiting', run: ready };
  const evaluate = runs.find((run) => run.status === 'waiting_evaluation');
  if (evaluate) return { kind: 'evaluate', run: evaluate };
  const blocked = runs.find((run) => run.status === 'blocked' && blockedRelease(run) === release
    && [...RETRY_REASONS, ...RECHECK_REASONS, ...EVALUATION_REASONS].includes(run.reason));
  if (blocked) return { kind: 'blocked', run: blocked };
  return { kind: 'idle', run: runs.find((run) => run.status === 'published') || null };
}

export function connection(state) {
  const evaluation = state?.evaluation || {};
  if (!evaluation.configured) return { color: 'gray', text: '未配置评测服务' };
  if (evaluation.reachable === false) return { color: 'amber', text: '评测服务暂不可用' };
  if (!evaluation.notifications) return { color: 'amber', text: '评测通知未配置，依靠定期对账' };
  return { color: 'green', text: '评测通知已连接' };
}

// 评测在评测服务里一跑就是很久，期间不需要密集轮询；进度只在结果到达时变化
export function nextPoll(state, open) {
  const current = state?.runs?.find((run) => run.id === state.active_id);
  const active = (!!state?.active_id && current?.status !== 'evaluating')
    || state?.runs?.some((run) => ['waiting_optimize', 'waiting_evaluation'].includes(run.status) && run.human_hold === false);
  return open || active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
}

// 由飞轮发布的版本评测后建立的运行：说明新版本仍有用例未通过
function nextRound(run) {
  if (run.status === 'optimizing') return ['success', '新版本仍有用例未通过，已自动开始下一轮优化。'];
  if (run.human_hold === false) return ['info', '新版本仍有用例未通过，建模助手空闲后自动开始下一轮优化。'];
  if (run.reason === 'round_limit') return ['info', '新版本仍有用例未通过；已达到连续自动处理上限，等待你确认继续。'];
  return ['info', '新版本仍有用例未通过，等待你确认继续优化。'];
}

function arrival(run) {
  // 飞轮发布的版本全部通过时，由发布它的运行给出提示
  if (run.status === 'passed') return run.source_id ? null : ['success', '评测集全部通过，无需继续优化。'];
  if (run.source_id && run.trigger && ['optimizing', 'waiting_optimize'].includes(run.status)) return nextRound(run);
  if (run.status === 'optimizing') return ['success', '已收到新评测建议，自动优化开始。'];
  if (run.status === 'waiting_optimize') {
    if (run.human_hold === false) return ['info', '已收到新评测建议，建模助手空闲后自动开始优化。'];
    if (run.reason === 'project_busy') return ['info', '已收到通知；项目仍有待处理事项，本次进入待确认。'];
    return ['info', '已收到评测建议，等待你确认继续。'];
  }
  if (run.reason === 'version_mismatch') return ['info', '收到旧版本的评测通知，未执行内容修改。'];
  if (run.reason === 'suggestions_failed') return ['info', '收到通知，优化建议生成失败，等待处理。'];
  return null;
}

function change(before, run) {
  if (run.status === 'passed') return ['success', `${run.id} 最新评测结果全部通过，无需继续处理。`];
  if (before.status === 'optimizing') {
    if (run.status === 'publishing') return ['success', '优化完成，全部检查通过，自动发布开始。'];
    if (run.status === 'waiting_publish') return ['success', '优化完成；请确认是否发布到 Hub。'];
    if (run.reason === 'checks_failed') return ['info', '优化已完成，发布检查未通过，结果已保留。'];
    if (run.reason === 'no_changes') return ['info', '优化已结束，本次未修改知识内容。'];
    if (run.status === 'blocked') return ['error', '优化未正常结束，已保留执行前快照。'];
  }
  if (before.status === 'publishing') {
    const released = `${version(run.target_version)} 已发布到 Hub`;
    if (run.status === 'evaluating') return ['success', `${released}，已自动发起新版本评测。`];
    if (run.status === 'waiting_evaluation') {
      return ['success', run.human_hold === false ? `${released}，即将自动发起评测。` : `${released}；确认后开始评测新版本。`];
    }
    if (run.status === 'published') return ['success', `${released}，完整运行记录已保存。`];
    if (run.reason === 'content_changed') return ['error', '候选内容已变化，未创建新版本。'];
    if (run.reason === 'solver_unverified') return ['error', '求解能力尚未验证，未创建新版本。'];
    if (run.status === 'blocked') return ['error', 'Hub 发布失败，候选内容与发布请求已保留。'];
  }
  if (before.status === 'waiting_evaluation' && run.status === 'evaluating' && run.evaluation?.mode === 'auto') {
    return ['info', '已自动发起新版本评测。'];
  }
  if (['waiting_evaluation', 'evaluating'].includes(before.status) && run.evaluation?.passed) {
    return ['success', `${version(run.target_version)} 通过评测集全部用例，数据飞轮循环结束。`];
  }
  if (before.status === 'evaluating' && run.status === 'blocked') return ['error', '新版本评测未完成，可确认后重新评测。'];
  if (before.status === 'waiting_optimize' && run.status === 'optimizing' && run.mode === 'auto') {
    return ['success', '符合条件的评测建议已开始自动优化。'];
  }
  return null;
}

// 两次同步之间的变化 → 提示；首次读取不提示
export function transitions(previous, next) {
  if (!previous?.runs || !next?.runs) return [];
  const before = new Map(previous.runs.map((run) => [run.id, run]));
  return next.runs
    .map((run) => (before.has(run.id) ? (before.get(run.id).status === run.status ? null : change(before.get(run.id), run))
      : arrival(run)))
    .filter(Boolean);
}

// 界面适配：状态颜色换成 Chip 色调（完成类用青绿），提示类型换成 toast 图标
export const chipTone = (color) => (color === 'green' ? 'teal' : color);
export const NOTICE_ICONS = { success: 'check', info: 'info', error: 'alert' };
// 这些操作会改写工作区：完成后发布页重读版本、发布检查与发布记录
export const CHANGES_WORKSPACE = ['optimize', 'stop', 'rollback'];

const CONFIRMED = {
  optimize: ['optimize', ['success', '已开始评测优化，修订过程可在工作台对话中查看。']],
  publish: ['publish', ['info', '已开始发布到 Hub，完成后在此显示结果。']],
  retry: ['publish', ['info', '已重新提交发布请求。']],
  stop: ['stop', ['info', '本次优化已中止，执行前内容已恢复。']],
  ignore: ['ignore', ['info', '已忽略本次建议，记录已保留。']],
  evaluate: ['evaluate', ['success', '已发起新版本评测，评测结果到达后在此显示。']],
  'skip-evaluation': ['skip-evaluation', ['info', '已跳过本次评测，记录已保留。']],
};

// 确认框点「确认」后调用的运行操作、请求体与成功提示；confirm 为 { kind, run, scope, replaceGraph }
export function confirmPlan({ kind, run, scope, replaceGraph }) {
  if (kind === 'rollback') {
    return {
      name: 'rollback',
      body: { scope },
      notice: scope === 'hub' ? ['info', '建模内容已恢复，正在检查并发布新的恢复版本。']
        : ['success', '建模内容已恢复，已新增回退记录；两个自动化开关已暂停。'],
    };
  }
  if (kind === 'skip-evaluation' && run?.status === 'evaluating') {
    return { name: 'skip-evaluation', body: {}, notice: ['info', '已不再等待本次评测，评测服务中的评测照常进行。'] };
  }
  const [name, notice] = CONFIRMED[kind];
  const body = kind === 'evaluate'
    ? { replace_graph: !!replaceGraph, graph_confirmation_code: replaceGraph?.code || '' } : {};
  return { name, body, notice };
}
