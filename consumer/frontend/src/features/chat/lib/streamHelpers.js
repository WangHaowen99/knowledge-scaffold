/**
 * Project canonical PublicEvent records into chat UI state.
 */

import { isPublicEvent, publicEventTimestamp } from './publicEvent.js';

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

export const friendlyRuntimeError = (code) => {
  const normalized = String(code || '').trim().toUpperCase();
  const presentation = RUNTIME_ERROR_PRESENTATION[normalized];
  return presentation ? { ...presentation } : null;
};

export const publicErrorMessage = (value, fallback = '') => {
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

const closeOpenNonToolSegment = (segments = []) => {
  if (!Array.isArray(segments) || segments.length === 0) {
    return Array.isArray(segments) ? segments : [];
  }
  const last = segments[segments.length - 1];
  if (!last || last.finished || last.type === 'tool') {
    return segments;
  }
  return closeOpenSegment(segments);
};

export const closeOpenSegment = (segments = []) => {
  if (!Array.isArray(segments) || segments.length === 0) {
    return Array.isArray(segments) ? segments : [];
  }
  const lastIdx = segments.length - 1;
  const last = segments[lastIdx];
  if (!last || last.finished) {
    return segments;
  }

  const next = segments.slice();
  next[lastIdx] = { ...last, finished: true };
  return next;
};

const closeSubagentEventsForTerminal = (events, terminalStatus, ts) =>
  (Array.isArray(events) ? events : []).map((event) => {
    if (!event || event.finished) {
      return event;
    }
    return {
      ...event,
      status: terminalStatus,
      finished: true,
      endTs: event.kind === 'tool' ? ts : event.endTs || event.ts || ts,
    };
  });

const closeSubagentGroupForTerminalTurn = (group, terminalStatus, ts) => {
  let fallbackStatus = 'completed';
  if (terminalStatus === 'cancelled') {
    fallbackStatus = 'cancelled';
  } else if (terminalStatus === 'error') {
    fallbackStatus = 'error';
  }
  const children = {};
  Object.entries(group?.children || {}).forEach(([subagentId, child]) => {
    const childStatus = ['completed', 'error', 'cancelled'].includes(
      child?.status,
    ) ?
      child.status :
      fallbackStatus;
    children[subagentId] = {
      ...child,
      status: childStatus,
      endTs: child?.endTs || ts,
      events: closeSubagentEventsForTerminal(child?.events, childStatus, ts),
      childGroups: (Array.isArray(child?.childGroups) ?
        child.childGroups :
        []
      ).map((nested) =>
        closeSubagentGroupForTerminalTurn(nested, terminalStatus, ts),
      ),
    };
  });
  const groupStatus = ['completed', 'error', 'cancelled'].includes(
    group?.status,
  ) ?
    group.status :
    fallbackStatus;
  return {
    ...group,
    status: groupStatus,
    finished: true,
    endTs: group?.endTs || ts,
    children,
  };
};

const closeUnfinishedSegmentsForTerminalTurn = (
  segments = [],
  terminalStatus = 'success',
  ts = Date.now(),
) => {
  if (!Array.isArray(segments) || segments.length === 0) {
    return Array.isArray(segments) ? segments : [];
  }

  const isError = terminalStatus === 'error';
  const isCancelled = terminalStatus === 'cancelled';
  return segments.map((segment) => {
    if (segment?.type === 'subagent_group') {
      return closeSubagentGroupForTerminalTurn(
        segment,
        terminalStatus,
        ts || Date.now(),
      );
    }
    if (!segment || segment.finished) {
      return segment;
    }

    const closed = {
      ...segment,
      finished: true,
      endTs: ts || Date.now(),
    };
    if (segment.type !== 'tool') {
      return closed;
    }

    let terminalToolStatus = 'completed';
    if (isCancelled) {
      terminalToolStatus = 'cancelled';
    } else if (isError) {
      terminalToolStatus = 'error';
    }
    return {
      ...closed,
      state: terminalToolStatus,
      toolStatus: terminalToolStatus,
      meta: {
        ...(segment.meta || {}),
        status: terminalStatus,
      },
    };
  });
};

const findLastSegmentIndex = (segments, predicate) => {
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    if (predicate(segments[i])) {
      return i;
    }
  }
  return -1;
};

const appendInlineTextSegment = (
  segments,
  {
    type,
    state,
    text,
    node,
    meta,
    mergeKey = null,
    ts,
    traceId = '',
    spanId = '',
    parentSpanId = '',
    spanKey = '',
    parentKey = '',
  },
// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
) => {
  if (!text) {
    return segments;
  }

  const next = Array.isArray(segments) ? segments.slice() : [];
  const last = next[next.length - 1];
  const usesSpanIdentity = Boolean(spanId || last?.spanId);
  const canMerge = Boolean(
    last &&
      !last.finished &&
      last.type === type &&
      last.state === state &&
      (last.node || '') === (node || '') &&
      (usesSpanIdentity ?
        Boolean(spanId && last?.spanId === spanId) :
        (last.spanKey || '') === spanKey &&
          (last.parentKey || '') === parentKey) &&
      (!mergeKey ||
        (typeof last?.meta?.[mergeKey] === 'string' &&
          typeof meta?.[mergeKey] === 'string' &&
          last.meta[mergeKey] === meta[mergeKey])),
  );

  if (!canMerge && String(text).trim().length === 0) {
    return next;
  }

  if (canMerge) {
    next[next.length - 1] = {
      ...last,
      text: `${last.text || ''}${text}`,
      meta: meta || last.meta,
      traceId: traceId || last.traceId,
      spanId: spanId || last.spanId,
      parentSpanId: parentSpanId || last.parentSpanId,
      endTs: ts || Date.now(),
    };
    return next;
  }

  return [
    ...closeOpenSegment(next),
    {
      type,
      state,
      text,
      node,
      meta,
      traceId: traceId || undefined,
      spanId: spanId || undefined,
      parentSpanId: parentSpanId || undefined,
      spanKey: spanKey || undefined,
      parentKey: parentKey || undefined,
      finished: false,
      startTs: ts || Date.now(),
      endTs: ts || Date.now(),
    },
  ];
};

const iterationParentKeyFromPayload = (payload = {}) => {
  const iteration = Number(payload?.iteration);
  if (!Number.isFinite(iteration) || iteration < 1) {
    return '';
  }
  return `iteration:main:${Math.floor(iteration)}`;
};

const extractTurnFinishedOutput = (
  payload = {},
  publicStatus = 'completed',
// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
) => {
  let status = publicStatus;
  if (publicStatus === 'completed') {
    status = 'success';
  } else if (publicStatus === 'failed') {
    status = 'error';
  }

  const result =
    payload.final_output ??
    payload.structured_content ??
    payload.result ??
    payload.content;
  let content = '';
  let options = null;
  let messageType = 'text';
  let interaction = null;
  let artifacts = [];
  const errorCode =
    typeof payload?.error === 'object' && payload.error ?
      String(payload.error.error_code ?? payload.error.code ?? '').trim() :
      '';
  const runtimeError = friendlyRuntimeError(errorCode);

  if (typeof result === 'string') {
    content = result;
  } else if (result && typeof result === 'object') {
    content = result.text || result.formatted_result || '';
    if (
      !content &&
      result.structured_result &&
      typeof result.structured_result === 'object'
    ) {
      const structured = result.structured_result;
      content =
        structured.text ||
        structured.formatted_result ||
        structured.message ||
        '';
      if (!content) {
        try {
          content = JSON.stringify(structured, null, 2);
        } catch {
          content = '';
        }
      }
    }
    options = Array.isArray(result.options) ? result.options : null;
    messageType = typeof result.type === 'string' ? result.type : 'text';
    interaction =
      result.interaction && typeof result.interaction === 'object' ?
        result.interaction :
        null;
    artifacts = Array.isArray(result.artifacts) ?
      result.artifacts.filter(
        (item) => item && typeof item === 'object' && item.output_ref,
      ) :
      [];
  }

  return {
    status,
    content,
    options,
    messageType,
    interaction,
    artifacts,
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

const getTrailingContentText = (segments = []) => {
  if (!Array.isArray(segments) || segments.length === 0) {
    return '';
  }

  const trailing = [];
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    const segment = segments[i];
    if (segment?.type === 'content') {
      trailing.unshift(segment?.text || '');
      continue;
    }
    if (trailing.length > 0) {
      break;
    }
  }

  return trailing.join('').trim();
};

const shouldAppendFinalContent = (segments = [], finalContent = '') => {
  const normalized = String(finalContent || '').trim();
  if (!normalized) {
    return false;
  }

  // Compare only the trailing content block of the current turn.
  // Earlier content blocks are intermediate outputs and should not trigger
  // a duplicate append of the final answer.
  const trailingContent = getTrailingContentText(segments);
  if (!trailingContent) {
    return true;
  }

  return (
    trailingContent !== normalized && !trailingContent.endsWith(normalized)
  );
};

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
const finalizeTurnState = (next, publicStatus, payload, eventTs) => {
  const terminal = extractTurnFinishedOutput(payload, publicStatus);
  next.streamSegments = closeUnfinishedSegmentsForTerminalTurn(
    next.streamSegments,
    terminal.status,
    eventTs,
  );

  if (shouldAppendFinalContent(next.streamSegments, terminal.content || '')) {
    const finalIteration = Array.isArray(next.iterationMetrics) ?
      next.iterationMetrics[next.iterationMetrics.length - 1] :
      null;
    next.streamSegments.push({
      type: 'content',
      state: 'final',
      text: terminal.content,
      node: 'final',
      meta: {},
      finished: true,
      parentKey: iterationParentKeyFromPayload(finalIteration) || undefined,
    });
  }
  if (terminal.artifacts.length > 0) {
    next.streamSegments.push({
      type: 'artifacts',
      state: 'final',
      text: '',
      node: 'final',
      meta: { artifacts: terminal.artifacts },
      finished: true,
    });
  }

  if (terminal.status === 'success') {
    next.status = 'finished';
  } else if (terminal.status === 'awaiting_input') {
    next.status = 'awaiting';
  } else if (terminal.status === 'cancelled') {
    next.status = 'aborted';
    next.error = true;
  } else {
    next.status = 'error';
    next.error = true;
  }

  next.isLoading = false;
  next.endTs = eventTs;
  next.currentSegmentType = null;

  if (terminal.content && !next.content) {
    next.content = terminal.content;
  }
  if (terminal.options) {
    next.options = terminal.options;
  }
  if (terminal.interaction) {
    next.interaction = terminal.interaction;
  }
  if (terminal.messageType) {
    next.messageType = terminal.messageType;
  }
  if (terminal.error) {
    next.meta = { ...(next.meta || {}), error: terminal.error };
  }
  if (terminal.errorCode) {
    next.meta = { ...(next.meta || {}), error_code: terminal.errorCode };
  }
  const runtimeError = friendlyRuntimeError(terminal.errorCode);
  if (runtimeError?.incomplete) {
    next.meta = { ...(next.meta || {}), incomplete: true };
  }
  if (terminal.errorRetriable) {
    next.meta = { ...(next.meta || {}), error_retriable: true };
  }
  if (terminal.cancelReason) {
    next.meta = { ...(next.meta || {}), cancel_reason: terminal.cancelReason };
  }
  if (terminal.recovery) {
    next.meta = { ...(next.meta || {}), recovery: terminal.recovery };
  }

  return next;
};

export const applyLocalRunTermination = (
  current,
  { status, error = '', cancelReason = '', timestamp = Date.now() } = {},
) => {
  const publicStatus = status === 'cancelled' ? 'cancelled' : 'failed';
  const eventTs = Number.isFinite(Number(timestamp)) ?
    Number(timestamp) :
    Date.now();
  const next = {
    ...current,
    streamSegments: Array.isArray(current?.streamSegments) ?
      current.streamSegments.slice() :
      [],
  };
  return finalizeTurnState(
    next,
    publicStatus,
    {
      status: publicStatus,
      ...(error ? { error } : {}),
      ...(cancelReason ? { cancel_reason: cancelReason } : {}),
    },
    eventTs,
  );
};

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
const extractBusinessDataSearchProgressEvent = (payload = {}, event = {}) => {
  const toolName = toolNameFromPayload(payload);
  if (!DATA_SEARCH_TOOL_NAMES.has(toolName)) {
    return null;
  }

  const rawEvent = payload?.progress?.business_data_search_event;
  if (!rawEvent || typeof rawEvent !== 'object') {
    return null;
  }

  const eventType =
    typeof rawEvent?.type === 'string' ?
      rawEvent.type.trim().toLowerCase() :
      '';
  if (!BUSINESS_DATA_SEARCH_EVENT_TYPES.has(eventType)) {
    return null;
  }

  const content =
    rawEvent?.content == null ? '' : String(rawEvent.content).trim();
  if (!content) {
    return null;
  }
  const rawTs = publicEventTimestamp(event);
  const rawSeq = Number(event?.seq);

  const stage =
    typeof rawEvent?.stage === 'string' ? rawEvent.stage.trim() : '';
  const bindingKey =
    typeof rawEvent?.binding_key === 'string' ?
      rawEvent.binding_key.trim() :
      '';
  const internalRequestId =
    typeof rawEvent?.internal_request_id === 'string' ?
      rawEvent.internal_request_id.trim() :
      '';
  const valueInputFieldCount = Number.isFinite(
    Number(rawEvent?.value_input_field_count),
  ) ?
    Number(rawEvent.value_input_field_count) :
    null;
  const valueInputValueCount = Number.isFinite(
    Number(rawEvent?.value_input_value_count),
  ) ?
    Number(rawEvent.value_input_value_count) :
    null;

  return {
    type: eventType,
    content,
    ts: Number.isFinite(rawTs) ? rawTs : Date.now(),
    seq: Number.isFinite(rawSeq) ? rawSeq : null,
    ...(stage ? { stage } : {}),
    ...(bindingKey ? { binding_key: bindingKey } : {}),
    ...(internalRequestId ? { internal_request_id: internalRequestId } : {}),
    ...(valueInputFieldCount != null ?
      { value_input_field_count: valueInputFieldCount } :
      {}),
    ...(valueInputValueCount != null ?
      { value_input_value_count: valueInputValueCount } :
      {}),
  };
};

const appendBusinessDataSearchEventToMeta = (meta = {}, dbEvent = null) => {
  if (!dbEvent) {
    return meta || {};
  }
  const existing = Array.isArray(meta?.db_events) ? meta.db_events : [];
  return {
    ...(meta || {}),
    db_events: [...existing, dbEvent],
  };
};

const extractWebSearchProgressEvent = (payload = {}, event = {}) => {
  const toolName = toolNameFromPayload(payload);
  if (toolName !== 'web_search') {
    return null;
  }

  const rawEvent = payload?.progress?.web_search_event;
  if (!rawEvent || typeof rawEvent !== 'object') {
    return null;
  }

  const eventType =
    typeof rawEvent?.type === 'string' ?
      rawEvent.type.trim().toLowerCase() :
      '';
  if (!WEB_SEARCH_EVENT_TYPES.has(eventType)) {
    return null;
  }

  const content = rawEvent?.content == null ? '' : String(rawEvent.content);
  const rawTs = publicEventTimestamp(event);
  const rawSeq = Number(event?.seq);

  return {
    type: eventType,
    content,
    ts: Number.isFinite(rawTs) ? rawTs : Date.now(),
    seq: Number.isFinite(rawSeq) ? rawSeq : null,
  };
};

const appendWebSearchEventToMeta = (meta = {}, webEvent = null) => {
  if (!webEvent) {
    return meta || {};
  }
  const existing = Array.isArray(meta?.web_search_events) ?
    meta.web_search_events :
    [];
  return {
    ...(meta || {}),
    web_search_events: [...existing, webEvent],
  };
};

const normalizeNonNegativeNumber = (value) => {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return null;
  }
  return numeric;
};

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
const upsertIterationMetric = (metrics = [], payload = {}, ts = Date.now()) => {
  const iterationValue =
    typeof payload?.iteration === 'number' ?
      payload.iteration :
      Number(payload?.iteration);
  const iteration =
    Number.isFinite(iterationValue) && iterationValue > 0 ?
      Math.floor(iterationValue) :
      null;
  if (iteration == null) {
    return Array.isArray(metrics) ? metrics : [];
  }

  const nextMetrics = Array.isArray(metrics) ? metrics.slice() : [];
  const nextMetric = {
    iteration,
    status: typeof payload?.status === 'string' ? payload.status : '',
    llmElapsedMs: normalizeNonNegativeNumber(payload?.llm_elapsed_ms),
    llmStreamElapsedMs: normalizeNonNegativeNumber(
      payload?.llm_stream_elapsed_ms,
    ),
    llmWallElapsedMs: normalizeNonNegativeNumber(payload?.llm_wall_elapsed_ms),
    toolsElapsedMs: normalizeNonNegativeNumber(payload?.tools_elapsed_ms),
    toolsWindowElapsedMs: normalizeNonNegativeNumber(
      payload?.tools_window_elapsed_ms,
    ),
    toolsWallElapsedMs: normalizeNonNegativeNumber(
      payload?.tools_wall_elapsed_ms,
    ),
    toolsExecutionElapsedMs: normalizeNonNegativeNumber(
      payload?.tools_execution_elapsed_ms,
    ),
    toolsOverlapLlmMs: normalizeNonNegativeNumber(
      payload?.tools_overlap_llm_ms,
    ),
    toolsNetWaitMs: normalizeNonNegativeNumber(payload?.tools_net_wait_ms),
    promptBuildMs: normalizeNonNegativeNumber(payload?.prompt_build_ms),
    totalElapsedMs: normalizeNonNegativeNumber(payload?.total_elapsed_ms),
    toolCallsCount: normalizeNonNegativeNumber(payload?.tool_calls_count),
    toolNames: Array.isArray(payload?.tool_names) ?
      payload.tool_names.map((item) => String(item)) :
      [],
    traceId: normalizeSpanId(payload?.trace_id),
    spanId: normalizeSpanId(payload?.span_id),
    parentSpanId: normalizeSpanId(payload?.parent_span_id),
    ts: normalizeNonNegativeNumber(ts) ?? Date.now(),
  };

  const existingIdx = nextMetrics.findIndex(
    (item) => Number(item?.iteration) === iteration,
  );
  if (existingIdx !== -1) {
    nextMetrics[existingIdx] = {
      ...nextMetrics[existingIdx],
      ...nextMetric,
    };
  } else {
    nextMetrics.push(nextMetric);
    nextMetrics.sort(
      (a, b) => Number(a?.iteration || 0) - Number(b?.iteration || 0),
    );
  }

  return nextMetrics;
};

const getChildGroups = (child) =>
  Array.isArray(child?.childGroups) ? child.childGroups : [];

function cloneChild(child) {
  return {
    ...child,
    events: Array.isArray(child?.events) ? child.events.slice() : [],
    childGroups: getChildGroups(child).map((group) =>
      cloneSubagentGroup(group),
    ),
  };
}

function cloneSubagentGroup(group) {
  const children = {};
  Object.entries(group?.children || {}).forEach(([subagentId, child]) => {
    children[subagentId] = cloneChild(child);
  });

  return {
    ...group,
    children,
    taskOrder: Array.isArray(group?.taskOrder) ? group.taskOrder.slice() : [],
    pendingTasks: { ...(group?.pendingTasks || {}) },
  };
}

const updateGroupRecursive = (group, matcher, updater) => {
  if (!group || group.type !== 'subagent_group') {
    return { found: false, group };
  }

  if (matcher(group)) {
    return { found: true, group: updater(group) };
  }

  const childrenEntries = Object.entries(group.children || {});
  for (
    let childIdx = childrenEntries.length - 1;
    childIdx >= 0;
    childIdx -= 1
  ) {
    const [subagentId, child] = childrenEntries[childIdx];
    const childGroups = getChildGroups(child);
    for (let groupIdx = childGroups.length - 1; groupIdx >= 0; groupIdx -= 1) {
      const result = updateGroupRecursive(
        childGroups[groupIdx],
        matcher,
        updater,
      );
      if (!result.found) {
        continue;
      }

      const nextGroup = cloneSubagentGroup(group);
      const nextChild = cloneChild(nextGroup.children[subagentId]);
      const nextChildGroups = getChildGroups(nextChild).slice();
      nextChildGroups[groupIdx] = result.group;
      nextChild.childGroups = nextChildGroups;
      nextGroup.children[subagentId] = nextChild;
      return { found: true, group: nextGroup };
    }
  }

  return { found: false, group };
};

const updateGroupInSegments = (segments, matcher, updater) => {
  if (!Array.isArray(segments) || segments.length === 0) {
    return null;
  }

  for (let idx = segments.length - 1; idx >= 0; idx -= 1) {
    const segment = segments[idx];
    if (segment?.type !== 'subagent_group') {
      continue;
    }
    const result = updateGroupRecursive(segment, matcher, updater);
    if (!result.found) {
      continue;
    }

    const nextSegments = segments.slice();
    nextSegments[idx] = result.group;
    return nextSegments;
  }

  return null;
};

const updateGroupByGroupId = (segments, groupId, updater) => {
  if (!groupId) {
    return null;
  }
  return updateGroupInSegments(
    segments,
    (group) => group?.groupId === groupId,
    updater,
  );
};

const updateGroupBySubagentId = (segments, subagentId, updater) => {
  if (!subagentId) {
    return null;
  }
  return updateGroupInSegments(
    segments,
    (group) =>
      Boolean(
        group?.children &&
          Object.prototype.hasOwnProperty.call(group.children, subagentId),
      ),
    updater,
  );
};

const updateSubagentChildInSegments = (segments, subagentId, meta, updater) => {
  const updateChildInGroup = (group) => {
    const { nextGroup, child } = ensureSubagentChild(group, subagentId, meta);
    nextGroup.children[subagentId] = updater(child);
    return nextGroup;
  };

  return (
    updateGroupByGroupId(segments, meta?.groupId, updateChildInGroup) ||
    updateGroupBySubagentId(segments, subagentId, updateChildInGroup)
  );
};

const appendChildGroupToSubagent = (segments, parentSubagentId, childGroup) =>
  updateSubagentChildInSegments(segments, parentSubagentId, {}, (child) => {
    const nextChild = cloneChild(child);
    const childGroups = getChildGroups(nextChild).slice();
    const existingIdx = findLastSegmentIndex(
      childGroups,
      (group) => group?.groupId === childGroup?.groupId,
    );
    if (existingIdx === -1) {
      childGroups.push(childGroup);
    } else {
      childGroups[existingIdx] = childGroup;
    }
    nextChild.childGroups = childGroups;
    return nextChild;
  });

const normalizeTaskIndex = (value, fallback = 0) => {
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
};

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
const ensureSubagentChild = (group, subagentId, meta = {}) => {
  const nextGroup = cloneSubagentGroup(group);
  const existingChild = nextGroup?.children?.[subagentId];
  if (existingChild) {
    return {
      nextGroup,
      child: cloneChild(existingChild),
    };
  }

  const taskId = typeof meta?.taskId === 'string' ? meta.taskId : '';
  const pending = (taskId && nextGroup?.pendingTasks?.[taskId]) || {};
  const task =
    typeof meta?.task === 'string' && meta.task.trim().length > 0 ?
      meta.task :
      pending?.task || '';
  const taskIndex = normalizeTaskIndex(
    meta?.taskIndex,
    normalizeTaskIndex(pending?.taskIndex, 0),
  );
  const child = {
    subagentId,
    taskId,
    taskIndex,
    task,
    status: 'running',
    events: [],
    startTs: meta?.ts || Date.now(),
  };

  nextGroup.children[subagentId] = child;
  if (!nextGroup.taskOrder.includes(subagentId)) {
    nextGroup.taskOrder.push(subagentId);
  }
  if (taskId && nextGroup.pendingTasks) {
    delete nextGroup.pendingTasks[taskId];
  }

  return {
    nextGroup,
    child,
  };
};

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
const appendChildEvent = (child, event) => {
  const next = cloneChild(child);
  const last = next.events[next.events.length - 1];

  if (event.kind === 'token') {
    const finiteOrder = (value) =>
      value != null && value !== '' && Number.isFinite(Number(value)) ?
        Number(value) :
        null;
    const lastStartedAt = Number(last?.startTs ?? last?.ts);
    const lastSourceOrder = finiteOrder(
      last?.endSourceOrder ?? last?.sourceOrder,
    );
    const latestGroupSourceOrder = Math.max(
      ...getChildGroups(next)
        .map((group) => finiteOrder(group?.sourceOrder))
        .filter((value) => value != null),
      Number.NEGATIVE_INFINITY,
    );
    const latestGroupStartedAt = Math.max(
      ...getChildGroups(next)
        .map((group) => Number(group?.startTs))
        .filter(Number.isFinite),
      Number.NEGATIVE_INFINITY,
    );
    const groupInterruptedToken =
      Number.isFinite(latestGroupSourceOrder) && lastSourceOrder != null ?
        latestGroupSourceOrder > lastSourceOrder :
        Number.isFinite(lastStartedAt) &&
          latestGroupStartedAt > lastStartedAt;
    const usesSpanIdentity = Boolean(event.spanId || last?.spanId);
    const sameSpan = usesSpanIdentity ?
      Boolean(event.spanId && last?.spanId === event.spanId) :
      true;
    const canMergeToken =
      last &&
      last.kind === 'token' &&
      last.channel === event.channel &&
      sameSpan &&
      !groupInterruptedToken;
    if (canMergeToken) {
      next.events[next.events.length - 1] = {
        ...last,
        text: `${last.text || ''}${event.text || ''}`,
        startTs: last.startTs ?? last.ts,
        endTs: event.ts || last.endTs || last.ts,
        sourceOrder: last.sourceOrder ?? event.sourceOrder,
        endSourceOrder:
          event.sourceOrder ?? last.endSourceOrder ?? last.sourceOrder,
      };
    } else {
      next.events.push({
        ...event,
        startTs: event.startTs ?? event.ts,
        endTs: event.endTs ?? event.ts,
        endSourceOrder: event.endSourceOrder ?? event.sourceOrder,
      });
    }
    return next;
  }

  if (event.kind === 'tool') {
    const spanId = event.spanId || '';
    const tcid = event.toolCallId || '';
    let idx = -1;
    if (spanId) {
      for (let i = next.events.length - 1; i >= 0; i -= 1) {
        if (
          next.events[i]?.kind === 'tool' &&
          next.events[i]?.spanId === spanId
        ) {
          idx = i;
          break;
        }
      }
    } else if (tcid) {
      for (let i = next.events.length - 1; i >= 0; i -= 1) {
        if (
          next.events[i]?.kind === 'tool' &&
          next.events[i]?.toolCallId === tcid
        ) {
          idx = i;
          break;
        }
      }
    }
    if (idx === -1) {
      next.events.push({
        kind: 'tool',
        toolCallId: tcid,
        toolName: event.toolName || '',
        input: event.input || null,
        status: event.finishedStatus || 'running',
        finished: Boolean(event.finishedStatus),
        progress: event.progressEntry ? [event.progressEntry] : [],
        output: event.output || '',
        outputRef: event.outputRef || '',
        outputSize: event.outputSize ?? null,
        outputTruncated: Boolean(event.outputTruncated),
        outputProjection: event.outputProjection || null,
        elapsedMs: event.elapsedMs ?? null,
        error: event.error || '',
        traceId: event.traceId || '',
        spanId: event.spanId || '',
        parentSpanId: event.parentSpanId || '',
        sourceOrder: event.sourceOrder ?? null,
        endSourceOrder: event.sourceOrder ?? null,
        startTs: event.ts,
        endTs: event.finishedStatus ? event.ts : null,
      });
    } else {
      const prev = next.events[idx];
      next.events[idx] = {
        ...prev,
        toolName: event.toolName || prev.toolName,
        input: event.input || prev.input,
        status: event.finishedStatus || prev.status,
        finished: event.finishedStatus ? true : prev.finished,
        progress: event.progressEntry ?
          [...(prev.progress || []), event.progressEntry] :
          prev.progress,
        output: event.output || prev.output,
        outputRef: event.outputRef || prev.outputRef,
        outputSize: event.outputSize ?? prev.outputSize,
        outputTruncated: event.outputTruncated ?? prev.outputTruncated,
        outputProjection: event.outputProjection || prev.outputProjection,
        elapsedMs: event.elapsedMs ?? prev.elapsedMs,
        error: event.error || prev.error,
        traceId: event.traceId || prev.traceId,
        spanId: event.spanId || prev.spanId,
        parentSpanId: event.parentSpanId || prev.parentSpanId,
        endSourceOrder:
          event.sourceOrder ?? prev.endSourceOrder ?? prev.sourceOrder,
        endTs: event.finishedStatus ? event.ts : prev.endTs,
      };
    }
    return next;
  }

  next.events.push(event);
  return next;
};

// eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
const buildToolEventFromPayload = (type, payload, ts, sourceOrder = null) => {
  const toolCallId =
    typeof payload?.tool_call_id === 'string' ? payload.tool_call_id : '';
  const toolName =
    typeof payload?.tool_name === 'string' ? payload.tool_name : '';
  if (type === 'tool.started') {
    return {
      kind: 'tool',
      toolCallId,
      toolName,
      traceId: payload?.trace_id || '',
      spanId: payload?.span_id || '',
      parentSpanId: payload?.parent_span_id || '',
      input: payload?.input || null,
      sourceOrder,
      ts,
    };
  }
  if (type === 'tool.progress') {
    const ontologyEvent = payload?.progress?.business_data_search_event || null;
    const webSearchEvent = payload?.progress?.web_search_event || null;
    const text =
      payload?.message ||
      (ontologyEvent?.content ? String(ontologyEvent.content) : '') ||
      (webSearchEvent?.content ? String(webSearchEvent.content) : '');
    return {
      kind: 'tool',
      toolCallId,
      toolName,
      traceId: payload?.trace_id || '',
      spanId: payload?.span_id || '',
      parentSpanId: payload?.parent_span_id || '',
      progressEntry: {
        text,
        ontology: ontologyEvent,
        web: webSearchEvent,
        ts,
      },
      sourceOrder,
      ts,
    };
  }
  if (type === 'tool.finished') {
    let legacyOutput = '';
    if (!payload?.output_preview && payload?.output != null) {
      try {
        legacyOutput =
          typeof payload.output === 'string' ?
            payload.output :
            JSON.stringify(payload.output);
      } catch {
        legacyOutput = '';
      }
    }
    return {
      kind: 'tool',
      toolCallId,
      toolName,
      traceId: payload?.trace_id || '',
      spanId: payload?.span_id || '',
      parentSpanId: payload?.parent_span_id || '',
      finishedStatus: normalizeToolStatus(payload?.status) || 'completed',
      output: payload?.output_preview || legacyOutput,
      outputRef: payload?.output_ref || '',
      outputSize: payload?.output_size ?? null,
      outputTruncated: Boolean(payload?.output_truncated),
      outputProjection: payload?.output_projection || null,
      elapsedMs: payload?.elapsed_ms ?? null,
      error: payload?.error || '',
      sourceOrder,
      ts,
    };
  }
  return null;
};

const routeEventToSubagentChild = (segments, subagentId, event, meta = {}) => {
  return updateSubagentChildInSegments(segments, subagentId, meta, (child) =>
    appendChildEvent(child, event),
  );
};

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export const applyPublicEvent = (current, publicEvent) => {
  if (!isPublicEvent(publicEvent)) {
    return current;
  }

  const next = {
    ...current,
    streamSegments: Array.isArray(current.streamSegments) ?
      current.streamSegments.slice() :
      [],
  };

  const type = publicEvent.event_type;
  const payload = {
    ...(publicEvent.payload || {}),
    ...(publicEvent.trace_id ? { trace_id: publicEvent.trace_id } : {}),
    ...(publicEvent.span_id ? { span_id: publicEvent.span_id } : {}),
    ...(publicEvent.parent_span_id ?
      { parent_span_id: publicEvent.parent_span_id } :
      {}),
  };
  const eventTs = publicEventTimestamp(publicEvent);
  const sourceNode = publicEvent?.source?.node || 'agent';
  // Trace linkage is optional compatibility metadata on historical events.
  const eventSpanKey =
    typeof publicEvent?.source?.span_key === 'string' ?
      publicEvent.source.span_key :
      '';
  const eventParentKey =
    typeof publicEvent?.source?.parent_key === 'string' ?
      publicEvent.source.parent_key :
      '';
  const eventSourceOrder =
    publicEvent?.seq != null && Number.isFinite(Number(publicEvent.seq)) ?
      Number(publicEvent.seq) :
      null;
  const subagentIdInPayload =
    typeof payload?.subagent_id === 'string' && payload.subagent_id ?
      payload.subagent_id :
      null;

  const isSubagentToolEvent =
    subagentIdInPayload &&
    (type === 'tool.started' ||
      type === 'tool.progress' ||
      type === 'tool.finished');
  if (isSubagentToolEvent) {
    const event = buildToolEventFromPayload(
      type,
      payload,
      eventTs,
      eventSourceOrder,
    );
    if (event) {
      const routed = routeEventToSubagentChild(
        next.streamSegments,
        subagentIdInPayload,
        event,
        {
          groupId: payload.group_id,
          taskId: payload.task_id,
          task: payload.task,
          taskIndex: payload.task_index,
          ts: eventTs,
        },
      );
      if (routed) {
        next.streamSegments = routed;
        next.status = 'running';
        next.isLoading = true;
        return next;
      }
    }
  }

  if (subagentIdInPayload && type === 'token.delta') {
    const event = {
      kind: 'token',
      channel: payload.channel === 'reasoning' ? 'reasoning' : 'content',
      text: payload.text == null ? '' : String(payload.text),
      traceId: payload.trace_id || '',
      spanId: payload.span_id || '',
      parentSpanId: payload.parent_span_id || '',
      sourceOrder: eventSourceOrder,
      ts: eventTs,
    };
    const routed = routeEventToSubagentChild(
      next.streamSegments,
      subagentIdInPayload,
      event,
      {
        groupId: payload.group_id,
        taskId: payload.task_id,
        task: payload.task,
        taskIndex: payload.task_index,
        ts: eventTs,
      },
    );
    if (routed) {
      next.streamSegments = routed;
      next.status = 'running';
      next.isLoading = true;
      return next;
    }
  }

  if (type === 'subagent_group.started') {
    const groupId = payload.group_id || `group_${Date.now()}`;
    const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    const parentSubagentId =
      typeof payload.parent_subagent_id === 'string' &&
      payload.parent_subagent_id ?
        payload.parent_subagent_id :
        null;
    const pendingTasks = {};
    tasks.forEach((task) => {
      const tid = task?.task_id || task?.id;
      if (tid) {
        pendingTasks[tid] = {
          taskId: tid,
          taskIndex: typeof task?.task_index === 'number' ? task.task_index : 0,
          task: task?.task || '',
        };
      }
    });
    const groupSegment = {
      type: 'subagent_group',
      groupId,
      depth: payload.depth || 0,
      parentSubagentId,
      taskCount: tasks.length || payload.task_count || 0,
      status: 'running',
      children: {},
      taskOrder: [],
      pendingTasks,
      finished: false,
      sourceOrder: eventSourceOrder,
      startTs: eventTs,
      traceId: payload.trace_id || '',
      spanId: payload.span_id || '',
      parentSpanId: payload.parent_span_id || '',
      meta: payload,
    };
    if (parentSubagentId) {
      const nestedSegments = appendChildGroupToSubagent(
        next.streamSegments,
        parentSubagentId,
        groupSegment,
      );
      if (nestedSegments) {
        next.streamSegments = nestedSegments;
        next.status = 'running';
        next.isLoading = true;
        next.currentSegmentType = 'subagent_group';
        return next;
      }
    }

    next.streamSegments = closeOpenSegment(next.streamSegments);
    next.streamSegments.push(groupSegment);
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'subagent_group';
    return next;
  }

  if (type === 'subagent_group.finished') {
    const normalizedStatus = normalizeToolStatus(payload.status);
    let status = 'completed';
    if (normalizedStatus === 'cancelled') {
      status = 'cancelled';
    } else if (
      normalizedStatus === 'error' ||
      (payload.error_count || 0) > 0
    ) {
      status = 'error';
    }
    let terminalStatus = 'success';
    if (status === 'cancelled') {
      terminalStatus = 'cancelled';
    } else if (status === 'error') {
      terminalStatus = 'error';
    }
    const updatedSegments = updateGroupByGroupId(
      next.streamSegments,
      payload.group_id,
      (group) => ({
        ...closeSubagentGroupForTerminalTurn(
          group,
          terminalStatus,
          eventTs,
        ),
        status,
        finished: true,
        endTs: eventTs,
        meta: { ...(group?.meta || {}), ...payload },
      }),
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      if (status === 'error') {
        next.error = true;
      }
    }
    next.currentSegmentType = null;
    return next;
  }

  if (type === 'subagent.started' && subagentIdInPayload) {
    const groupId = payload.group_id;
    const taskId = payload.task_id || '';
    const updatedSegments = updateGroupByGroupId(
      next.streamSegments,
      groupId,
      // eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
      (group) => {
        const pending =
          (group.pendingTasks && group.pendingTasks[taskId]) || {};
        const taskText = payload.task || pending.task || '';
        const { nextGroup, child } = ensureSubagentChild(
          group,
          subagentIdInPayload,
          {
            groupId,
            taskId,
            task: taskText,
            taskIndex: payload.task_index,
            ts: eventTs,
          },
        );
        nextGroup.children[subagentIdInPayload] = {
          ...cloneChild(child),
          subagentId: subagentIdInPayload,
          taskId,
          taskIndex: normalizeTaskIndex(
            payload.task_index,
            child?.taskIndex ?? pending?.taskIndex ?? 0,
          ),
          task: taskText || child?.task || '',
          status: payload.status || 'running',
          startTs: child?.startTs || eventTs,
          traceId: payload.trace_id || child?.traceId || '',
          spanId: payload.span_id || child?.spanId || '',
          parentSpanId: payload.parent_span_id || child?.parentSpanId || '',
        };
        return nextGroup;
      },
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      next.status = 'running';
      next.isLoading = true;
      next.currentSegmentType = 'subagent_group';
      return next;
    }
  }

  if (type === 'subagent.delta' && subagentIdInPayload) {
    const event = {
      kind: 'token',
      eventType: type,
      channel: payload.channel || 'content',
      text: payload.text == null ? '' : String(payload.text),
      traceId: payload.trace_id || '',
      spanId: payload.span_id || '',
      parentSpanId: payload.parent_span_id || '',
      meta: payload,
      sourceOrder: eventSourceOrder,
      ts: eventTs,
    };
    const routed = routeEventToSubagentChild(
      next.streamSegments,
      subagentIdInPayload,
      event,
      {
        groupId: payload.group_id,
        taskId: payload.task_id,
        task: payload.task,
        taskIndex: payload.task_index,
        ts: eventTs,
      },
    );
    if (routed) {
      next.streamSegments = routed;
      next.status = 'running';
      next.isLoading = true;
      next.currentSegmentType = 'subagent_group';
      return next;
    }
  }

  if (type === 'subagent.progress' && subagentIdInPayload) {
    const updatedSegments = updateSubagentChildInSegments(
      next.streamSegments,
      subagentIdInPayload,
      {
        groupId: payload.group_id,
        taskId: payload.task_id,
        task: payload.task,
        taskIndex: payload.task_index,
        ts: eventTs,
      },
      (child) => ({
        ...cloneChild(child),
        status: payload.status || child.status,
        traceId: payload.trace_id || child?.traceId || '',
        spanId: payload.span_id || child?.spanId || '',
        parentSpanId: payload.parent_span_id || child?.parentSpanId || '',
      }),
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      return next;
    }
  }

  if (type === 'subagent.finished' && subagentIdInPayload) {
    const finalStatus =
      normalizeToolStatus(payload.status) || payload.status || 'completed';
    const subagentError = hasPublicErrorValue(payload.error) ?
      publicErrorMessage(payload.error, '子 Agent 执行失败') :
      null;
    const updatedSegments = updateSubagentChildInSegments(
      next.streamSegments,
      subagentIdInPayload,
      {
        groupId: payload.group_id,
        taskId: payload.task_id,
        task: payload.task,
        taskIndex: payload.task_index,
        ts: eventTs,
      },
      (child) => ({
        ...cloneChild(child),
        status: finalStatus,
        endTs: eventTs,
        events: closeSubagentEventsForTerminal(
          child?.events,
          finalStatus,
          eventTs,
        ),
        result: payload.result || null,
        error: subagentError,
        traceId: payload.trace_id || child?.traceId || '',
        spanId: payload.span_id || child?.spanId || '',
        parentSpanId: payload.parent_span_id || child?.parentSpanId || '',
      }),
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      if (finalStatus === 'error') {
        next.error = true;
      }
      return next;
    }
  }

  if (type === 'turn.started') {
    next.status = 'running';
    next.isLoading = true;
    next.meta = payload;
    next.startTs = eventTs;
    next.currentSegmentType = null;
    next.terminalToolBatchReached = false;
    return next;
  }

  const suppressTrailingDelta =
    next.terminalToolBatchReached &&
    (type === 'token.delta' ||
      type === 'reasoning.delta' ||
      type === 'reasoning.summary');
  if (suppressTrailingDelta) {
    return next;
  }

  if (type === 'token.delta' || type === 'reasoning.delta') {
    const channel =
      type === 'reasoning.delta' || payload.channel === 'reasoning' ?
        'reasoning' :
        'content';
    const text = payload.text == null ? '' : String(payload.text);
    next.streamSegments = appendInlineTextSegment(next.streamSegments, {
      type: channel,
      state: 'streaming',
      text,
      node: sourceNode,
      meta: payload,
      ts: eventTs,
      traceId: payload.trace_id || '',
      spanId: payload.span_id || '',
      parentSpanId: payload.parent_span_id || '',
      spanKey: eventSpanKey,
      parentKey: eventParentKey || iterationParentKeyFromPayload(payload),
    });
    if (channel === 'reasoning') {
      next.reasoning = `${next.reasoning || ''}${text}`;
    } else {
      next.content = `${next.content || ''}${text}`;
    }
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = channel;
    return next;
  }

  if (type === 'iteration.completed') {
    next.streamSegments = closeOpenSegment(next.streamSegments);
    next.iterationMetrics = upsertIterationMetric(
      current?.iterationMetrics,
      payload,
      eventTs,
    );
    if (payload.status === 'terminal_tool_batch') {
      next.terminalToolBatchReached = true;
    }
    next.currentSegmentType = null;
    return next;
  }

  if (type === 'interaction.requested') {
    const interactionId = String(
      payload.interaction_id || payload.id || '',
    ).trim();
    if (!interactionId) {
      return next;
    }
    const prompt = typeof payload.prompt === 'string' ? payload.prompt : '';
    next.interaction = {
      id: interactionId,
      type: payload.type || 'input',
      prompt,
      options: Array.isArray(payload.options) ? payload.options : [],
    };
    next.options = next.interaction.options;
    next.messageType = 'user_interaction';
    if (prompt && !next.content) {
      next.content = prompt;
    }
    if (publicEvent.status === 'awaiting_input') {
      next.streamSegments = closeUnfinishedSegmentsForTerminalTurn(
        next.streamSegments,
        'awaiting_input',
        eventTs,
      );
      next.status = 'awaiting';
      next.isLoading = false;
      next.endTs = eventTs;
      next.currentSegmentType = null;
    }
    return next;
  }

  if (type === 'reasoning.summary') {
    const summary =
      typeof payload.summary === 'string' ? payload.summary.trim() : '';
    if (!summary) {
      return next;
    }
    const blockId =
      typeof payload.reasoning_block_id === 'string' ?
        payload.reasoning_block_id.trim() :
        '';

    if (blockId) {
      const idxByBlockId = findLastSegmentIndex(
        next.streamSegments,
        (segment) =>
          segment?.type === 'reasoning' &&
          String(segment?.meta?.reasoning_block_id || '').trim() === blockId,
      );
      if (idxByBlockId !== -1) {
        const target = next.streamSegments[idxByBlockId];
        next.streamSegments[idxByBlockId] = {
          ...target,
          meta: {
            ...(target?.meta || {}),
            ...(payload || {}),
            reasoning_summary: summary,
          },
        };
        return next;
      }
    }

    const sourceNodeName =
      typeof sourceNode === 'string' ? sourceNode.trim() : '';
    const matchReasoningSegment = (segment) =>
      segment?.type === 'reasoning' &&
      (!sourceNodeName || (segment?.node || '') === sourceNodeName);
    const targetOpenIdx = findLastSegmentIndex(
      next.streamSegments,
      (segment) => matchReasoningSegment(segment) && !segment?.finished,
    );
    const targetAnyIdx =
      targetOpenIdx !== -1 ?
        targetOpenIdx :
        findLastSegmentIndex(next.streamSegments, matchReasoningSegment);
    if (targetAnyIdx === -1) {
      return next;
    }

    const target = next.streamSegments[targetAnyIdx];
    next.streamSegments[targetAnyIdx] = {
      ...target,
      meta: {
        ...(target?.meta || {}),
        ...(payload || {}),
        reasoning_summary: summary,
      },
    };
    return next;
  }

  if (SOLVER_EVENT_TYPES.has(type)) {
    const event = projectSolverEvent(type, payload);
    const locator = resolveToolLocator(event);
    const targetIdx = findLastSegmentIndex(next.streamSegments, (segment) =>
      segmentMatchesToolLocator(segment, locator),
    );
    if (targetIdx !== -1) {
      const target = next.streamSegments[targetIdx];
      next.streamSegments[targetIdx] = {
        ...target,
        text: event.message || target.text,
        meta: mergeSolverEventMeta(target.meta, event),
      };
    } else if (hasToolLocator(locator)) {
      next.streamSegments = closeOpenNonToolSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: 'running',
        text: event.message || '求解处理中',
        node: sourceNode,
        meta: mergeSolverEventMeta({}, event),
        finished: false,
        toolStatus: null,
        startTs: eventTs,
        endTs: null,
        spanKey: eventSpanKey || undefined,
        parentKey: eventParentKey || undefined,
      });
    }
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'tool';
    return next;
  }

  if (type === 'tool.started') {
    const publicPayload = withPublicToolInput(payload);
    const locator = resolveToolLocator(publicPayload);
    const toolName = locator.toolName || 'unknown_tool';
    const targetIdx = hasToolLocator(locator) ?
      findLastSegmentIndex(next.streamSegments, (segment) =>
        segmentMatchesToolLocator(segment, locator),
      ) :
      -1;

    if (targetIdx !== -1) {
      const target = next.streamSegments[targetIdx];
      next.streamSegments[targetIdx] = {
        ...target,
        state: 'running',
        text: publicPayload.message || target.text || `调用工具: ${toolName}`,
        node: sourceNode || target.node,
        meta: mergeToolMeta(target.meta, publicPayload),
        traceId: payload.trace_id || target.traceId,
        spanId: payload.span_id || target.spanId,
        parentSpanId: payload.parent_span_id || target.parentSpanId,
        finished: false,
        startTs: target.startTs || eventTs,
        endTs: null,
        spanKey: target.spanKey || eventSpanKey || undefined,
        parentKey: target.parentKey || eventParentKey || undefined,
      };
    } else {
      next.streamSegments = closeOpenNonToolSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: 'running',
        text: publicPayload.message || `调用工具: ${toolName}`,
        node: sourceNode,
        meta: publicPayload,
        traceId: payload.trace_id || '',
        spanId: payload.span_id || '',
        parentSpanId: payload.parent_span_id || '',
        finished: false,
        toolStatus: null,
        startTs: eventTs,
        endTs: null,
        spanKey: eventSpanKey || undefined,
        parentKey: eventParentKey || undefined,
      });
    }
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'tool';
    return next;
  }

  if (type === 'tool.progress') {
    const text = payload.message == null ? '' : String(payload.message);
    const locator = resolveToolLocator(payload);
    const toolName = locator.toolName || 'unknown_tool';
    const dbSearchEvent = extractBusinessDataSearchProgressEvent(
      payload,
      publicEvent,
    );
    const webSearchEvent = extractWebSearchProgressEvent(payload, publicEvent);
    const targetOpenIdx = findLastSegmentIndex(
      next.streamSegments,
      (segment) =>
        segmentMatchesToolLocator(segment, locator) && !segment?.finished,
    );
    const targetAnyIdx = findLastSegmentIndex(next.streamSegments, (segment) =>
      segmentMatchesToolLocator(segment, locator),
    );

    if (targetOpenIdx !== -1 || targetAnyIdx !== -1) {
      const targetIdx = targetOpenIdx !== -1 ? targetOpenIdx : targetAnyIdx;
      const target = next.streamSegments[targetIdx];
      const mergedMeta = mergeToolMeta(target.meta, payload);
      next.streamSegments[targetIdx] = {
        ...target,
        state: targetOpenIdx !== -1 ? 'running' : target.state || 'running',
        text:
          text ||
          target.text ||
          (dbSearchEvent ? `business_data_search ${dbSearchEvent.type}` : '') ||
          (webSearchEvent ? `web_search ${webSearchEvent.type}` : ''),
        node: sourceNode || target.node,
        meta: appendWebSearchEventToMeta(
          appendBusinessDataSearchEventToMeta(mergedMeta, dbSearchEvent),
          webSearchEvent,
        ),
        traceId: payload.trace_id || target.traceId,
        spanId: payload.span_id || target.spanId,
        parentSpanId: payload.parent_span_id || target.parentSpanId,
        finished: targetOpenIdx !== -1 ? false : Boolean(target.finished),
        startTs: target.startTs || eventTs,
        spanKey: target.spanKey || eventSpanKey || undefined,
        parentKey: target.parentKey || eventParentKey || undefined,
      };
    } else if (dbSearchEvent || webSearchEvent || hasToolLocator(locator)) {
      next.streamSegments = closeOpenNonToolSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: 'running',
        text:
          text ||
          (dbSearchEvent ? `business_data_search ${dbSearchEvent.type}` : '') ||
          (webSearchEvent ? `web_search ${webSearchEvent.type}` : '') ||
          `调用工具: ${toolName}`,
        node: sourceNode,
        meta: appendWebSearchEventToMeta(
          appendBusinessDataSearchEventToMeta(
            mergeToolMeta({}, payload),
            dbSearchEvent,
          ),
          webSearchEvent,
        ),
        traceId: payload.trace_id || '',
        spanId: payload.span_id || '',
        parentSpanId: payload.parent_span_id || '',
        finished: false,
        toolStatus: null,
        startTs: eventTs,
        endTs: null,
        spanKey: eventSpanKey || undefined,
        parentKey: eventParentKey || undefined,
      });
    } else {
      next.streamSegments = appendInlineTextSegment(next.streamSegments, {
        type: 'tool',
        state: 'running',
        text,
        node: sourceNode,
        meta: payload,
        mergeKey: 'tool_call_id',
        ts: eventTs,
        traceId: payload.trace_id || '',
        spanId: payload.span_id || '',
        parentSpanId: payload.parent_span_id || '',
        spanKey: eventSpanKey || undefined,
        parentKey: eventParentKey || undefined,
      });
    }
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'tool';
    return next;
  }

  if (type === 'tool.finished') {
    const locator = resolveToolLocator(payload);
    const toolName = locator.toolName || 'unknown_tool';
    const hasLocator = hasToolLocator(locator);
    const toolStatus = normalizeToolStatus(payload.status);
    const toolError = hasPublicErrorValue(payload.error) ?
      publicErrorMessage(payload.error, '工具执行失败') :
      '';
    const publicPayload = toolError ?
      { ...payload, error: toolError } :
      payload;
    const targetIdx = findLastSegmentIndex(next.streamSegments, (segment) =>
      hasLocator ?
        segmentMatchesToolLocator(segment, locator) :
        segment?.type === 'tool' && !segment?.finished,
    );

    if (targetIdx !== -1) {
      const target = next.streamSegments[targetIdx];
      next.streamSegments[targetIdx] = {
        ...target,
        meta: mergeToolMeta(target.meta, publicPayload),
        traceId: payload.trace_id || target.traceId,
        spanId: payload.span_id || target.spanId,
        parentSpanId: payload.parent_span_id || target.parentSpanId,
        state: toolStatus || 'completed',
        text: target.text || payload.output_preview || toolError,
        finished: true,
        toolStatus,
        startTs:
          target.startTs ||
          (Number.isFinite(Number(payload.elapsed_ms)) ?
            eventTs - Number(payload.elapsed_ms) :
            eventTs),
        endTs: eventTs,
        spanKey: target.spanKey || eventSpanKey || undefined,
        parentKey: target.parentKey || eventParentKey || undefined,
      };
    } else {
      next.streamSegments = closeOpenSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: toolStatus || 'completed',
        text: payload.output_preview || toolError || `工具完成: ${toolName}`,
        node: sourceNode,
        meta: publicPayload,
        traceId: payload.trace_id || '',
        spanId: payload.span_id || '',
        parentSpanId: payload.parent_span_id || '',
        finished: true,
        toolStatus,
        startTs: Number.isFinite(Number(payload.elapsed_ms)) ?
          eventTs - Number(payload.elapsed_ms) :
          eventTs,
        endTs: eventTs,
        spanKey: eventSpanKey || undefined,
        parentKey: eventParentKey || undefined,
      });
    }

    next.currentSegmentType = null;
    if (toolStatus === 'error') {
      next.error = true;
    }
    return next;
  }

  if (type === 'subagent.delta') {
    const text = payload.text == null ? '' : String(payload.text);
    next.streamSegments = appendInlineTextSegment(next.streamSegments, {
      type: 'subagent',
      state: payload.channel || 'content',
      text,
      node: payload.agent_type || 'subagent',
      meta: payload,
      mergeKey: 'subagent_id',
      ts: eventTs,
      traceId: payload.trace_id || '',
      spanId: payload.span_id || '',
      parentSpanId: payload.parent_span_id || '',
    });
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'subagent';
    return next;
  }

  if (
    type === 'subagent.started' ||
    type === 'subagent.progress' ||
    type === 'subagent.finished'
  ) {
    let fallbackText = `子智能体状态更新: ${payload.agent_type || 'subagent'}`;
    if (type === 'subagent.started') {
      fallbackText = `子智能体启动: ${payload.agent_type || 'subagent'}`;
    } else if (type === 'subagent.finished') {
      fallbackText = `子智能体结束: ${payload.agent_type || 'subagent'}`;
    }
    const text = payload.text || payload.step || fallbackText;

    next.streamSegments = closeOpenSegment(next.streamSegments);
    next.streamSegments.push({
      type: 'subagent',
      state:
        payload.status ||
        (type === 'subagent.finished' ? 'completed' : 'running'),
      text,
      node: payload.agent_type || 'subagent',
      meta: payload,
      finished: type !== 'subagent.progress',
    });

    next.status = 'running';
    next.isLoading = true;
    if (
      type === 'subagent.finished' &&
      normalizeToolStatus(payload.status) === 'error'
    ) {
      next.error = true;
    }
    next.currentSegmentType = type === 'subagent.progress' ? 'subagent' : null;
    return next;
  }

  if (type === 'turn.finished') {
    return finalizeTurnState(next, publicEvent.status, payload, eventTs);
  }

  return next;
};
