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
  return run.package_ref?.manifest_id || run.package_ref?.hub_package_id || '知识包';
}
export function packageIds(runs) {
  return [...new Set(runs.map((r) => r.package_ref?.hub_package_id).filter(Boolean))].sort();
}
// The modeling-projects endpoint accepts at most 20 packages per request.
export function modelingProjectQueries(ids, size = 20) {
  const queries = [];
  for (let i = 0; i < ids.length; i += size) {
    queries.push(ids.slice(i, i + size).map((id) => `hub_package_id=${encodeURIComponent(id)}`).join('&'));
  }
  return queries;
}
export function modelingProjectLabel(entry) {
  if (!entry?.projects?.length) return '—';
  return entry.total > 1 ? `${entry.projects[0]} 等 ${entry.total} 个` : entry.projects[0];
}
// Questions of one run sent to consumer at the same time; the service accepts 1 to 8.
export const DEFAULT_CONCURRENCY = 4;
export const MAX_CONCURRENCY = 8;
export function modelOptionLabel(item) {
  const name = item.name || item.id;
  return `${name}${item.model && item.model !== name ? ` · ${item.model}` : ''}${item.is_default ? '（默认）' : ''}`;
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
  if (path === '/new') return { page: 'new', from: new URLSearchParams(search).get('from') || '' };
  if (path === '/eval-sets') return { page: 'sets' };
  if (path === '/data-sources') return { page: 'sources' };
  const definition = /^\/(eval-sets|data-sources)\/([a-zA-Z0-9_-]+)$/.exec(path);
  if (definition)
    return { page: definition[1] === 'eval-sets' ? 'sets' : 'sources', id: definition[2] };
  if (path === '/settings') return { page: 'settings' };
  return { page: 'tasks' };
}
export function parseCases(text) {
  let items;
  try {
    items = JSON.parse(text);
  } catch {
    throw new Error('文件不是有效的 JSON，请检查格式');
  }
  if (!Array.isArray(items) || !items.length || items.length > 500)
    throw new Error('评测集需要 1-500 条用例');
  return items.map((item, i) => {
    if (typeof item?.question !== 'string' || !item.question.trim())
      throw new Error(`用例 ${i + 1} 缺少问题`);
    const reference = item.reference ?? item.groundTruth ?? '';
    if (typeof reference !== 'string' || item.question.length > 10000 || reference.length > 20000)
      throw new Error(`用例 ${i + 1} 内容格式或长度无效`);
    return { question: item.question.trim(), reference };
  });
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
    mode: 'dataset',
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
    hub_package_id: ref.hub_package_id,
    release_id: ref.release_id,
    package_digest: ref.package_digest || '',
  };
}
