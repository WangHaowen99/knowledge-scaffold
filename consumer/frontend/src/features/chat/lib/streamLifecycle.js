export const streamStatusForAdmission = (runStatus) =>
  String(runStatus || '')
    .trim()
    .toLowerCase() === 'queued' ?
    'queued' :
    'reconnecting';

export const streamStatusForConnection = (reconnectAttempts = 0) =>
  Number(reconnectAttempts) > 0 ? 'reconnecting' : 'streaming';

export const streamStatusForTerminal = (runStatus) => {
  const normalized = String(runStatus || '')
    .trim()
    .toLowerCase();
  if (normalized === 'awaiting_input' || normalized === 'awaiting') {
    return 'awaiting';
  }
  if (normalized === 'cancelled' || normalized === 'canceled') {
    return 'cancelled';
  }
  if (normalized === 'error' || normalized === 'failed') {
    return 'error';
  }
  return 'finished';
};

export const streamStatusForError = (error) =>
  error?.name === 'AbortError' ? 'cancelled' : 'error';

export const messageStatusForTerminalRun = (runStatus) => {
  const normalized = String(runStatus || '')
    .trim()
    .toLowerCase();
  if (normalized === 'awaiting' || normalized === 'awaiting_input') {
    return 'awaiting';
  }
  if (normalized === 'cancelled' || normalized === 'canceled') {
    return 'cancelled';
  }
  if (normalized === 'error' || normalized === 'failed') {
    return 'error';
  }
  return 'finished';
};

const RUN_PROGRESS_PHASE_META = {
  starting: { label: '启动问答任务中' },
  running: { label: '执行问答任务中' },
  queued: { label: '排队等待中' },
  reconnecting: { label: '恢复连接中' },
  analyzing: { label: '分析问题中' },
  business_data: { label: '查询业务数据中' },
  web_search: { label: '查询联网资料中' },
  script: { label: '执行脚本任务中' },
  tool: { label: '执行工具任务中' },
  subagent: { label: '执行子任务中' },
  generating: { label: '生成回答中' },
  finalizing: { label: '整理最终答案中' },
  cancel_error: { label: '停止失败，运行仍在继续' },
};

const phaseLabel = (phase) =>
  RUN_PROGRESS_PHASE_META[phase]?.label ||
  RUN_PROGRESS_PHASE_META.starting.label;

export const formatElapsedMs = (elapsedMs) => {
  const normalizedElapsedMs = Number(elapsedMs);
  const totalSeconds = Number.isFinite(normalizedElapsedMs) ?
    Math.max(0, Math.floor(normalizedElapsedMs / 1000)) :
    0;
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  const pad = (value) => String(value).padStart(2, '0');
  return hours > 0 ?
    `${hours}:${pad(minutes)}:${pad(seconds)}` :
    `${pad(minutes)}:${pad(seconds)}`;
};

const startedAtFromRunId = (runId) => {
  const match = String(runId || '')
    .trim()
    .match(/^agentic-(?<timestamp>\d{13})(?:-|$)/);
  if (!match) {
    return null;
  }
  const timestamp = Number(match.groups.timestamp);
  return Number.isFinite(timestamp) ? timestamp : null;
};

export const resolveRunStartedAt = (
  { runId = '' } = {},
  fallbackNow = Date.now(),
) => {
  const normalizedFallback = Number(fallbackNow);
  const fallback =
    Number.isFinite(normalizedFallback) && normalizedFallback > 0 ?
      normalizedFallback :
      Date.now();
  const runIdStartedAt = startedAtFromRunId(runId);
  if (
    Number.isFinite(runIdStartedAt) &&
    runIdStartedAt > 0 &&
    runIdStartedAt <= fallback
  ) {
    return runIdStartedAt;
  }
  return fallback;
};

const normalizePayload = (payload) =>
  payload && typeof payload === 'object' ? payload : {};

const normalizeToolName = (payload) =>
  String(
    payload.tool_name ||
      payload.toolName ||
      payload.tool ||
      payload.name ||
      payload.provider ||
      payload.type ||
      '',
  )
    .trim()
    .toLowerCase();

const classifyToolPhase = (payload) => {
  const toolName = normalizeToolName(payload);
  if (!toolName) {
    return 'tool';
  }
  if (/(?:script|python|sandbox|code|executor|exec)/i.test(toolName)) {
    return 'script';
  }
  if (
    /(?:business|data|dataset|database|sql|query|neo4j|mindsdb|ontology|kg|graph)/i.test(
      toolName,
    )
  ) {
    return 'business_data';
  }
  if (/(?:web|search|browser|crawl|http|url)/i.test(toolName)) {
    return 'web_search';
  }
  return 'tool';
};

export const classifyRunProgressFromPublicEvent = (event) => {
  if (!event || typeof event !== 'object') {
    return null;
  }

  const type = String(event.event_type || '')
    .trim()
    .toLowerCase();
  const payload = normalizePayload(event.payload);
  if (!type) {
    return null;
  }

  if (type === 'turn.started') {
    return { phase: 'analyzing' };
  }
  if (type === 'turn.finished') {
    return { phase: 'finalizing' };
  }

  if (type.startsWith('tool.') || type.includes('.tool.')) {
    if (type.endsWith('.finished')) {
      return { phase: 'analyzing' };
    }
    const phase = classifyToolPhase(payload);
    return { phase };
  }

  if (type.startsWith('subagent') || type.includes('.subagent')) {
    if (type.endsWith('.finished')) {
      return { phase: 'analyzing' };
    }
    return { phase: 'subagent' };
  }

  if (type.startsWith('token.') || type.includes('.token.')) {
    const channel = String(payload.channel || payload.kind || '').toLowerCase();
    if (channel.includes('reason')) {
      return { phase: 'analyzing' };
    }
    return { phase: 'generating' };
  }

  if (type.includes('reasoning') || type.includes('iteration')) {
    return { phase: 'analyzing' };
  }

  return null;
};

const phaseForStreamStatus = (status) => {
  const normalized = String(status || '')
    .trim()
    .toLowerCase();
  if (normalized === 'queued') {
    return 'queued';
  }
  if (normalized === 'reconnecting') {
    return 'reconnecting';
  }
  if (normalized === 'finalizing') {
    return 'finalizing';
  }
  return 'running';
};

export const resolveRunProgressView = (streamState, nowMs = Date.now()) => {
  if (!streamState?.isLoading) {
    return null;
  }

  const normalizedNow = Number(nowMs);
  const now = Number.isFinite(normalizedNow) ? normalizedNow : Date.now();
  const normalizedStartedAt = Number(streamState.startedAt);
  const startedAt =
    Number.isFinite(normalizedStartedAt) && normalizedStartedAt > 0 ?
      normalizedStartedAt :
      now;
  const elapsedMs = Math.max(0, now - startedAt);
  const status = String(streamState.status || '')
    .trim()
    .toLowerCase();
  if (status === 'error') {
    return null;
  }
  if (status === 'cancel_error') {
    const label =
      String(streamState.error || '').trim() || phaseLabel('cancel_error');
    return {
      phase: 'cancel_error',
      label,
      elapsedMs,
      elapsedText: formatElapsedMs(elapsedMs),
      summary: `${label} · 已耗时 ${formatElapsedMs(elapsedMs)}`,
    };
  }
  const transportPhase =
    status === 'queued' || status === 'reconnecting' || status === 'finalizing';
  const basePhase = transportPhase ?
    phaseForStreamStatus(status) :
    streamState.progressPhase || phaseForStreamStatus(status);
  const baseLabel = phaseLabel(basePhase);
  const queuePosition = streamState.queue?.position ?
    ` · 第 ${streamState.queue.position} 位` :
    '';

  return {
    phase: basePhase,
    label: baseLabel,
    elapsedMs,
    elapsedText: formatElapsedMs(elapsedMs),
    summary: `${baseLabel}${status === 'queued' ? queuePosition : ''} · 已耗时 ${formatElapsedMs(elapsedMs)}`,
  };
};

const TERMINAL_RUN_STATUSES = new Set([
  'success',
  'completed',
  'finished',
  'error',
  'failed',
  'cancelled',
  'canceled',
  'awaiting',
  'awaiting_input',
]);

const silentAbortControllers = new WeakSet();

export const isTerminalRunStatus = (status) =>
  TERMINAL_RUN_STATUSES.has(
    String(status || '')
      .trim()
      .toLowerCase(),
  );

export const abortStreamController = (controller, { silent = false } = {}) => {
  if (!controller) {
    return;
  }
  if (silent) {
    silentAbortControllers.add(controller);
  }
  controller.abort();
};

export const abortStreamControllers = (controllers, options = {}) => {
  controllers?.forEach((controller) =>
    abortStreamController(controller, options),
  );
};

export const isSilentStreamAbort = (controller) =>
  Boolean(controller && silentAbortControllers.has(controller));
