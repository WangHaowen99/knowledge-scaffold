// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
/**
 * Project canonical PublicEvent records into chat UI state.
 */

import { isPublicEvent, publicEventTimestamp } from './publicEvent.js';
import {
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
} from './streamProtocolHelpers.js';

export { friendlyRuntimeError, publicErrorMessage };

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

const inlineSegmentSpanMatches = (last, spanId, spanKey, parentKey) => {
  if (spanId || last?.spanId) {
    return Boolean(spanId && last?.spanId === spanId);
  }
  return (
    (last?.spanKey || '') === spanKey &&
    (last?.parentKey || '') === parentKey
  );
};

const inlineSegmentMergeKeyMatches = (last, meta, mergeKey) => {
  if (!mergeKey) {
    return true;
  }
  return (
    typeof last?.meta?.[mergeKey] === 'string' &&
    typeof meta?.[mergeKey] === 'string' &&
    last.meta[mergeKey] === meta[mergeKey]
  );
};

const inlineSegmentCanMerge = (last, options) => {
  const {
    type,
    state,
    node,
    meta,
    mergeKey,
    spanId,
    spanKey,
    parentKey,
  } = options;
  return Boolean(
    last &&
      !last.finished &&
      last.type === type &&
      last.state === state &&
      (last.node || '') === (node || '') &&
      inlineSegmentSpanMatches(last, spanId, spanKey, parentKey) &&
      inlineSegmentMergeKeyMatches(last, meta, mergeKey),
  );
};

const mergedInlineSegment = (last, options) => ({
  ...last,
  text: `${last.text || ''}${options.text}`,
  meta: options.meta || last.meta,
  traceId: options.traceId || last.traceId,
  spanId: options.spanId || last.spanId,
  parentSpanId: options.parentSpanId || last.parentSpanId,
  endTs: options.ts || Date.now(),
});

const newInlineSegment = (options) => ({
  type: options.type,
  state: options.state,
  text: options.text,
  node: options.node,
  meta: options.meta,
  traceId: options.traceId || undefined,
  spanId: options.spanId || undefined,
  parentSpanId: options.parentSpanId || undefined,
  spanKey: options.spanKey || undefined,
  parentKey: options.parentKey || undefined,
  finished: false,
  startTs: options.ts || Date.now(),
  endTs: options.ts || Date.now(),
});

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
) => {
  if (!text) {
    return segments;
  }

  const next = Array.isArray(segments) ? segments.slice() : [];
  const last = next[next.length - 1];
  const canMerge = inlineSegmentCanMerge(last, {
    type,
    state,
    node,
    meta,
    mergeKey,
    spanId,
    spanKey,
    parentKey,
  });

  if (!canMerge && String(text).trim().length === 0) {
    return next;
  }

  if (canMerge) {
    next[next.length - 1] = mergedInlineSegment(last, {
      text,
      meta,
      traceId,
      spanId,
      parentSpanId,
      ts,
    });
    return next;
  }

  return [
    ...closeOpenSegment(next),
    newInlineSegment({
      type,
      state,
      text,
      node,
      meta,
      traceId: traceId || undefined,
      spanId: spanId || undefined,
      parentSpanId: parentSpanId || undefined,
      spanKey,
      parentKey,
      ts,
    }),
  ];
};

const iterationParentKeyFromPayload = (payload = {}) => {
  const iteration = Number(payload?.iteration);
  if (!Number.isFinite(iteration) || iteration < 1) {
    return '';
  }
  return `iteration:main:${Math.floor(iteration)}`;
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

const applyTerminalStatus = (next, status) => {
  if (status === 'success') {
    next.status = 'finished';
  } else if (status === 'awaiting_input') {
    next.status = 'awaiting';
  } else if (status === 'cancelled') {
    next.status = 'aborted';
    next.error = true;
  } else {
    next.status = 'error';
    next.error = true;
  }
};

const applyTerminalOutput = (next, terminal) => {
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
};

const setTerminalMeta = (next, key, value) => {
  if (value) {
    next.meta = { ...(next.meta || {}), [key]: value };
  }
};

const applyTerminalMetadata = (next, terminal) => {
  setTerminalMeta(next, 'error', terminal.error);
  setTerminalMeta(next, 'error_code', terminal.errorCode);
  const runtimeError = friendlyRuntimeError(terminal.errorCode);
  if (runtimeError?.incomplete) {
    setTerminalMeta(next, 'incomplete', true);
  }
  if (terminal.errorRetriable) {
    setTerminalMeta(next, 'error_retriable', true);
  }
  setTerminalMeta(next, 'cancel_reason', terminal.cancelReason);
  setTerminalMeta(next, 'recovery', terminal.recovery);
};

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

  applyTerminalStatus(next, terminal.status);

  next.isLoading = false;
  next.endTs = eventTs;
  next.currentSegmentType = null;

  applyTerminalOutput(next, terminal);
  applyTerminalMetadata(next, terminal);

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

const finiteEventOrder = (value) =>
  value != null && value !== '' && Number.isFinite(Number(value)) ?
    Number(value) :
    null;

const tokenInterruptedByChildGroup = (child, last) => {
  const lastStartedAt = Number(last?.startTs ?? last?.ts);
  const lastSourceOrder = finiteEventOrder(
    last?.endSourceOrder ?? last?.sourceOrder,
  );
  const latestGroupSourceOrder = Math.max(
    ...getChildGroups(child)
      .map((group) => finiteEventOrder(group?.sourceOrder))
      .filter((value) => value != null),
    Number.NEGATIVE_INFINITY,
  );
  const latestGroupStartedAt = Math.max(
    ...getChildGroups(child)
      .map((group) => Number(group?.startTs))
      .filter(Number.isFinite),
    Number.NEGATIVE_INFINITY,
  );
  if (Number.isFinite(latestGroupSourceOrder) && lastSourceOrder != null) {
    return latestGroupSourceOrder > lastSourceOrder;
  }
  return Number.isFinite(lastStartedAt) && latestGroupStartedAt > lastStartedAt;
};

const childTokenCanMerge = (child, event, last) => {
  if (!last || last.kind !== 'token' || last.channel !== event.channel) {
    return false;
  }
  const usesSpanIdentity = Boolean(event.spanId || last?.spanId);
  const sameSpan = usesSpanIdentity ?
    Boolean(event.spanId && last?.spanId === event.spanId) :
    true;
  return sameSpan && !tokenInterruptedByChildGroup(child, last);
};

const appendChildTokenEvent = (child, event) => {
  const last = child.events[child.events.length - 1];
  if (childTokenCanMerge(child, event, last)) {
    child.events[child.events.length - 1] = {
      ...last,
      text: `${last.text || ''}${event.text || ''}`,
      startTs: last.startTs ?? last.ts,
      endTs: event.ts || last.endTs || last.ts,
      sourceOrder: last.sourceOrder ?? event.sourceOrder,
      endSourceOrder:
        event.sourceOrder ?? last.endSourceOrder ?? last.sourceOrder,
    };
  } else {
    child.events.push({
      ...event,
      startTs: event.startTs ?? event.ts,
      endTs: event.endTs ?? event.ts,
      endSourceOrder: event.endSourceOrder ?? event.sourceOrder,
    });
  }
  return child;
};

const childToolEventIndex = (events, event) => {
  const spanId = event.spanId || '';
  if (spanId) {
    return findLastSegmentIndex(
      events,
      (item) => item?.kind === 'tool' && item?.spanId === spanId,
    );
  }
  const toolCallId = event.toolCallId || '';
  if (!toolCallId) {
    return -1;
  }
  return findLastSegmentIndex(
    events,
    (item) => item?.kind === 'tool' && item?.toolCallId === toolCallId,
  );
};

const newChildToolEvent = (event) => ({
  kind: 'tool',
  toolCallId: event.toolCallId || '',
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

const mergeChildToolEvent = (previous, event) => ({
  ...previous,
  toolName: event.toolName || previous.toolName,
  input: event.input || previous.input,
  status: event.finishedStatus || previous.status,
  finished: event.finishedStatus ? true : previous.finished,
  progress: event.progressEntry ?
    [...(previous.progress || []), event.progressEntry] :
    previous.progress,
  output: event.output || previous.output,
  outputRef: event.outputRef || previous.outputRef,
  outputSize: event.outputSize ?? previous.outputSize,
  outputTruncated: event.outputTruncated ?? previous.outputTruncated,
  outputProjection: event.outputProjection || previous.outputProjection,
  elapsedMs: event.elapsedMs ?? previous.elapsedMs,
  error: event.error || previous.error,
  traceId: event.traceId || previous.traceId,
  spanId: event.spanId || previous.spanId,
  parentSpanId: event.parentSpanId || previous.parentSpanId,
  endSourceOrder:
    event.sourceOrder ?? previous.endSourceOrder ?? previous.sourceOrder,
  endTs: event.finishedStatus ? event.ts : previous.endTs,
});

const appendChildToolEvent = (child, event) => {
  const index = childToolEventIndex(child.events, event);
  if (index === -1) {
    child.events.push(newChildToolEvent(event));
  } else {
    child.events[index] = mergeChildToolEvent(child.events[index], event);
  }
  return child;
};

const appendChildEvent = (child, event) => {
  const next = cloneChild(child);
  if (event.kind === 'token') {
    return appendChildTokenEvent(next, event);
  }
  if (event.kind === 'tool') {
    return appendChildToolEvent(next, event);
  }
  next.events.push(event);
  return next;
};

const toolEventBase = (payload, ts, sourceOrder) => ({
  kind: 'tool',
  toolCallId:
    typeof payload?.tool_call_id === 'string' ? payload.tool_call_id : '',
  toolName: typeof payload?.tool_name === 'string' ? payload.tool_name : '',
  traceId: payload?.trace_id || '',
  spanId: payload?.span_id || '',
  parentSpanId: payload?.parent_span_id || '',
  sourceOrder,
  ts,
});

const startedToolEvent = (payload, ts, sourceOrder) => ({
  ...toolEventBase(payload, ts, sourceOrder),
  input: payload?.input || null,
});

const progressToolEvent = (payload, ts, sourceOrder) => {
  const ontologyEvent = payload?.progress?.business_data_search_event || null;
  const webSearchEvent = payload?.progress?.web_search_event || null;
  const text =
    payload?.message ||
    (ontologyEvent?.content ? String(ontologyEvent.content) : '') ||
    (webSearchEvent?.content ? String(webSearchEvent.content) : '');
  return {
    ...toolEventBase(payload, ts, sourceOrder),
    progressEntry: {
      text,
      ontology: ontologyEvent,
      web: webSearchEvent,
      ts,
    },
  };
};

const legacyToolOutput = (payload) => {
  if (payload?.output_preview || payload?.output == null) {
    return '';
  }
  try {
    return typeof payload.output === 'string' ?
      payload.output :
      JSON.stringify(payload.output);
  } catch {
    return '';
  }
};

const finishedToolEvent = (payload, ts, sourceOrder) => ({
  ...toolEventBase(payload, ts, sourceOrder),
  finishedStatus: normalizeToolStatus(payload?.status) || 'completed',
  output: payload?.output_preview || legacyToolOutput(payload),
  outputRef: payload?.output_ref || '',
  outputSize: payload?.output_size ?? null,
  outputTruncated: Boolean(payload?.output_truncated),
  outputProjection: payload?.output_projection || null,
  elapsedMs: payload?.elapsed_ms ?? null,
  error: payload?.error || '',
});

const buildToolEventFromPayload = (type, payload, ts, sourceOrder = null) => {
  if (type === 'tool.started') {
    return startedToolEvent(payload, ts, sourceOrder);
  }
  if (type === 'tool.progress') {
    return progressToolEvent(payload, ts, sourceOrder);
  }
  if (type === 'tool.finished') {
    return finishedToolEvent(payload, ts, sourceOrder);
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
