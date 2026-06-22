/**
 * Shared helpers for agent-stream.v2 event processing.
 */

export const AGENT_STREAM_PROTOCOL = 'agent-stream.v2';
export const AGENT_STREAM_EVENT = 'agent.v2';

const TERMINAL_STATUSES = new Set(['success', 'error', 'cancelled', 'awaiting_input']);
const BUSINESS_DATA_SEARCH_EVENT_TYPES = new Set(['start', 'intermediate', 'final']);
const WEB_SEARCH_EVENT_TYPES = new Set(['start', 'final', 'error']);
const DATA_SEARCH_TOOL_NAMES = new Set(['business_data_search']);

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
  cancelled: 'error',
  canceled: 'error',
  aborted: 'error',
  abort: 'error',
};

const normalizeToolStatus = (value) => {
  if (typeof value !== 'string') return null;
  return TOOL_STATUS_ICON_MAP[value.trim().toLowerCase()] || null;
};

const normalizeToolCallId = (value) => (
  typeof value === 'string' ? value.trim() : ''
);

const normalizeToolName = (value) => (
  typeof value === 'string' ? value.trim() : ''
);

const extractToolQuery = (meta = {}) => String(
  meta?.input?.query
  ?? meta?.tool_input?.query
  ?? meta?.tool_args?.query
  ?? meta?.query
  ?? ''
).trim();

const resolveToolLocator = (payload = {}) => {
  const toolCallId = normalizeToolCallId(payload?.tool_call_id);
  const toolName = normalizeToolName(payload?.tool_name);
  const query = extractToolQuery(payload);

  return {
    toolCallId,
    toolName,
    query,
    isOntology: DATA_SEARCH_TOOL_NAMES.has(toolName),
  };
};

const hasToolLocator = (locator = {}) => {
  if (locator?.toolCallId) return true;
  if (locator?.isOntology) return Boolean(locator?.query);
  return Boolean(locator?.toolName);
};

const segmentMatchesToolLocator = (segment, locator = {}) => {
  if (!segment || segment.type !== 'tool') return false;

  const segmentMeta = segment?.meta || {};
  const segmentToolCallId = normalizeToolCallId(segmentMeta?.tool_call_id);
  if (locator.toolCallId) {
    return segmentToolCallId === locator.toolCallId;
  }

  const segmentToolName = normalizeToolName(segmentMeta?.tool_name);
  if (locator.isOntology && locator.query) {
    if (!DATA_SEARCH_TOOL_NAMES.has(segmentToolName)) return false;
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
    const baseInput = baseMeta?.input && typeof baseMeta.input === 'object' ? baseMeta.input : {};
    const incomingInput = incomingMeta?.input && typeof incomingMeta.input === 'object' ? incomingMeta.input : {};
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

const closeOpenNonToolSegment = (segments = []) => {
  if (!Array.isArray(segments) || segments.length === 0) return Array.isArray(segments) ? segments : [];
  const last = segments[segments.length - 1];
  if (!last || last.finished || last.type === 'tool') return segments;
  return closeOpenSegment(segments);
};

export const isAgentV2Envelope = (payload) => (
  payload
  && typeof payload === 'object'
  && payload.protocol === AGENT_STREAM_PROTOCOL
  && typeof payload.type === 'string'
);

export const closeOpenSegment = (segments = []) => {
  if (!Array.isArray(segments) || segments.length === 0) return Array.isArray(segments) ? segments : [];
  const lastIdx = segments.length - 1;
  const last = segments[lastIdx];
  if (!last || last.finished) return segments;

  const next = segments.slice();
  next[lastIdx] = { ...last, finished: true };
  return next;
};

const closeUnfinishedSegmentsForTerminalTurn = (segments = [], terminalStatus = 'success', ts = Date.now()) => {
  if (!Array.isArray(segments) || segments.length === 0) return Array.isArray(segments) ? segments : [];

  const isFailure = terminalStatus === 'error' || terminalStatus === 'cancelled';
  return segments.map((segment) => {
    if (!segment || segment.finished) return segment;

    const closed = {
      ...segment,
      finished: true,
      endTs: ts || Date.now(),
    };
    if (segment.type !== 'tool') return closed;

    const terminalToolStatus = isFailure ? 'error' : 'completed';
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

const findLastSegmentIndex = (segments = [], predicate) => {
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    if (predicate(segments[i])) return i;
  }
  return -1;
};

const appendInlineTextSegment = (segments, { type, state, text, node, meta, mergeKey = null, ts }) => {
  if (!text) return segments;

  const next = Array.isArray(segments) ? segments.slice() : [];
  const last = next[next.length - 1];
  const canMerge = Boolean(
    last
    && !last.finished
    && last.type === type
    && last.state === state
    && (last.node || '') === (node || '')
    && (
      !mergeKey
      || (
        typeof last?.meta?.[mergeKey] === 'string'
        && typeof meta?.[mergeKey] === 'string'
        && last.meta[mergeKey] === meta[mergeKey]
      )
    )
  );

  if (!canMerge && String(text).trim().length === 0) {
    return next;
  }

  if (canMerge) {
    next[next.length - 1] = {
      ...last,
      text: `${last.text || ''}${text}`,
      meta: meta || last.meta,
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
      finished: false,
      startTs: ts || Date.now(),
      endTs: ts || Date.now(),
    },
  ];
};

const extractTurnFinishedOutput = (payload = {}) => {
  const statusRaw = typeof payload.status === 'string' ? payload.status.toLowerCase() : 'success';
  const status = TERMINAL_STATUSES.has(statusRaw) ? statusRaw : 'success';

  const result = payload.final_output;
  let content = '';
  let options = null;
  let messageType = 'text';
  let interaction = null;

  if (typeof result === 'string') {
    content = result;
  } else if (result && typeof result === 'object') {
    content = result.text || result.formatted_result || '';
    options = Array.isArray(result.options) ? result.options : null;
    messageType = typeof result.type === 'string' ? result.type : 'text';
    interaction = result.interaction && typeof result.interaction === 'object' ? result.interaction : null;
  }

  return {
    status,
    content,
    options,
    messageType,
    interaction,
    error: payload.error || null,
    cancelReason: payload.cancel_reason || null,
  };
};

const getTrailingContentText = (segments = []) => {
  if (!Array.isArray(segments) || segments.length === 0) return '';

  const trailing = [];
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    const segment = segments[i];
    if (segment?.type === 'content') {
      trailing.unshift(segment?.text || '');
      continue;
    }
    if (trailing.length > 0) break;
  }

  return trailing.join('').trim();
};

const shouldAppendFinalContent = (segments = [], finalContent = '') => {
  const normalized = String(finalContent || '').trim();
  if (!normalized) return false;

  // Compare only the trailing content block of the current turn.
  // Earlier content blocks are intermediate outputs and should not trigger
  // a duplicate append of the final answer.
  const trailingContent = getTrailingContentText(segments);
  if (!trailingContent) return true;

  return trailingContent !== normalized && !trailingContent.endsWith(normalized);
};

const extractBusinessDataSearchProgressEvent = (payload = {}, envelope = {}) => {
  const toolName = normalizeToolName(payload?.tool_name);
  if (!DATA_SEARCH_TOOL_NAMES.has(toolName)) return null;

  const rawEvent = payload?.progress?.business_data_search_event;
  if (!rawEvent || typeof rawEvent !== 'object') return null;

  const eventType = typeof rawEvent?.type === 'string'
    ? rawEvent.type.trim().toLowerCase()
    : '';
  if (!BUSINESS_DATA_SEARCH_EVENT_TYPES.has(eventType)) return null;

  const content = rawEvent?.content == null ? '' : String(rawEvent.content);
  const rawTs = Number(envelope?.ts);
  const rawSeq = Number(envelope?.seq);

  return {
    type: eventType,
    content,
    ts: Number.isFinite(rawTs) ? rawTs : Date.now(),
    seq: Number.isFinite(rawSeq) ? rawSeq : null,
  };
};

const appendBusinessDataSearchEventToMeta = (meta = {}, dbEvent = null) => {
  if (!dbEvent) return meta || {};
  const existing = Array.isArray(meta?.db_events) ? meta.db_events : [];
  return {
    ...(meta || {}),
    db_events: [...existing, dbEvent],
  };
};

const extractWebSearchProgressEvent = (payload = {}, envelope = {}) => {
  const toolName = normalizeToolName(payload?.tool_name);
  if (toolName !== 'web_search') return null;

  const rawEvent = payload?.progress?.web_search_event;
  if (!rawEvent || typeof rawEvent !== 'object') return null;

  const eventType = typeof rawEvent?.type === 'string'
    ? rawEvent.type.trim().toLowerCase()
    : '';
  if (!WEB_SEARCH_EVENT_TYPES.has(eventType)) return null;

  const content = rawEvent?.content == null ? '' : String(rawEvent.content);
  const rawTs = Number(envelope?.ts);
  const rawSeq = Number(envelope?.seq);

  return {
    type: eventType,
    content,
    ts: Number.isFinite(rawTs) ? rawTs : Date.now(),
    seq: Number.isFinite(rawSeq) ? rawSeq : null,
  };
};

const appendWebSearchEventToMeta = (meta = {}, webEvent = null) => {
  if (!webEvent) return meta || {};
  const existing = Array.isArray(meta?.web_search_events) ? meta.web_search_events : [];
  return {
    ...(meta || {}),
    web_search_events: [...existing, webEvent],
  };
};

const normalizeNonNegativeNumber = (value) => {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return null;
  return numeric;
};

const upsertIterationMetric = (metrics = [], payload = {}, ts = Date.now()) => {
  const iterationValue = typeof payload?.iteration === 'number'
    ? payload.iteration
    : Number(payload?.iteration);
  const iteration = Number.isFinite(iterationValue) && iterationValue > 0
    ? Math.floor(iterationValue)
    : null;
  if (iteration == null) return Array.isArray(metrics) ? metrics : [];

  const nextMetrics = Array.isArray(metrics) ? metrics.slice() : [];
  const nextMetric = {
    iteration,
    status: typeof payload?.status === 'string' ? payload.status : '',
    llmElapsedMs: normalizeNonNegativeNumber(payload?.llm_elapsed_ms),
    llmStreamElapsedMs: normalizeNonNegativeNumber(payload?.llm_stream_elapsed_ms),
    llmWallElapsedMs: normalizeNonNegativeNumber(payload?.llm_wall_elapsed_ms),
    toolsElapsedMs: normalizeNonNegativeNumber(payload?.tools_elapsed_ms),
    toolsWindowElapsedMs: normalizeNonNegativeNumber(payload?.tools_window_elapsed_ms),
    toolsWallElapsedMs: normalizeNonNegativeNumber(payload?.tools_wall_elapsed_ms),
    toolsExecutionElapsedMs: normalizeNonNegativeNumber(payload?.tools_execution_elapsed_ms),
    toolsOverlapLlmMs: normalizeNonNegativeNumber(payload?.tools_overlap_llm_ms),
    toolsNetWaitMs: normalizeNonNegativeNumber(payload?.tools_net_wait_ms),
    promptBuildMs: normalizeNonNegativeNumber(payload?.prompt_build_ms),
    totalElapsedMs: normalizeNonNegativeNumber(payload?.total_elapsed_ms),
    toolCallsCount: normalizeNonNegativeNumber(payload?.tool_calls_count),
    toolNames: Array.isArray(payload?.tool_names) ? payload.tool_names.map((item) => String(item)) : [],
    ts: normalizeNonNegativeNumber(ts) ?? Date.now(),
  };

  const existingIdx = nextMetrics.findIndex((item) => Number(item?.iteration) === iteration);
  if (existingIdx !== -1) {
    nextMetrics[existingIdx] = {
      ...nextMetrics[existingIdx],
      ...nextMetric,
    };
  } else {
    nextMetrics.push(nextMetric);
    nextMetrics.sort((a, b) => Number(a?.iteration || 0) - Number(b?.iteration || 0));
  }

  return nextMetrics;
};

const getChildGroups = (child) => (
  Array.isArray(child?.childGroups) ? child.childGroups : []
);

function cloneChild(child) {
  return {
    ...child,
    events: Array.isArray(child?.events) ? child.events.slice() : [],
    childGroups: getChildGroups(child).map((group) => cloneSubagentGroup(group)),
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
  for (let childIdx = childrenEntries.length - 1; childIdx >= 0; childIdx -= 1) {
    const [subagentId, child] = childrenEntries[childIdx];
    const childGroups = getChildGroups(child);
    for (let groupIdx = childGroups.length - 1; groupIdx >= 0; groupIdx -= 1) {
      const result = updateGroupRecursive(childGroups[groupIdx], matcher, updater);
      if (!result.found) continue;

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
  if (!Array.isArray(segments) || segments.length === 0) return null;

  for (let idx = segments.length - 1; idx >= 0; idx -= 1) {
    const segment = segments[idx];
    if (segment?.type !== 'subagent_group') continue;
    const result = updateGroupRecursive(segment, matcher, updater);
    if (!result.found) continue;

    const nextSegments = segments.slice();
    nextSegments[idx] = result.group;
    return nextSegments;
  }

  return null;
};

const updateGroupByGroupId = (segments, groupId, updater) => {
  if (!groupId) return null;
  return updateGroupInSegments(
    segments,
    (group) => group?.groupId === groupId,
    updater,
  );
};

const updateGroupBySubagentId = (segments, subagentId, updater) => {
  if (!subagentId) return null;
  return updateGroupInSegments(
    segments,
    (group) => Boolean(group?.children && Object.prototype.hasOwnProperty.call(group.children, subagentId)),
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
    updateGroupByGroupId(segments, meta?.groupId, updateChildInGroup)
    || updateGroupBySubagentId(segments, subagentId, updateChildInGroup)
  );
};

const appendChildGroupToSubagent = (segments, parentSubagentId, childGroup) => (
  updateSubagentChildInSegments(
    segments,
    parentSubagentId,
    {},
    (child) => {
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
    },
  )
);

const normalizeTaskIndex = (value, fallback = 0) => {
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
};

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
  const task = typeof meta?.task === 'string' && meta.task.trim().length > 0
    ? meta.task
    : (pending?.task || '');
  const taskIndex = normalizeTaskIndex(meta?.taskIndex, normalizeTaskIndex(pending?.taskIndex, 0));
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

const appendChildEvent = (child, event) => {
  const next = cloneChild(child);
  const last = next.events[next.events.length - 1];

  if (event.kind === 'token') {
    if (last && last.kind === 'token' && last.channel === event.channel) {
      next.events[next.events.length - 1] = {
        ...last,
        text: `${last.text || ''}${event.text || ''}`,
        ts: event.ts || last.ts,
      };
    } else {
      next.events.push(event);
    }
    return next;
  }

  if (event.kind === 'tool') {
    const tcid = event.toolCallId || '';
    let idx = -1;
    if (tcid) {
      for (let i = next.events.length - 1; i >= 0; i -= 1) {
        if (next.events[i]?.kind === 'tool' && next.events[i]?.toolCallId === tcid) {
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
        error: event.error || '',
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
        progress: event.progressEntry
          ? [...(prev.progress || []), event.progressEntry]
          : prev.progress,
        output: event.output || prev.output,
        error: event.error || prev.error,
        endTs: event.finishedStatus ? event.ts : prev.endTs,
      };
    }
    return next;
  }

  next.events.push(event);
  return next;
};

const buildToolEventFromPayload = (type, payload, ts) => {
  const toolCallId = typeof payload?.tool_call_id === 'string' ? payload.tool_call_id : '';
  const toolName = typeof payload?.tool_name === 'string' ? payload.tool_name : '';
  if (type === 'tool.started') {
    return {
      kind: 'tool',
      toolCallId,
      toolName,
      input: payload?.input || null,
      ts,
    };
  }
  if (type === 'tool.progress') {
    const ontologyEvent = payload?.progress?.business_data_search_event || null;
    const webSearchEvent = payload?.progress?.web_search_event || null;
    const text = payload?.message
      || (ontologyEvent?.content ? String(ontologyEvent.content) : '')
      || (webSearchEvent?.content ? String(webSearchEvent.content) : '');
    return {
      kind: 'tool',
      toolCallId,
      toolName,
      progressEntry: {
        text,
        ontology: ontologyEvent,
        web: webSearchEvent,
        ts,
      },
      ts,
    };
  }
  if (type === 'tool.finished') {
    return {
      kind: 'tool',
      toolCallId,
      toolName,
      finishedStatus: payload?.status === 'error' ? 'error' : 'completed',
      output: payload?.output_preview || '',
      error: payload?.error || '',
      ts,
    };
  }
  return null;
};

const routeEventToSubagentChild = (segments, subagentId, event, meta = {}) => {
  return updateSubagentChildInSegments(
    segments,
    subagentId,
    meta,
    (child) => appendChildEvent(child, event),
  );
};

export const applyAgentV2Event = (current, envelope) => {
  if (!isAgentV2Envelope(envelope)) return current;

  const next = {
    ...current,
    streamSegments: Array.isArray(current.streamSegments)
      ? current.streamSegments.slice()
      : [],
  };

  const type = envelope.type;
  const payload = envelope.payload || {};
  const sourceNode = envelope?.source?.node || 'agent';
  const subagentIdInPayload = typeof payload?.subagent_id === 'string' && payload.subagent_id
    ? payload.subagent_id
    : null;

  if (subagentIdInPayload && (type === 'tool.started' || type === 'tool.progress' || type === 'tool.finished')) {
    const event = buildToolEventFromPayload(type, payload, envelope.ts || Date.now());
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
          ts: envelope.ts || Date.now(),
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
      ts: envelope.ts || Date.now(),
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
        ts: envelope.ts || Date.now(),
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
    const parentSubagentId = typeof payload.parent_subagent_id === 'string' && payload.parent_subagent_id
      ? payload.parent_subagent_id
      : null;
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
      startTs: envelope.ts || Date.now(),
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
    const status = payload.status === 'error' || (payload.error_count || 0) > 0
      ? 'error'
      : 'completed';
    const updatedSegments = updateGroupByGroupId(
      next.streamSegments,
      payload.group_id,
      (group) => ({
        ...cloneSubagentGroup(group),
        status,
        finished: true,
        endTs: envelope.ts || Date.now(),
        meta: { ...(group?.meta || {}), ...payload },
      }),
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      if (status === 'error') next.error = true;
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
      (group) => {
        const pending = (group.pendingTasks && group.pendingTasks[taskId]) || {};
        const taskText = payload.task || pending.task || '';
        const { nextGroup, child } = ensureSubagentChild(group, subagentIdInPayload, {
          groupId,
          taskId,
          task: taskText,
          taskIndex: payload.task_index,
          ts: envelope.ts || Date.now(),
        });
        nextGroup.children[subagentIdInPayload] = {
          ...cloneChild(child),
          subagentId: subagentIdInPayload,
          taskId,
          taskIndex: normalizeTaskIndex(payload.task_index, child?.taskIndex ?? pending?.taskIndex ?? 0),
          task: taskText || child?.task || '',
          status: payload.status || 'running',
          startTs: child?.startTs || envelope.ts || Date.now(),
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
      meta: payload,
      ts: envelope.ts || Date.now(),
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
        ts: envelope.ts || Date.now(),
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
        ts: envelope.ts || Date.now(),
      },
      (child) => ({
        ...cloneChild(child),
        status: payload.status || child.status,
      }),
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      return next;
    }
  }

  if (type === 'subagent.finished' && subagentIdInPayload) {
    const finalStatus = normalizeToolStatus(payload.status) === 'error'
      ? 'error'
      : (payload.status || 'completed');
    const updatedSegments = updateSubagentChildInSegments(
      next.streamSegments,
      subagentIdInPayload,
      {
        groupId: payload.group_id,
        taskId: payload.task_id,
        task: payload.task,
        taskIndex: payload.task_index,
        ts: envelope.ts || Date.now(),
      },
      (child) => ({
        ...cloneChild(child),
        status: finalStatus,
        endTs: envelope.ts || Date.now(),
        result: payload.result || null,
        error: payload.error || null,
      }),
    );
    if (updatedSegments) {
      next.streamSegments = updatedSegments;
      if (finalStatus === 'error') next.error = true;
      return next;
    }
  }

  if (type === 'turn.started') {
    next.status = 'running';
    next.isLoading = true;
    next.meta = payload;
    next.startTs = envelope.ts || Date.now();
    next.currentSegmentType = null;
    return next;
  }

  if (type === 'token.delta') {
    const channel = payload.channel === 'reasoning' ? 'reasoning' : 'content';
    const text = payload.text == null ? '' : String(payload.text);
    next.streamSegments = appendInlineTextSegment(next.streamSegments, {
      type: channel,
      state: 'streaming',
      text,
      node: sourceNode,
      meta: payload,
      ts: envelope.ts || Date.now(),
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
      envelope.ts || Date.now(),
    );
    next.currentSegmentType = null;
    return next;
  }

  if (type === 'reasoning.summary') {
    const summary = typeof payload.summary === 'string' ? payload.summary.trim() : '';
    if (!summary) return next;
    const blockId = typeof payload.reasoning_block_id === 'string'
      ? payload.reasoning_block_id.trim()
      : '';

    if (blockId) {
      const idxByBlockId = findLastSegmentIndex(
        next.streamSegments,
        (segment) => (
          segment?.type === 'reasoning'
          && String(segment?.meta?.reasoning_block_id || '').trim() === blockId
        )
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

    const sourceNodeName = typeof sourceNode === 'string' ? sourceNode.trim() : '';
    const matchReasoningSegment = (segment) => (
      segment?.type === 'reasoning'
      && (!sourceNodeName || (segment?.node || '') === sourceNodeName)
    );
    const targetOpenIdx = findLastSegmentIndex(
      next.streamSegments,
      (segment) => matchReasoningSegment(segment) && !segment?.finished
    );
    const targetAnyIdx = targetOpenIdx !== -1
      ? targetOpenIdx
      : findLastSegmentIndex(next.streamSegments, matchReasoningSegment);
    if (targetAnyIdx === -1) return next;

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

  if (type === 'tool.started') {
    const locator = resolveToolLocator(payload);
    const targetIdx = hasToolLocator(locator)
      ? findLastSegmentIndex(
        next.streamSegments,
        (segment) => segmentMatchesToolLocator(segment, locator)
      )
      : -1;

    if (targetIdx !== -1) {
      const target = next.streamSegments[targetIdx];
      next.streamSegments[targetIdx] = {
        ...target,
        state: 'running',
        text: payload.message || target.text || `调用工具: ${payload.tool_name || 'unknown_tool'}`,
        node: sourceNode || target.node,
        meta: mergeToolMeta(target.meta, payload),
        finished: false,
      };
    } else {
      next.streamSegments = closeOpenNonToolSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: 'running',
        text: payload.message || `调用工具: ${payload.tool_name || 'unknown_tool'}`,
        node: sourceNode,
        meta: payload,
        finished: false,
        toolStatus: null,
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
    const dbSearchEvent = extractBusinessDataSearchProgressEvent(payload, envelope);
    const webSearchEvent = extractWebSearchProgressEvent(payload, envelope);
    const targetOpenIdx = findLastSegmentIndex(
      next.streamSegments,
      (segment) => (
        segmentMatchesToolLocator(segment, locator)
        && !segment?.finished
      )
    );
    const targetAnyIdx = findLastSegmentIndex(
      next.streamSegments,
      (segment) => segmentMatchesToolLocator(segment, locator)
    );

    if (targetOpenIdx !== -1 || targetAnyIdx !== -1) {
      const targetIdx = targetOpenIdx !== -1 ? targetOpenIdx : targetAnyIdx;
      const target = next.streamSegments[targetIdx];
      const mergedMeta = mergeToolMeta(target.meta, payload);
      next.streamSegments[targetIdx] = {
        ...target,
        state: targetOpenIdx !== -1 ? 'running' : (target.state || 'running'),
        text: text
          || target.text
          || (dbSearchEvent ? `business_data_search ${dbSearchEvent.type}` : '')
          || (webSearchEvent ? `web_search ${webSearchEvent.type}` : ''),
        node: sourceNode || target.node,
        meta: appendWebSearchEventToMeta(
          appendBusinessDataSearchEventToMeta(mergedMeta, dbSearchEvent),
          webSearchEvent,
        ),
        finished: targetOpenIdx !== -1 ? false : Boolean(target.finished),
      };
    } else if (dbSearchEvent || webSearchEvent || hasToolLocator(locator)) {
      next.streamSegments = closeOpenNonToolSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: 'running',
        text: text
          || (dbSearchEvent ? `business_data_search ${dbSearchEvent.type}` : '')
          || (webSearchEvent ? `web_search ${webSearchEvent.type}` : '')
          || `调用工具: ${payload.tool_name || 'unknown_tool'}`,
        node: sourceNode,
        meta: appendWebSearchEventToMeta(
          appendBusinessDataSearchEventToMeta(mergeToolMeta({}, payload), dbSearchEvent),
          webSearchEvent,
        ),
        finished: false,
        toolStatus: null,
      });
    } else {
      next.streamSegments = appendInlineTextSegment(next.streamSegments, {
        type: 'tool',
        state: 'running',
        text,
        node: sourceNode,
        meta: payload,
        mergeKey: 'tool_call_id',
        ts: envelope.ts || Date.now(),
      });
    }
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'tool';
    return next;
  }

  if (type === 'tool.finished') {
    const locator = resolveToolLocator(payload);
    const hasLocator = hasToolLocator(locator);
    const toolStatus = normalizeToolStatus(payload.status);
    const targetIdx = findLastSegmentIndex(
      next.streamSegments,
      (segment) => (
        hasLocator
          ? segmentMatchesToolLocator(segment, locator)
          : (segment?.type === 'tool' && !segment?.finished)
      )
    );

    if (targetIdx !== -1) {
      const target = next.streamSegments[targetIdx];
      next.streamSegments[targetIdx] = {
        ...target,
        meta: mergeToolMeta(target.meta, payload),
        state: toolStatus || 'completed',
        text: target.text || payload.output_preview || '',
        finished: true,
        toolStatus,
      };
    } else {
      next.streamSegments = closeOpenSegment(next.streamSegments);
      next.streamSegments.push({
        type: 'tool',
        state: toolStatus || 'completed',
        text: payload.output_preview || payload.error || `工具完成: ${payload.tool_name || 'unknown_tool'}`,
        node: sourceNode,
        meta: payload,
        finished: true,
        toolStatus,
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
      ts: envelope.ts || Date.now(),
    });
    next.status = 'running';
    next.isLoading = true;
    next.currentSegmentType = 'subagent';
    return next;
  }

  if (type === 'subagent.started' || type === 'subagent.progress' || type === 'subagent.finished') {
    const text =
      payload.text
      || payload.step
      || (type === 'subagent.started'
        ? `子智能体启动: ${payload.agent_type || 'subagent'}`
        : (type === 'subagent.finished'
          ? `子智能体结束: ${payload.agent_type || 'subagent'}`
          : `子智能体状态更新: ${payload.agent_type || 'subagent'}`));

    next.streamSegments = closeOpenSegment(next.streamSegments);
    next.streamSegments.push({
      type: 'subagent',
      state: payload.status || (type === 'subagent.finished' ? 'completed' : 'running'),
      text,
      node: payload.agent_type || 'subagent',
      meta: payload,
      finished: type !== 'subagent.progress',
    });

    next.status = 'running';
    next.isLoading = true;
    if (type === 'subagent.finished' && normalizeToolStatus(payload.status) === 'error') {
      next.error = true;
    }
    next.currentSegmentType = type === 'subagent.progress' ? 'subagent' : null;
    return next;
  }

  if (type === 'turn.finished') {
    const terminal = extractTurnFinishedOutput(payload);
    next.streamSegments = closeUnfinishedSegmentsForTerminalTurn(
      next.streamSegments,
      terminal.status,
      envelope.ts || Date.now(),
    );

    if (shouldAppendFinalContent(next.streamSegments, terminal.content || '')) {
      next.streamSegments.push({
        type: 'content',
        state: 'final',
        text: terminal.content,
        node: 'final',
        meta: {},
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
    next.endTs = envelope.ts || Date.now();
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
      next.meta = {
        ...(next.meta || {}),
        error: terminal.error,
      };
    }
    if (terminal.cancelReason) {
      next.meta = {
        ...(next.meta || {}),
        cancel_reason: terminal.cancelReason,
      };
    }

    return next;
  }

  return next;
};
