// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
const WINDOW_PRESETS = Object.freeze({
  '1h': 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
});

export const OBSERVABILITY_VIEWS = Object.freeze({
  SESSION: 'session',
  GLOBAL: 'global',
});

export const EMPTY_OBSERVABILITY_FILTERS = Object.freeze({
  workspace_id: '',
  status: '',
  model_provider: '',
  agent_mode: '',
});

const PERF_EVENT_LABELS = {
  'run.started': '运行开始',
  'run.finished': '运行完成',
  'run.failed': '运行失败',
  'run.cancelled': '运行取消',
  'run.summary': '运行汇总',
  'agent_loop.started': '智能体循环开始',
  'agent_loop.finished': '智能体循环结束',
  'agent_iteration.prepared': '迭代准备完成',
  'agent_iteration.finished': '迭代完成',
  'llm.started': 'LLM 请求开始',
  'llm.finished': 'LLM 响应完成',
  'llm.failed': 'LLM 请求失败',
  'tool_batch.started': '工具批次开始',
  'tool_batch.finished': '工具批次完成',
  'tool_batch.failed': '工具批次失败',
  'tool.started': '工具调用开始',
  'tool.progress': '工具调用进度',
  'tool.finished': '工具调用完成',
  'runtime_phase.started': '运行阶段开始',
  'runtime_phase.finished': '运行阶段完成',
  'runtime_phase.failed': '运行阶段失败',
};

const PERF_KIND_LABELS = {
  run: '运行',
  run_summary: '运行汇总',
  agent_loop: '智能体循环',
  agent_iteration: '迭代',
  llm_call: 'LLM 调用',
  tool_batch: '工具批次',
  tool_call: '工具调用',
  runtime_phase: '运行阶段',
};

const PERF_COMPONENT_LABELS = {
  orchestrator: '编排器',
  agent_loop: '智能体循环',
  tool_executor: '工具执行器',
  memory: '记忆',
  observability: '观测',
};

const PERF_PHASE_LABELS = {
  stream_events_batch_save: '批量保存流事件',
  chat_session_compact: '会话压缩',
  llm_context_save: '保存 LLM 上下文',
  turn_complete: '轮次收尾',
  run_complete: '运行收尾',
  turn_finished_emit: '发送轮次完成事件',
  memory_append_assistant_message: '写入助手消息记忆',
  memory_append_turn_summary: '写入轮次摘要记忆',
  memory_commit_session: '提交会话记忆',
};

const PERF_CALLER_LABELS = {
  main: '主智能体',
  subagent: '子智能体',
};

const PERF_AGENT_MODE_LABELS = {
  quick: '快速模式',
  thinking: '思考模式',
  multi_agent: '多智能体模式',
};

const PERF_TOOL_LABELS = {
  ontology_context: '本体上下文',
  skill: '技能加载',
  business_data_search: '业务数据查询',
  web_search: '联网搜索',
  execute_script: '执行脚本',
  memory: '记忆写入',
  spawn_sub_agent: '派生子智能体',
};

const asObject = (value) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const asArray = (value) => (Array.isArray(value) ? value : []);
const cleanText = (value) => String(value ?? '').trim();

export function resolveObservabilityAccess({
  view,
  sessionId,
  user,
  canViewGlobal,
} = {}) {
  const normalizedView =
    view === OBSERVABILITY_VIEWS.GLOBAL ?
      OBSERVABILITY_VIEWS.GLOBAL :
      OBSERVABILITY_VIEWS.SESSION;
  if (!user) {
    return {
      allowed: false,
      view: normalizedView,
      reason: 'unauthenticated',
      title: '需要登录',
      detail: '登录后才能查看可观测性数据。',
    };
  }
  if (normalizedView === OBSERVABILITY_VIEWS.GLOBAL) {
    const permitted =
      typeof canViewGlobal === 'boolean' ?
        canViewGlobal :
        user.role === 'admin';
    if (!permitted) {
      return {
        allowed: false,
        view: normalizedView,
        reason: 'forbidden',
        title: '无权查看全局观测',
        detail: '全局运行总览仅对具有 observability.global 权限的用户开放。',
      };
    }
  }
  if (normalizedView === OBSERVABILITY_VIEWS.SESSION && !cleanText(sessionId)) {
    return {
      allowed: false,
      view: normalizedView,
      reason: 'missing-session',
      title: '请先选择会话',
      detail: '选择会话后可查看该会话的运行性能与调用链。',
    };
  }
  return {
    allowed: true,
    view: normalizedView,
    reason: '',
    title: '',
    detail: '',
  };
}

export function createPerformanceWindowQuery(windowId, endMs = Date.now()) {
  const durationMs = WINDOW_PRESETS[windowId] || WINDOW_PRESETS['24h'];
  const parsedEnd = Number(endMs);
  const safeEnd =
    Number.isFinite(parsedEnd) && parsedEnd > 0 ?
      Math.round(parsedEnd) :
      Date.now();
  return new URLSearchParams({
    from: String(safeEnd - durationMs),
    to: String(safeEnd),
  }).toString();
}

export function normalizeObservabilityFilters(filters = {}) {
  return Object.fromEntries(
    Object.keys(EMPTY_OBSERVABILITY_FILTERS).map((key) => [
      key,
      cleanText(filters?.[key]),
    ]),
  );
}

export function appendObservabilityFilters(query, filters = {}) {
  const params = new URLSearchParams(query);
  Object.entries(normalizeObservabilityFilters(filters)).forEach(
    ([key, value]) => {
      if (value) {
        params.set(key, value);
      }
    },
  );
  return params.toString();
}

export function buildSessionPerformanceUrl(sessionId, { limitRuns = 50 } = {}) {
  const params = new URLSearchParams();
  const limit = Math.min(Math.max(Number(limitRuns) || 50, 1), 200);
  params.set('limit_runs', String(limit));
  return `/agentic/sessions/${encodeURIComponent(cleanText(sessionId))}/performance?${params}`;
}

export function buildGlobalObservabilityUrls({
  windowId = '24h',
  endMs = Date.now(),
  filters = EMPTY_OBSERVABILITY_FILTERS,
  limit = 50,
  offset = 0,
} = {}) {
  const query = appendObservabilityFilters(
    createPerformanceWindowQuery(windowId, endMs),
    filters,
  );
  const runsParams = new URLSearchParams(query);
  const normalizedOffset = Math.max(Number(offset) || 0, 0);
  // The current route accepts a one-based offset and converts it back to the
  // zero-based offset returned by the service. Keep zero as zero for page one.
  const requestOffset = normalizedOffset > 0 ? normalizedOffset + 1 : 0;
  runsParams.set(
    'limit',
    String(Math.min(Math.max(Number(limit) || 50, 1), 200)),
  );
  runsParams.set('offset', String(requestOffset));
  return {
    query,
    overview: `/api/local-observability/overview?${query}`,
    runs: `/api/local-observability/runs?${runsParams}`,
  };
}

export function buildRunDetailUrl({
  runId,
  sessionId = '',
  view = OBSERVABILITY_VIEWS.SESSION,
  query = '',
  workspaceId = '',
} = {}) {
  const params = new URLSearchParams();
  if (view === OBSERVABILITY_VIEWS.SESSION) {
    params.set('session_id', cleanText(sessionId));
  } else {
    const windowParams = new URLSearchParams(query);
    for (const key of ['from', 'to']) {
      if (windowParams.get(key)) {
        params.set(key, windowParams.get(key));
      }
    }
    const scopedWorkspaceId =
      cleanText(workspaceId) || cleanText(windowParams.get('workspace_id'));
    if (scopedWorkspaceId) {
      params.set('workspace_id', scopedWorkspaceId);
    }
  }
  return `/agentic/performance/runs/${encodeURIComponent(cleanText(runId))}?${params}`;
}

export function normalizeSessionAnalysis(payload) {
  const body = asObject(payload);
  return {
    ...body,
    summary: asObject(body.summary),
    trends: {
      ...asObject(body.trends),
      turns: asArray(asObject(body.trends).turns),
    },
    runs: asArray(body.runs),
    diagnostics: asObject(body.diagnostics),
    window: asObject(body.window),
  };
}

export function normalizeGlobalOverview(payload) {
  const body = asObject(payload);
  return {
    ...body,
    summary: asObject(body.summary),
    trends: asArray(body.trends),
    duration_buckets: asArray(body.duration_buckets),
    slow_runs: asArray(body.slow_runs),
    tools: asArray(body.tools),
    runtime_phases: asArray(body.runtime_phases),
    failures: asArray(body.failures),
    diagnostics: asObject(body.diagnostics),
    window: asObject(body.window),
  };
}

export function normalizeGlobalRuns(payload) {
  const body = asObject(payload);
  return {
    ...body,
    count: Math.max(Number(body.count) || 0, 0),
    limit: Math.min(Math.max(Number(body.limit) || 50, 1), 200),
    offset: Math.max(Number(body.offset) || 0, 0),
    runs: asArray(body.runs),
    diagnostics: asObject(body.diagnostics),
  };
}

export function normalizeRunDetail(payload) {
  const body = asObject(payload);
  return {
    ...body,
    run: Object.keys(asObject(body.run)).length ? asObject(body.run) : null,
    breakdown: asObject(body.breakdown),
    iterations: asArray(body.iterations),
    iteration_calls: asArray(body.iteration_calls),
    llm_calls: asArray(body.llm_calls),
    tools: asArray(body.tools),
    runtime_phases: asArray(body.runtime_phases),
    timeline: asArray(body.timeline),
    diagnostics: asObject(body.diagnostics),
  };
}

export function selectRunId(currentId, ...collections) {
  const rows = collections.flatMap((collection) => asArray(collection));
  const normalizedCurrent = cleanText(currentId);
  if (
    normalizedCurrent &&
    rows.some((row) => cleanText(row?.run_id) === normalizedCurrent)
  ) {
    return normalizedCurrent;
  }
  return cleanText(rows.find((row) => cleanText(row?.run_id))?.run_id);
}

export function findRunById(runId, ...collections) {
  const normalizedRunId = cleanText(runId);
  return (
    collections
      .flatMap((collection) => asArray(collection))
      .find((row) => cleanText(row?.run_id) === normalizedRunId) || null
  );
}

export function detailForSelectedRun(detail, runId) {
  const selectedRunId = cleanText(runId);
  const detailRunId = cleanText(asObject(asObject(detail).run).run_id);
  return selectedRunId && detailRunId === selectedRunId ? detail : null;
}

export function resolveCollectionState({
  loading = false,
  error = '',
  itemCount = 0,
} = {}) {
  const count = Math.max(Number(itemCount) || 0, 0);
  if (loading && count === 0) {
    return 'loading';
  }
  if (error && count === 0) {
    return 'error';
  }
  if (count === 0) {
    return 'empty';
  }
  if (error) {
    return 'stale-error';
  }
  return 'ready';
}

export function getPageInfo({ count = 0, limit = 50, offset = 0 } = {}) {
  const safeCount = Math.max(Number(count) || 0, 0);
  const safeLimit = Math.max(Number(limit) || 50, 1);
  const lastPageOffset = safeCount ?
    Math.floor((safeCount - 1) / safeLimit) * safeLimit :
    0;
  const safeOffset = Math.min(Math.max(Number(offset) || 0, 0), lastPageOffset);
  return {
    page: safeCount ? Math.floor(safeOffset / safeLimit) + 1 : 1,
    pageCount: Math.max(Math.ceil(safeCount / safeLimit), 1),
    previousOffset: Math.max(safeOffset - safeLimit, 0),
    nextOffset:
      safeOffset + safeLimit < safeCount ? safeOffset + safeLimit : safeOffset,
    hasPrevious: safeOffset > 0,
    hasNext: safeOffset + safeLimit < safeCount,
  };
}

export function getLocalObservabilityRuntime(config) {
  const observability = asObject(asObject(config).observability);
  const enabled = observability.enabled === true;
  const mode =
    cleanText(observability.mode).toLowerCase() === 'langfuse' ?
      'langfuse' :
      'local';
  if (enabled && mode === 'local') {
    return {
      loaded: true,
      enabled,
      mode,
      isWritingLocal: true,
      label: '当前正在写入本地观测日志',
      detail: '新运行完成后会进入本地观测分析。',
      tone: 'success',
    };
  }
  if (!enabled) {
    return {
      loaded: true,
      enabled,
      mode,
      isWritingLocal: false,
      label: '当前未开启观测',
      detail: '仍可分析历史本地日志；新运行不会写入本地观测数据。',
      tone: 'muted',
    };
  }
  return {
    loaded: true,
    enabled,
    mode,
    isWritingLocal: false,
    label: '当前观测写入 Langfuse',
    detail: '本页仅分析历史本地日志；新运行不会写入本地日志。',
    tone: 'warning',
  };
}

export function getUnknownLocalObservabilityRuntime() {
  return {
    loaded: false,
    enabled: null,
    mode: '',
    isWritingLocal: false,
    label: '正在读取观测配置',
    detail: '配置读取完成后会显示当前写入目标。',
    tone: 'muted',
  };
}

export function getFailedLocalObservabilityRuntime(error = '') {
  return {
    loaded: true,
    enabled: null,
    mode: '',
    isWritingLocal: false,
    label: '观测配置读取失败',
    detail: cleanText(error) || '请刷新后重试。',
    tone: 'error',
  };
}

export function diagnosticsText(
  diagnostics,
  { includeScannedFiles = false } = {},
) {
  const value = asObject(diagnostics);
  const parseErrors = Number(value.parse_error_count) || 0;
  const unknownSchemas = Number(value.unknown_schema_count) || 0;
  if (!parseErrors && !unknownSchemas && !value.truncated) {
    return '';
  }
  const parts = [];
  if (includeScannedFiles && value.scanned_files != null) {
    parts.push(`扫描文件 ${formatMetric(value.scanned_files)}`);
  }
  parts.push(`跳过旧版或不兼容日志 ${formatMetric(unknownSchemas)} 条`);
  parts.push(`损坏日志 ${formatMetric(parseErrors)} 条`);
  if (value.truncated) {
    parts.push('结果已截断');
  }
  return `${parts.join('，')}。`;
}

export function formatDuration(value) {
  const ms = Number(value || 0);
  if (!Number.isFinite(ms) || ms <= 0) {
    return '0 ms';
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  if (ms < 60_000) {
    return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  }
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatMetric(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) {
    return '-';
  }
  return new Intl.NumberFormat('zh-CN').format(number);
}

export function formatTimestamp(value) {
  if (value == null || value === '') {
    return '-';
  }
  const date = new Date(Number(value));
  if (Number.isNaN(date.getTime())) {
    return '-';
  }
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

export function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return '-';
  }
  return `${Math.round(number * 1000) / 10}%`;
}

export function formatCacheHit(row = {}) {
  if (
    row.llm_cache_hit_tokens == null ||
    row.llm_cache_total_input_tokens == null ||
    row.llm_cache_hit_rate == null
  ) {
    return '-';
  }
  const hitTokens = formatMetric(row.llm_cache_hit_tokens);
  const totalTokens = formatMetric(row.llm_cache_total_input_tokens);
  const hitRate = formatPercent(row.llm_cache_hit_rate);
  return `${hitTokens} / ${totalTokens} · ${hitRate}`;
}

export function shortId(value, maximum = 18) {
  const text = cleanText(value) || '-';
  if (text.length <= maximum) {
    return text;
  }
  return `${text.slice(0, 8)}...${text.slice(-6)}`;
}

export function llmWallDuration(metrics = {}) {
  return (
    Number(metrics.wall_duration_ms || 0) ||
    Number(metrics.duration_ms || 0) ||
    Number(metrics.stream_duration_ms || 0)
  );
}

export function formatPromptSize(metrics = {}) {
  const tokens = Number(metrics.prompt_tokens || 0);
  const chars = Number(metrics.prompt_chars || 0);
  if (tokens) {
    return `${formatMetric(tokens)} tokens`;
  }
  if (chars) {
    return `${formatMetric(chars)} chars`;
  }
  return '-';
}

export function detailCount(detail, tabId) {
  const body = asObject(detail);
  if (tabId === 'callchain') {
    return asArray(body.iteration_calls).length;
  }
  if (tabId === 'iterations') {
    return asArray(body.iterations).length;
  }
  if (tabId === 'llm') {
    return asArray(body.llm_calls).length;
  }
  if (tabId === 'tools') {
    return asArray(body.tools).length;
  }
  if (tabId === 'debug') {
    return asArray(body.runtime_phases).length + asArray(body.timeline).length;
  }
  return 0;
}

export const formatPerfEventLabel = (value) =>
  formatMappedPerfValue(value, PERF_EVENT_LABELS);
export const formatPerfKindLabel = (value) =>
  formatMappedPerfValue(value, PERF_KIND_LABELS);
export const formatPerfComponentLabel = (value) =>
  formatMappedPerfValue(value, PERF_COMPONENT_LABELS);
export const formatPerfPhaseLabel = (value) =>
  formatMappedPerfValue(value, PERF_PHASE_LABELS);
export const formatPerfCallerLabel = (value) =>
  formatMappedPerfValue(value, PERF_CALLER_LABELS);
export const formatPerfAgentModeLabel = (value) =>
  formatMappedPerfValue(value, PERF_AGENT_MODE_LABELS);
export const formatPerfToolLabel = (value) =>
  formatMappedPerfValue(value, PERF_TOOL_LABELS);

function formatMappedPerfValue(value, labels) {
  const raw = cleanText(value);
  if (!raw) {
    return '-';
  }
  const direct = labels[raw] || labels[raw.toLowerCase()];
  if (direct) {
    return direct;
  }
  return raw
    .replace(/^subagent:(?<name>.+)$/i, '子智能体 $<name>')
    .replace(/_/g, ' ')
    .replace(/\./g, ' / ');
}
