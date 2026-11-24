export const ACTIVE = new Set(['queued', 'preparing', 'running', 'cancelling']);
export const ENDED = new Set(['completed', 'cancelled', 'failed']);
export const RECOVERABLE = new Set(['waiting_auth', 'interrupted']);
export const STATUS = {
  queued: ['排队中', 'neutral'],
  preparing: ['准备中', 'blue'],
  running: ['运行中', 'blue'],
  cancelling: ['停止中', 'amber'],
  completed: ['已完成', 'green'],
  cancelled: ['已停止', 'neutral'],
  failed: ['执行失败', 'red'],
  interrupted: ['已中断', 'amber'],
  waiting_auth: ['待授权', 'amber'],
};
export const VERDICTS = {
  pass: ['通过', 'green', '✓'],
  partial: ['部分通过', 'amber', '◐'],
  fail: ['未通过', 'red', '×'],
  manual: ['待人工', 'neutral', '!'],
  execution_error: ['执行异常', 'red', '!'],
  judge_error: ['判分异常', 'amber', '!'],
  cancelled: ['已取消', 'neutral', '○'],
  executing: ['回答中', 'blue', '◌'],
  answer_ready: ['待判分', 'blue', '◌'],
  unknown: ['核对中', 'amber', '◌'],
  pending: ['待执行', 'neutral', '○'],
};
export const PHASES = [
  ['preparing', '初始化', '校验配置与工作空间'],
  ['package_fetch', '知识包导入', '固定本轮 Hub 版本'],
  ['data_import', '数据源准备', '导入数据与连接检查'],
  ['cases', '问答与判分', '逐条执行并保存结果'],
];
export function caseState(item) {
  return (item.override || item.judgement)?.verdict || item.status || 'pending';
}
export function phaseName(run) {
  return run.phase === 'finished'
    ? '执行已结束'
    : PHASES.find(([key]) => key === run.phase)?.[1] || '等待执行';
}
export function needsAttention(run) {
  return RECOVERABLE.has(run.status) || run.status === 'failed' || !!run.pending_deliveries ||
    !!run.historical_failures || !!(run.summary?.counts?.fail || run.summary?.counts?.partial || run.summary?.counts?.execution_error);
}
export function canResume(run) {
  return RECOVERABLE.has(run.status) && ![
    'configuration_changed',
    'graph_scope_changed',
    'graph_check_required',
    'graph_confirmation_required',
  ].includes(run.error?.code);
}
export function datasetIssue(run) {
  const code = run.error?.code || '';
  if (!code.startsWith('dataset_')) return null;
  const missing = ['dataset_unconfigured', 'dataset_query_unconfigured'].includes(code);
  return {
    title: missing ? '上传数据尚未具备执行条件' : '上传数据准备未完成',
    message: missing
      ? '文件已保存，尚未导入评测数据库。请管理员在“数据源 → 上传数据存储设置”中补齐 MySQL 与查询入口配置，保存后回到本任务点击“恢复并继续”，无需重新上传文件。'
      : run.error.message,
  };
}

export function datasetFieldErrors(form, saved = {}) {
  const errors = {};
  if (!form.host.trim()) errors.host = '请填写 MySQL 主机名或 IP';
  else if (/[\s/@?#]/.test(form.host.trim())) errors.host = '主机地址不含协议、路径或空格';
  const port = Number(form.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) errors.port = '端口必须是 1–65535 的整数';
  if (!form.user.trim()) errors.user = '请填写 MySQL 用户名';
  const unchanged = saved.host === form.host.trim() && Number(saved.port) === port && saved.user === form.user.trim();
  if (!form.password && (!saved.password_set || !unchanged))
    errors.password = saved.password_set ? '连接信息已变更，请重新填写密码' : '首次配置请填写 MySQL 密码';
  if (form.query_url.trim()) {
    try {
      const url = new URL(form.query_url.trim());
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash ||
          !url.pathname.replace(/\/+$/, '').endsWith('/evaluation/api/nl2sql')) throw new Error();
    } catch { errors.query_url = '请填写有效的 HTTP(S) 查询地址，以 /evaluation/api/nl2sql 结尾；标准部署可留空'; }
  }
  return errors;
}
export function packageName(run) {
  return run.package_ref?.name || run.package_ref?.ontology_id || '知识包';
}
// The release the user chose, and the evaluation snapshot that actually ran when the mapping changed.
// Runs created before base_ref was recorded only know the base release ID.
export function packageVersions(run) {
  const ref = run?.package_ref || {};
  const snapshot = ref.kind === 'evaluation_snapshot' ? ref.version || '' : '';
  return {
    base: run?.base_ref?.version || (snapshot ? '' : ref.version || ''),
    baseId: run?.base_ref?.release_id || ref.base_release_id || ref.release_id || '',
    snapshot,
  };
}
// Runs still queued have no started_at yet, so they show when they were created.
// One row per knowledge package: its newest run, plus how many runs the package has.
export function taskRows(items) {
  return items.map((item) => ({ ...item.latest, run_count: item.run_count, package_executing: item.executing, pending_deliveries: item.pending_deliveries, historical_failures: item.historical_failures, history_limited: item.history_limited }));
}
function clock(seconds) {
  const at = runStartTime({ created_at: seconds });
  return at ? `${at.date} ${at.time}` : '';
}
// Date and time to the minute, as delivery records name each other instead of by ID.
export function minute(seconds) {
  const at = runStartTime({ created_at: seconds });
  return at ? `${at.date} ${at.time.slice(0, 5)}` : '';
}
// Which batch the judge model is working on and how long this generation has waited, for a page refreshed every few seconds.
function generationProgress(progress, now) {
  if (!progress) return '';
  const step = progress.merging ? '合并各批结果' : progress.batches > 1 ? `第 ${progress.batch}/${progress.batches} 批` : '';
  const minutes = Math.floor(Math.max(0, now - progress.started_at) / 60);
  return [step, minutes < 1 ? '已等待不到 1 分钟' : `已等待 ${minutes} 分钟`].filter(Boolean).join(' · ');
}
// What the run detail can show of the report and optimization suggestions.
export function artifactState(run, now = Date.now() / 1000) {
  const a = run.artifacts,
    stopped = !ACTIVE.has(run.status);
  if (!stopped)
    return { label: '评测进行中', tone: 'blue', hint: '评测结束后生成评测报告与优化建议', report: false, suggestions: false };
  if (!a)
    return { label: '未生成', tone: 'neutral', hint: '评测结束且有用例结果后自动生成', report: false, suggestions: false, canRegenerate: true };
  const report = a.report === 'ready',
    base = { report, suggestions: a.suggestions === 'ready', canRegenerate: a.status !== 'generating' };
  if (a.status === 'generating') {
    // Suggestions not started yet wait behind other runs' suggestions; reports never wait for them.
    const waiting = a.queue != null ? `评测报告已生成，优化建议排队中，前面还有 ${a.queue} 个` : '评测报告已生成，优化建议生成中';
    const hint = report ? [waiting, generationProgress(a.progress, now)].filter(Boolean).join(' · ') : '';
    return { ...base, label: '生成中', tone: 'blue', hint };
  }
  if (a.status === 'failed') return { ...base, label: '优化建议生成失败', tone: 'amber', hint: report ? '评测报告可查看' : '' };
  // revision counts generations of this run's outputs, not knowledge package versions.
  const at = clock(a.generated_at),
    hint = [at && `生成于 ${at}`, a.revision > 1 && `第 ${a.revision} 次生成`].filter(Boolean).join(' · ');
  // A changed verdict rebuilt the report only; suggestions are regenerated when the user asks.
  if (a.suggestions_stale)
    return {
      ...base,
      label: '判分已修改',
      tone: 'amber',
      hint: [hint, base.suggestions ? '优化建议基于修改前的判分，可点击“重新生成”' : '点击“重新生成”生成优化建议'].join(' · '),
    };
  return { ...base, label: '已生成', tone: 'green', hint };
}
// Delivery of the completion notification to onto-modeling.
export function notificationState(n) {
  if (!n || n.status === 'none') return null;
  if (n.status === 'disabled') return { label: '未配置', tone: 'neutral', hint: '未配置建模通知凭据，建模服务打开页面时自行同步' };
  if (n.status === 'delivered')
    return {
      label: '已送达',
      tone: 'green',
      hint: [clock(n.delivered_at), '已完成评测并通知了建模服务，前往建模服务“已发布版本（知识包评测）”中“优化建议”弹框中可点击知识包优化。'].filter(Boolean).join(' · '),
    };
  if (n.status === 'rejected') return { label: '被拒绝', tone: 'red', hint: n.last_error || '', canRetry: true };
  if (n.status === 'failed')
    return { label: '通知失败', tone: 'red', hint: `已尝试 ${n.attempts} 次（${n.last_error || '未送达'}）`, canRetry: true };
  if (!n.attempts) return { label: '发送中', tone: 'blue', hint: '' };
  return {
    label: '重试中',
    tone: 'amber',
    hint: `已尝试 ${n.attempts} 次（${n.last_error || '未送达'}）${n.next_attempt ? `，下次 ${clock(n.next_attempt)}` : ''}`,
    canRetry: true,
  };
}
// How modeling handled a run's suggestions: written back after it changed the package and published a new version.
// 建模回写的处理结果：发布的新版本，或核对后最新版本已解决（没有发布新版本）
export function handledText(result = {}) {
  if (result.outcome === 'resolved') return `最新版本已解决${result.version ? `（核对 ${result.version}）` : ''}`;
  return `新版本 ${result.version || result.release_id || ''}`;
}
export function optimizationState(run, peers = []) {
  const o = run?.optimization;
  if (!o) return null;
  const result = o.result || {},
    times = (o.history?.length || 0) + (o.result ? 1 : 0),
    when = clock(result.reported_at || o.updated_at),
    count = times > 1 ? `共 ${times} 次回写` : '';
  if (o.status === 'awaiting_review') return { label: '待负责人确认', tone: 'blue', hint: '确认后通知建模' };
  if (o.status === 'notified') {
    const carried = carriedState(run, peers);
    if (carried) return { ...carried, label: carried.label, hint: carried.hint };
    const content = run.delivery?.state;
    if (content === 'none') return { label: '无需优化', tone: 'green', hint: '本轮全部通过，未携带待处理历史内容' };
    if (content === 'solver') return null;
    if (content === 'pending') return { label: '交付内容生成中', tone: 'blue', hint: '报告或建议尚未就绪' };
    if (content === 'diagnose') return { label: '需诊断', tone: 'amber', hint: '存在运行异常或待人工判断，不直接视为知识包错误' };
    const notice = run.notification?.status;
    if ((content || notice) && notice !== 'delivered') return { label: ['failed', 'rejected'].includes(notice) ? '同步失败' : notice === 'disabled' ? '待同步（未配置通知）' : '待同步', tone: 'amber', hint: '尚未确认建模收到通知，请查看交付详情' };
    if (content === 'unknown') return { label: '待核实优化内容', tone: 'amber', hint: '历史记录缺少结构化建议，请查看报告' };
    return {
      label: '待建模处理',
      tone: 'neutral',
      hint: '建模按建议修改并发布新版本后，这里显示新版本',
    };
  }
  if (o.status === 'modeling_done')
    return {
      label: '已处理',
      tone: 'green',
      hint: [handledText(result), when, result.note, count].filter(Boolean).join(' · '),
      releaseId: result.release_id,
    };
  if (o.status === 'modeling_failed')
    return { label: '处理失败', tone: 'red', hint: [result.note || '建模处理失败', when, count].filter(Boolean).join(' · ') };
  if (o.status === 'dismissed') return { label: '已驳回', tone: 'neutral', hint: o.reason || '负责人驳回，不交给建模' };
  return null;
}
// A run still waiting for modeling rides along with a newer push of the same source, or was left out of its window.
export function carriedState(run, peers = []) {
  const o = run?.optimization;
  if (o?.status !== 'notified') return null;
  if (o.carried_into)
    return {
      label: `已合并到 ${shortId(o.carried_into.run_id, peers)} 编号的交付`,
      tone: 'blue',
      runId: o.carried_into.run_id,
      hint: '建模还没处理，随较新的一次交付一起交给建模，处理结果会一起回写',
    };
  if (o.slid_out)
    return {
      label: '未能一起带上',
      tone: 'amber',
      runId: o.slid_out.run_id,
      hint: `${shortId(o.slid_out.run_id, peers)} 编号的交付文件超过大小上限，没有带上这一轮，仍待建模处理`,
    };
  return null;
}
// What a push carries from earlier runs still waiting for modeling: runs, questions and suggestions, and runs left out.
export function carryoverCounts(carryover) {
  const rounds = carryover?.rounds || [];
  return {
    rounds: rounds.length,
    cases: rounds.reduce((sum, r) => sum + (r.cases || 0), 0),
    items: rounds.reduce((sum, r) => sum + (r.items || 0), 0),
    slidOut: (carryover?.slid_out || []).length,
  };
}
// Where a feedback run stands: collecting, waiting for an owner, handed to modeling, or dismissed.
export function feedbackStatus(run) {
  if (run.status === 'collecting') return { label: '收集中', tone: 'blue' };
  if (!run.artifacts) return { label: '生成报告中', tone: 'blue' };
  return optimizationState(run) || { label: '已生成', tone: 'green' };
}
export function runStartTime(run) {
  const seconds = run?.started_at || run?.created_at;
  if (!seconds) return null;
  const at = new Date(seconds * 1000),
    pad = (value) => String(value).padStart(2, '0');
  return {
    date: `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`,
    time: `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`,
  };
}
// Why a run cannot be judged yet, shown when starting instead of silently disabling the button.
export function judgeMissingReason(judge) {
  if (judge?.source === 'disabled')
    return '管理员已停用默认判分模型，你还没有配置自己的判分模型，评测回答将无法判分。请先在“评测判分模型配置”中填写你自己的判分模型，再开始评测。';
  return '当前没有可用的评测判分模型，评测回答将无法判分。请先在“评测判分模型配置”中填写你自己的判分模型，或请管理员配置默认判分模型，再开始评测。';
}
export function packageIds(runs) {
  return [...new Set(runs.map((r) => r.package_ref?.ontology_id).filter(Boolean))].sort();
}
// The modeling-projects endpoint accepts at most 20 packages per request.
export function modelingProjectQueries(ids, size = 20) {
  const queries = [];
  for (let i = 0; i < ids.length; i += size) {
    queries.push(ids.slice(i, i + size).map((id) => `ontology_id=${encodeURIComponent(id)}`).join('&'));
  }
  return queries;
}
export function modelingProjectLabel(entry) {
  if (!entry?.projects?.length) return '—';
  return entry.total > 1 ? `${entry.projects[0]} 等 ${entry.total} 个` : entry.projects[0];
}
// Questions of one run sent to consumer at the same time; the service accepts 1 to 8.
export const DEFAULT_CONCURRENCY = 4;
export const MAX_CONCURRENCY = 16;
export function modelOptionLabel(item) {
  const name = item.name || item.id;
  return `${name}${item.model && item.model !== name ? ` · ${item.model}` : ''}${item.is_default ? '（默认）' : ''}`;
}
// Answering model choices grouped by consumer pool; a pool without enabled models is not shown.
export function modelOptionGroups(items = []) {
  const personal = (item) => item.scope === 'user';
  return [
    { label: '全局模型池', items: items.filter((item) => !personal(item)) },
    { label: '个人模型池', items: items.filter(personal) },
  ].filter((group) => group.items.length);
}
export function shortId(id = '', peers = []) {
  let length = 12;
  while (length < id.length && peers.some((other) => other !== id && other.slice(0, length) === id.slice(0, length))) length += 4;
  return id.slice(0, length);
}
// A run's cases by verdict for the result bar: execution and judge errors count as errors, unrun cases as todo.
export function resultCounts(summary) {
  const c = summary?.counts || {};
  return {
    pass: c.pass || 0,
    partial: c.partial || 0,
    fail: c.fail || 0,
    error: (c.execution_error || 0) + (c.judge_error || 0),
    manual: c.manual || 0,
    todo: (c.pending || 0) + (c.cancelled || 0),
  };
}
// The two numbers shown for a run: the report's score (pass 1, partial 0.5, over valid cases) and the pass rate
// (passed over all cases; partial, failed, error and manual cases do not pass). Feedback runs have neither.
export function scoreText(summary) {
  return summary?.score == null ? '—' : String(summary.score);
}
export function passRate(summary) {
  return summary?.strict_pass_rate == null ? '—' : `${Math.round(summary.strict_pass_rate * 100)}%`;
}
// Which consumer workspace a data source's run uses, as the wizard explains it.
export function sourceWorkspace(source) {
  if (!source || source.mode !== 'connection') return '使用评测专用空间';
  return source.provider === 'neo4j' ? '使用 Workspace 对应的空间' : '使用你填写的评测工作空间';
}
// How long a stopped run took, from start to finish.
export function duration(run) {
  const start = run?.started_at || run?.created_at,
    end = run?.finished_at;
  if (!start || !end || end < start) return '';
  const minutes = Math.floor((end - start) / 60),
    seconds = Math.round(end - start - minutes * 60);
  return minutes ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`;
}
export function parseRoute(hash = '') {
  const [path, search = ''] = hash.replace(/^#/, '').split('?');
  const match = /^\/runs\/([a-zA-Z0-9_-]+)$/.exec(path);
  if (match) return { page: 'run', id: match[1] };
  if (path === '/new') {
    const query = new URLSearchParams(search),
      release = query.get('release_id');
    // A release with "from": re-evaluate that run's settings on another version of its package.
    return { page: 'new', from: query.get('from') || '', ...(release ? { release } : {}), ...(query.get('run_type') === 'solver' ? { runType: 'solver' } : {}) };
  }
  if (path === '/eval-sets') return { page: 'sets' };
  if (path === '/solver-sets') return { page: 'solverSets' };
  if (path === '/data-sources') return { page: 'sources' };
  const definition = /^\/(eval-sets|solver-sets|data-sources)\/([a-zA-Z0-9_-]+)$/.exec(path);
  if (definition)
    return {
      page: { 'eval-sets': 'sets', 'solver-sets': 'solverSets', 'data-sources': 'sources' }[definition[1]],
      id: definition[2],
    };
  if (path === '/settings') return { page: 'settings' };
  if (path === '/feedback') return { page: 'feedback' };
  const feedbackPackage = /^\/feedback\/package\/([A-Za-z0-9_-]+)$/.exec(path);
  if (feedbackPackage) return { page: 'feedback', package: feedbackPackage[1] };
  const feedback = /^\/feedback\/([a-zA-Z0-9_-]+)$/.exec(path);
  if (feedback) return { page: 'feedback', id: feedback[1] };
  return { page: 'tasks' };
}
export function parseCases(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('文件不是有效的 JSON，请检查格式');
  }
  const items = Array.isArray(value) ? value : value?.cases;
  if (!Array.isArray(items))
    throw new Error('文件格式不正确：最外层应为用例数组，或包含 cases 数组的对象');
  if (!items.length || items.length > 500)
    throw new Error(`评测集需要 1-500 条用例，当前文件有 ${items.length} 条`);
  return items.map((item, i) => {
    if (typeof item?.question !== 'string' || !item.question.trim())
      throw new Error(`用例 ${i + 1} 缺少问题`);
    const reference = item.reference ?? item.groundTruth ?? '';
    if (typeof reference !== 'string' || item.question.length > 10000 || reference.length > 20000)
      throw new Error(`用例 ${i + 1} 内容格式或长度无效`);
    const mode = item.execution_mode ?? 'auto';
    if (!['auto', 'business_query', 'reasoning_only'].includes(mode))
      throw new Error(`用例 ${i + 1} 的执行要求无效`);
    return { question: item.question.trim(), reference, ...(mode !== 'auto' ? { execution_mode: mode } : {}) };
  });
}
// Solver contract validation runs: deterministic assertions on one solver call per case, no Judge.
export const RUN_TYPES = { data: '数据评测', solver: '求解验证' };
export const SOLVER_MAX_CASES = 20;
const SOLVER_KEY = /^[a-z][a-z0-9_]{0,31}$/;
const SOLVER_CASE_ID = /^[A-Za-z0-9_-]{1,64}$/;
export function isSolverRun(run) {
  return run?.run_type === 'solver';
}
// Default and maximum questions sent to consumer at once; solver jobs are heavier than plain questions.
export function concurrencyLimits(runType) {
  return runType === 'solver' ? { default: 1, max: 4 } : { default: DEFAULT_CONCURRENCY, max: MAX_CONCURRENCY };
}
export function emptySolverCase(index = 0, solverKey = '') {
  return { id: `case_${index + 1}`, name: '', solver_key: solverKey, argumentsText: '{}', question: '', expected: '' };
}
// Editor rows keep arguments as text so a half-typed JSON object is not lost.
export function solverCaseRows(cases = []) {
  return cases.map((item) => ({
    id: item.id,
    name: item.name || '',
    solver_key: item.solver_key || '',
    argumentsText: JSON.stringify(item.arguments ?? {}, null, 2),
    question: item.question || '',
    expected: item.expected_candidate_count ?? '',
  }));
}
// The API payload of edited rows; throws the first problem with its row number.
export function solverCasePayload(rows) {
  if (!rows.length || rows.length > SOLVER_MAX_CASES) throw new Error(`求解验证集需要 1-${SOLVER_MAX_CASES} 条用例`);
  const seen = new Set();
  return rows.map((row, i) => {
    const label = `用例 ${i + 1}`;
    const id = String(row.id || '').trim();
    if (!SOLVER_CASE_ID.test(id) || seen.has(id)) throw new Error(`${label} 的标识需为字母、数字、下划线或短横线且不重复`);
    seen.add(id);
    const name = String(row.name || '').trim();
    if (!name || name.length > 80) throw new Error(`${label} 需要 1-80 字的名称`);
    if (!SOLVER_KEY.test(row.solver_key || '')) throw new Error(`${label} 请选择或填写有效的求解操作`);
    let args;
    try {
      args = JSON.parse(row.argumentsText || '{}');
    } catch {
      throw new Error(`${label} 的入参不是有效的 JSON`);
    }
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error(`${label} 的入参须为 JSON 对象`);
    const item = { id, name, solver_key: row.solver_key, arguments: args };
    if (String(row.question || '').trim()) item.question = row.question.trim();
    if (row.expected !== '' && row.expected != null) {
      const count = Number(row.expected);
      if (!Number.isInteger(count) || count < 0 || count > 100000) throw new Error(`${label} 的期望候选数须为 0-100000 的整数`);
      item.expected_candidate_count = count;
    }
    return item;
  });
}
// Accepts a case array or {cases: [...]}, including cases exported from the earlier modeling-side validation.
export function parseSolverCases(text) {
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('文件不是有效的 JSON，请检查格式');
  }
  const items = Array.isArray(value) ? value : value?.cases;
  if (!Array.isArray(items)) throw new Error('文件格式不正确：最外层应为用例数组，或包含 cases 数组的对象');
  return solverCaseRows(
    solverCasePayload(
      items.map((item, i) => ({
        id: item?.id ?? `case_${i + 1}`,
        name: item?.name ?? item?.id ?? `case_${i + 1}`,
        solver_key: item?.solver_key ?? '',
        argumentsText: JSON.stringify(item?.arguments ?? {}),
        question: item?.question ?? '',
        expected: item?.expected_candidate_count ?? '',
      })),
    ),
  );
}
export const ASSERTION_TITLES = {
  solver_tool_called: '调用了对应的求解工具',
  arguments_match: '入参与用例一致',
  completed: '得到求解结果',
  authoritative: '结果为权威求解结果',
  evidence: '权威结果证据齐全',
  candidate_count: '候选数符合期望',
};
export function assertionNote(item) {
  if (item.name === 'candidate_count') return `期望 ${item.expected}，实际 ${item.actual ?? '未读到'}`;
  return [...(item.diff || []), ...(item.missing || [])].join('、');
}
export function sourceSummary(source) {
  if (!source || source.mode === 'none') return '仅验证知识包结构与语义，不查询数据';
  if (source.mode === 'dataset') return `上传数据 · ${source.input_ids?.length || 0} 个文件`;
  return `${source.provider === 'neo4j' ? 'Neo4j 图库' : 'NL2SQL 外部连接'}${source.import_data ? ' · 评测前导入图数据' : ' · 查询已有数据'}`;
}
// 新建评测中哪一步预览数据源：上传的数据在选择数据源时预览映射；导入图数据的 Neo4j 连接到准备与确认时
// 再预览图导入（实体、实例与关系），选择数据源时只填隔离范围。0 表示不预览
export function previewStep(source) {
  if (source?.mode === 'dataset') return 1;
  return source?.mode === 'connection' && source.provider === 'neo4j' && source.import_data ? 3 : 0;
}
// Same Neo4j scope properties as the consumer graph backend.
export const GRAPH_SCOPE_DEFAULTS = { workspace_property: 'd2o_workspace_id', ontology_property: 'd2o_ontology_id' };
// 外部连接的评测隔离范围：在新建评测选中数据源时填写，随数据源保存；规则与后端 source_config 一致
const SCOPE_VALUE = /^[a-z][a-z0-9_-]{0,63}$/;
const SCOPE_IDENTIFIER = /^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{0,63}$/;
const SCOPE_VALUE_RULE = '须以小写字母开头，最多 64 个小写字母、数字、连字符或下划线';
export const needsScope = (source) => source?.mode === 'connection';
export function scopeOf(source) {
  const view = source?.connection_view || {};
  if (source?.provider === 'neo4j') {
    return {
      workspace_property: view.workspace_property || GRAPH_SCOPE_DEFAULTS.workspace_property,
      workspace_value: view.workspace_value || '',
      ontology_property: view.ontology_property ?? GRAPH_SCOPE_DEFAULTS.ontology_property,
    };
  }
  return { evaluation_workspace_id: view.evaluation_workspace_id || '' };
}
// 字段 → 问题说明；空对象表示可以保存与评测
export function scopeErrors(source, values) {
  const errors = {};
  const value = (key) => String(values[key] || '').trim();
  if (source?.provider !== 'neo4j') {
    if (!value('evaluation_workspace_id')) errors.evaluation_workspace_id = '请填写评测工作空间 ID';
    else if (!SCOPE_VALUE.test(value('evaluation_workspace_id'))) errors.evaluation_workspace_id = SCOPE_VALUE_RULE;
    return errors;
  }
  if (!value('workspace_property')) errors.workspace_property = '请填写 Workspace 隔离字段';
  else if (!SCOPE_IDENTIFIER.test(value('workspace_property'))) errors.workspace_property = '字段名格式无效';
  if (!value('workspace_value')) errors.workspace_value = '请填写 Workspace 对应值';
  else if (!SCOPE_VALUE.test(value('workspace_value'))) errors.workspace_value = SCOPE_VALUE_RULE;
  if (!value('ontology_property')) {
    if (source.import_data) errors.ontology_property = '导入图数据需要 Ontology 隔离字段';
  } else if (!SCOPE_IDENTIFIER.test(value('ontology_property'))) errors.ontology_property = '字段名格式无效';
  else if (value('ontology_property') === value('workspace_property'))
    errors.ontology_property = '不能与 Workspace 隔离字段相同';
  return errors;
}
export function emptySource() {
  return {
    name: '',
    mode: 'connection',
    provider: 'nl2sql_http',
    input_ids: [],
    inputs: [],
    import_data: false,
    connection: {
      url: '',
      scene: '',
      token: '',
      evaluation_workspace_id: '',
      uri: '',
      database: 'neo4j',
      username: '',
      password: '',
      workspace_property: GRAPH_SCOPE_DEFAULTS.workspace_property,
      workspace_value: '',
      ontology_property: GRAPH_SCOPE_DEFAULTS.ontology_property,
    },
    secret_configured: false,
  };
}
export function sourceFromRecord(record) {
  return {
    ...emptySource(),
    ...record,
    connection: { ...emptySource().connection, ...record.connection_view },
    inputs: record.inputs || [],
  };
}
export function sourcePayload(form) {
  const graph = form.provider === 'neo4j';
  const keys = graph
    ? [
        'uri',
        'database',
        'username',
        'password',
        'workspace_property',
        'workspace_value',
        'ontology_property',
      ]
    : ['url', 'scene', 'token', 'evaluation_workspace_id'];
  const connection = Object.fromEntries(
    keys
      .map((k) => [k, String(form.connection[k] || '').trim()])
      .filter(([k, v]) => v || !['token', 'password'].includes(k)),
  );
  return {
    name: form.name.trim(),
    mode: form.mode,
    provider: form.mode === 'dataset' ? 'nl2sql_http' : form.provider,
    input_ids: form.mode === 'dataset' || (graph && form.import_data) ? form.input_ids : [],
    import_data: form.mode === 'connection' && graph && form.import_data,
    ...(form.mode === 'connection' ? { connection } : {}),
    ...(form.id ? { revision: form.revision } : {}),
  };
}
export function packageReference(ref) {
  return {
    ontology_id: ref.ontology_id,
    release_id: ref.release_id,
    package_digest: ref.package_digest || '',
  };
}
