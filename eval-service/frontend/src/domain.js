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
  return RECOVERABLE.has(run.status) || run.status === 'failed';
}
export function canResume(run) {
  return RECOVERABLE.has(run.status) && ![
    'configuration_changed',
    'graph_scope_changed',
    'graph_check_required',
    'graph_confirmation_required',
  ].includes(run.error?.code);
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
  return items.map((item) => ({ ...item.latest, run_count: item.run_count, package_executing: item.executing }));
}
function clock(seconds) {
  const at = runStartTime({ created_at: seconds });
  return at ? `${at.date} ${at.time}` : '';
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
  if (n.status === 'delivered') return { label: '已送达', tone: 'green', hint: clock(n.delivered_at) };
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
export function optimizationState(run) {
  const o = run?.optimization;
  if (!o) return null;
  const result = o.result || {},
    times = (o.history?.length || 0) + (o.result ? 1 : 0),
    when = clock(result.reported_at || o.updated_at),
    count = times > 1 ? `共 ${times} 次回写` : '';
  if (o.status === 'awaiting_review') return { label: '待负责人确认', tone: 'blue', hint: '确认后通知建模' };
  if (o.status === 'notified')
    return { label: '待建模处理', tone: 'neutral', hint: '建模按建议修改并发布新版本后，这里显示新版本' };
  if (o.status === 'modeling_done')
    return {
      label: '已处理',
      tone: 'green',
      hint: [`新版本 ${result.version || result.release_id}`, when, result.note, count].filter(Boolean).join(' · '),
      releaseId: result.release_id,
    };
  if (o.status === 'modeling_failed')
    return { label: '处理失败', tone: 'red', hint: [result.note || '建模处理失败', when, count].filter(Boolean).join(' · ') };
  if (o.status === 'dismissed') return { label: '已驳回', tone: 'neutral', hint: o.reason || '负责人驳回，不交给建模' };
  return null;
}
// Where a feedback run stands: collecting, waiting for an owner, handed to modeling, or dismissed.
export function feedbackStatus(run) {
  if (run.status === 'collecting') return { label: '收集中', tone: 'blue' };
  if (!run.artifacts) return { label: '生成报告中', tone: 'blue' };
  return optimizationState(run) || { label: '已生成', tone: 'green' };
}
// Short mark in run lists: the new version modeling published from this run's suggestions, or that it failed.
export function optimizationTag(run) {
  const o = run?.optimization;
  if (o?.status === 'modeling_done') return { label: `建模已处理 → ${o.result?.version || '新版本'}`, tone: 'green' };
  if (o?.status === 'modeling_failed') return { label: '建模处理失败', tone: 'red' };
  return null;
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
export function shortId(id = '') {
  return id.length > 12 ? id.slice(0, 12) : id;
}
export function percent(value) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`;
}
export function parseRoute(hash = '') {
  const [path, search = ''] = hash.replace(/^#/, '').split('?');
  const match = /^\/runs\/([a-zA-Z0-9_-]+)$/.exec(path);
  if (match) return { page: 'run', id: match[1] };
  if (path === '/new') {
    const query = new URLSearchParams(search),
      release = query.get('release_id');
    // A release with "from": re-evaluate that run's settings on another version of its package.
    return { page: 'new', from: query.get('from') || '', ...(release ? { release } : {}) };
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
    return { question: item.question.trim(), reference };
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
// Same Neo4j scope properties as the consumer graph backend.
export const GRAPH_SCOPE_DEFAULTS = { workspace_property: 'd2o_workspace_id', ontology_property: 'd2o_ontology_id' };
export function emptySource() {
  return {
    name: '',
    mode: 'connection',
    provider: 'nl2sql_http',
    input_ids: [],
    inputs: [],
    tables: {},
    schema: [],
    inspection_id: '',
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
    inspection_id: record.inspection_id || '',
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
    ...(form.mode === 'connection'
      ? { connection, ...(graph ? {} : { inspection_id: form.inspection_id }) }
      : {}),
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
