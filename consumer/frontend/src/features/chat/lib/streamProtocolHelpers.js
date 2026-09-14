const BUSINESS_DATA_SEARCH_EVENT_TYPES = new Set([
  'start',
  'intermediate',
  'final',
  'error',
]);
const WEB_SEARCH_EVENT_TYPES = new Set(['start', 'final', 'error']);
const DATA_SEARCH_TOOL_NAMES = new Set(['business_data_search']);
const SOLVER_EVENT_TYPES = new Set([
  'solver.input.awaiting_confirmation',
  'solver.progress',
  'solver.verification.completed',
  'solver.verification.rejected',
  'solver.result.authoritative',
]);
const SOLVER_PUBLIC_FIELDS = new Set([
  'run_id',
  'job_id',
  'attempt_id',
  'event_seq',
  'phase',
  'status',
  'message',
  'elapsed_ms',
  'percent',
  'authoritative',
  'feasibility',
  'optimality',
  'proof_kind',
  'summary',
  'result_artifact_ref',
  'verification_evidence_ref',
  'candidate_results_artifact_ref',
  'candidate_results_artifact_digest',
  'knowledge_revision_id',
  'fact_snapshot_digest',
  'problem_digest',
  'code',
  'retryable',
  'missing',
  'questions',
  'tool_call_id',
  'tool_name',
]);

const RUNTIME_ERROR_PRESENTATION = Object.freeze({
  RUNTIME_EVENT_BUFFER_FULL: {
    message: '执行中断，当前结果不完整，请重试。',
    incomplete: true,
  },
  BUSINESS_SUMMARY_INCONSISTENT: {
    message: '结果口径不一致，当前结论未通过校验，请检查调整项后重试。',
    incomplete: true,
  },
});

const friendlyRuntimeError = (code) => {
  const normalized = String(code || '').trim().toUpperCase();
  const presentation = RUNTIME_ERROR_PRESENTATION[normalized];
  return presentation ? { ...presentation } : null;
};

const publicErrorMessage = (value, fallback = '') => {
  const visited = new Set();
  const extract = (candidate, depth = 0) => {
    if (candidate == null || depth > 4) {
      return '';
    }
    if (typeof candidate === 'string') {
      return candidate.trim();
    }
    if (typeof candidate === 'number') {
      return String(candidate);
    }
    if (Array.isArray(candidate)) {
      for (const item of candidate) {
        const message = extract(item, depth + 1);
        if (message) {
          return message;
        }
      }
      return '';
    }
    if (typeof candidate !== 'object' || visited.has(candidate)) {
      return '';
    }
    visited.add(candidate);
    for (const key of ['message', 'detail', 'error', 'reason', 'description']) {
      const message = extract(candidate[key], depth + 1);
      if (message) {
        return message;
      }
    }
    return extract(candidate.error_code ?? candidate.code, depth + 1);
  };
  const extracted = extract(value);
  const code =
    typeof value === 'object' && value ?
      value.error_code ?? value.code :
      extracted;
  return (
    friendlyRuntimeError(code)?.message ||
    extracted ||
    (typeof fallback === 'string' ? fallback : '')
  );
};

const hasPublicErrorValue = (value) =>
  value != null && (typeof value !== 'string' || Boolean(value.trim()));

const terminalStatusFromPublicStatus = (publicStatus) => {
  if (publicStatus === 'completed') {
    return 'success';
  }
  if (publicStatus === 'failed') {
    return 'error';
  }
  return publicStatus;
};

const stringifyStructuredResult = (structured) => {
  const content =
    structured.text || structured.formatted_result || structured.message || '';
  if (content) {
    return content;
  }
  try {
    return JSON.stringify(structured, null, 2);
  } catch {
    return '';
  }
};

const terminalResultDetails = (result) => {
  if (typeof result === 'string') {
    return {
      content: result,
      options: null,
      messageType: 'text',
      interaction: null,
      artifacts: [],
    };
  }
  if (!result || typeof result !== 'object') {
    return {
      content: '',
      options: null,
      messageType: 'text',
      interaction: null,
      artifacts: [],
    };
  }
  const structured =
    result.structured_result && typeof result.structured_result === 'object' ?
      result.structured_result :
      null;
  return {
    content:
      result.text ||
      result.formatted_result ||
      (structured ? stringifyStructuredResult(structured) : ''),
    options: Array.isArray(result.options) ? result.options : null,
    messageType: typeof result.type === 'string' ? result.type : 'text',
    interaction:
      result.interaction && typeof result.interaction === 'object' ?
        result.interaction :
        null,
    artifacts: Array.isArray(result.artifacts) ?
      result.artifacts.filter(
        (item) => item && typeof item === 'object' && item.output_ref,
      ) :
      [],
  };
};

const extractTurnFinishedOutput = (
  payload = {},
  publicStatus = 'completed',
) => {
  const result =
    payload.final_output ??
    payload.structured_content ??
    payload.result ??
    payload.content;
  const resultDetails = terminalResultDetails(result);
  const errorCode =
    typeof payload?.error === 'object' && payload.error ?
      String(payload.error.error_code ?? payload.error.code ?? '').trim() :
      '';
  const runtimeError = friendlyRuntimeError(errorCode);

  return {
    status: terminalStatusFromPublicStatus(publicStatus),
    ...resultDetails,
    error:
      runtimeError?.message ||
      (hasPublicErrorValue(payload.error) ?
        publicErrorMessage(payload.error, '运行失败') :
        null),
    errorCode,
    errorRetriable:
      payload.error && typeof payload.error === 'object' ?
        payload.error.retriable === true :
        false,
    cancelReason: payload.cancel_reason || null,
    recovery:
      payload.recovery && typeof payload.recovery === 'object' ?
        payload.recovery :
        null,
  };
};

const TOOL_STATUS_ICON_MAP = {
  completed: 'completed',
  complete: 'completed',
  done: 'completed',
  success: 'completed',
  succeeded: 'completed',
  ok: 'completed',
  error: 'error',
  failed: 'error',
  fail: 'error',
  cancelled: 'cancelled',
  canceled: 'cancelled',
  aborted: 'cancelled',
  abort: 'cancelled',
};

const normalizeToolStatus = (value) => {
  if (typeof value !== 'string') {
    return null;
  }
  return TOOL_STATUS_ICON_MAP[value.trim().toLowerCase()] || null;
};

const normalizeToolCallId = (value) =>
  typeof value === 'string' ? value.trim() : '';

const normalizeToolName = (value) =>
  typeof value === 'string' ? value.trim() : '';

const normalizeSpanId = (value) =>
  typeof value === 'string' ? value.trim() : '';

const toolNameFromPayload = (payload = {}) =>
  normalizeToolName(payload?.tool_name) || normalizeToolName(payload?.name);

const extractToolQuery = (meta = {}) =>
  String(
    meta?.input?.query ??
      meta?.input_preview?.query ??
      meta?.tool_input?.query ??
      meta?.tool_args?.query ??
      meta?.query ??
      '',
  ).trim();

const withPublicToolInput = (payload = {}) =>
  payload?.input == null && payload?.input_preview != null ?
    { ...payload, input: payload.input_preview } :
    payload;

const resolveToolLocator = (payload = {}) => {
  const spanId = normalizeSpanId(payload?.span_id);
  const toolCallId = normalizeToolCallId(payload?.tool_call_id);
  const toolName = toolNameFromPayload(payload);
  const query = extractToolQuery(payload);

  return {
    spanId,
    toolCallId,
    toolName,
    query,
    isOntology: DATA_SEARCH_TOOL_NAMES.has(toolName),
  };
};

const hasToolLocator = (locator = {}) => {
  if (locator?.spanId) {
    return true;
  }
  if (locator?.toolCallId) {
    return true;
  }
  if (locator?.isOntology) {
    return Boolean(locator?.query);
  }
  return Boolean(locator?.toolName);
};

const segmentMatchesToolLocator = (segment, locator = {}) => {
  if (!segment || segment.type !== 'tool') {
    return false;
  }

  const segmentMeta = segment?.meta || {};
  const segmentSpanId = normalizeSpanId(
    segment?.spanId || segmentMeta?.span_id,
  );
  if (locator.spanId) {
    return segmentSpanId === locator.spanId;
  }

  const segmentToolCallId = normalizeToolCallId(segmentMeta?.tool_call_id);
  if (locator.toolCallId) {
    return segmentToolCallId === locator.toolCallId;
  }

  const segmentToolName = toolNameFromPayload(segmentMeta);
  if (locator.isOntology && locator.query) {
    if (!DATA_SEARCH_TOOL_NAMES.has(segmentToolName)) {
      return false;
    }
    return extractToolQuery(segmentMeta) === locator.query;
  }
  if (locator.isOntology) {
    return false;
  }

  if (locator.toolName) {
    return segmentToolName === locator.toolName;
  }

  return false;
};

const mergeToolMeta = (baseMeta = {}, incomingMeta = {}) => {
  const merged = {
    ...(baseMeta || {}),
    ...(incomingMeta || {}),
  };

  const existingQuery = extractToolQuery(baseMeta);
  const incomingQuery = extractToolQuery(incomingMeta);
  const resolvedQuery = incomingQuery || existingQuery;
  if (resolvedQuery) {
    const baseInput =
      baseMeta?.input && typeof baseMeta.input === 'object' ?
        baseMeta.input :
        {};
    const incomingInput =
      incomingMeta?.input && typeof incomingMeta.input === 'object' ?
        incomingMeta.input :
        {};
    merged.query = resolvedQuery;
    merged.input = {
      ...baseInput,
      ...incomingInput,
      query: resolvedQuery,
    };
  }

  const existingToolCallId = normalizeToolCallId(baseMeta?.tool_call_id);
  const incomingToolCallId = normalizeToolCallId(incomingMeta?.tool_call_id);
  if (!incomingToolCallId && existingToolCallId) {
    merged.tool_call_id = existingToolCallId;
  }

  return merged;
};

const solverValueIsSafe = (value, depth = 0) => {
  if (depth > 6) {
    return null;
  }
  if (value == null || ['string', 'number', 'boolean'].includes(typeof value)) {
    return value;
  }
  if (Array.isArray(value)) {
    return value
      .slice(0, 100)
      .map((item) => solverValueIsSafe(item, depth + 1));
  }
  if (typeof value !== 'object') {
    return null;
  }
  const safe = {};
  Object.entries(value).forEach(([key, item]) => {
    const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '');
    if (
      /(?:token|secret|password|credential|endpoint|internalpath|problemir)/.test(
        normalized,
      )
    ) {
      return;
    }
    if (normalized === 'factsnapshot' || normalized === 'factsnapshotpayload') {
      return;
    }
    safe[key] = solverValueIsSafe(item, depth + 1);
  });
  return safe;
};

const projectSolverEvent = (type, payload = {}) => {
  const projected = { type };
  Object.entries(payload || {}).forEach(([key, value]) => {
    if (SOLVER_PUBLIC_FIELDS.has(key)) {
      projected[key] = solverValueIsSafe(value);
    }
  });
  return projected;
};

const mergeSolverEventMeta = (baseMeta = {}, event = {}) => {
  const existing = Array.isArray(baseMeta?.solver_events) ?
    baseMeta.solver_events :
    [];
  const eventSeq = Number(event?.event_seq);
  const isReplay =
    Number.isFinite(eventSeq) &&
    existing.some(
      (item) =>
        Number(item?.event_seq) === eventSeq && item?.job_id === event?.job_id,
    );
  const solverEvents = isReplay ?
    existing :
    [...existing, event].sort((left, right) => {
      const leftSeq = Number(left?.event_seq);
      const rightSeq = Number(right?.event_seq);
      if (Number.isFinite(leftSeq) && Number.isFinite(rightSeq)) {
        return leftSeq - rightSeq;
      }
      return 0;
    });
  const merged = {
    ...mergeToolMeta(baseMeta, event),
    solver_events: solverEvents,
  };
  if (
    event.type === 'solver.result.authoritative' &&
    event.authoritative === true
  ) {
    merged.solver_result = event;
  }
  if (event.type === 'solver.verification.rejected') {
    merged.solver_rejection = event;
  }
  return merged;
};

export {
  BUSINESS_DATA_SEARCH_EVENT_TYPES,
  DATA_SEARCH_TOOL_NAMES,
  extractTurnFinishedOutput,
  friendlyRuntimeError,
  hasPublicErrorValue,
  hasToolLocator,
  mergeSolverEventMeta,
  mergeToolMeta,
  normalizeSpanId,
  normalizeToolStatus,
  projectSolverEvent,
  publicErrorMessage,
  resolveToolLocator,
  segmentMatchesToolLocator,
  SOLVER_EVENT_TYPES,
  toolNameFromPayload,
  WEB_SEARCH_EVENT_TYPES,
  withPublicToolInput,
};
