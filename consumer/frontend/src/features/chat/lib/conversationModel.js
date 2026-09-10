import { publicErrorMessage } from './streamHelpers.js';
import {
  getWorkflowErrorPresentation,
  normalizeWorkflowRecovery,
} from './workflowErrorPresentation.js';

const TERMINAL_STATUSES = new Set([
  'finished',
  'error',
  'cancelled',
  'awaiting',
]);
const BUSINESS_DATA_TOOLS = new Set(['business_data_search']);

const stringValue = (value) => (value == null ? '' : String(value));

const plainObject = (value) =>
  value != null && typeof value === 'object' && !Array.isArray(value);

const parseOutput = (value) => {
  if (plainObject(value) || Array.isArray(value)) {
    return value;
  }
  if (typeof value !== 'string' || !value.trim()) {
    return null;
  }
  try {
    const parsed = JSON.parse(value);
    return plainObject(parsed) || Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
};

const numberValue = (value) => {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
};

const timestampValue = (value) => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
};

const elapsedValue = (startedAt, endedAt) =>
  startedAt != null && endedAt != null && endedAt >= startedAt ?
    endedAt - startedAt :
    null;

const hashText = (value) => {
  let hash = 2166136261;
  const text = stringValue(value);
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
};

const uniqueKey = (base, occurrences) => {
  const count = occurrences.get(base) || 0;
  occurrences.set(base, count + 1);
  return count === 0 ? base : `${base}:${count + 1}`;
};

export const normalizeConversationStatus = (status, isLoading = false) => {
  const normalized = stringValue(status).trim().toLowerCase();
  if (normalized === 'awaiting' || normalized === 'awaiting_input') {
    return 'awaiting';
  }
  if (
    normalized === 'aborted' ||
    normalized === 'cancelled' ||
    normalized === 'canceled'
  ) {
    return 'cancelled';
  }
  if (normalized === 'error' || normalized === 'failed') {
    return 'error';
  }
  const isFinished =
    normalized === 'success' ||
    normalized === 'completed' ||
    normalized === 'finished' ||
    normalized === 'done' ||
    normalized === 'final';
  if (isFinished) {
    return 'finished';
  }
  if (normalized === 'queued') {
    return 'queued';
  }
  if (
    isLoading ||
    ['running', 'streaming', 'reconnecting', 'finalizing'].includes(normalized)
  ) {
    return 'running';
  }
  return normalized || 'idle';
};

const normalizeToolStatus = (segment) => {
  const raw = segment?.toolStatus || segment?.meta?.status || segment?.state;
  const normalized = stringValue(raw).trim().toLowerCase();
  if (normalized === 'error' || normalized === 'failed') {
    return 'error';
  }
  if (['cancelled', 'canceled', 'aborted', 'abort'].includes(normalized)) {
    return 'cancelled';
  }
  if (
    segment?.finished ||
    ['success', 'completed', 'finished', 'done', 'final'].includes(normalized)
  ) {
    return 'finished';
  }
  return 'running';
};

export const conversationKindForTool = (toolName) => {
  const normalized = stringValue(toolName).trim().toLowerCase();
  if (BUSINESS_DATA_TOOLS.has(normalized)) {
    return 'business_data';
  }
  if (normalized === 'web_search') {
    return 'web_search';
  }
  if (normalized === 'solve' || normalized.startsWith('solve_')) {
    return 'solver';
  }
  return 'tool';
};

export const conversationKindForSegment = (segment) => {
  const type = stringValue(segment?.type).trim().toLowerCase();
  if (type === 'tool') {
    return conversationKindForTool(
      segment?.meta?.tool_name || segment?.meta?.name,
    );
  }
  if (type === 'subagent' || type === 'subagent_group') {
    return 'subagent';
  }
  if (type === 'reasoning') {
    return 'reasoning';
  }
  if (type === 'content') {
    return 'content';
  }
  if (type === 'artifacts') {
    return 'artifacts';
  }
  return type || 'unknown';
};

const safeWebUrl = (url) => {
  const value = stringValue(url).trim();
  if (!value) {
    return '';
  }
  try {
    const parsed = new URL(value);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
      return '';
    }
    return parsed.href;
  } catch {
    return '';
  }
};

const sourceDomain = (url) => {
  const safeUrl = safeWebUrl(url);
  if (!safeUrl) {
    return '';
  }
  return new URL(safeUrl).hostname.toLowerCase();
};

const normalizeWebSources = (parsedOutput) => {
  let rawSources = [];
  if (Array.isArray(parsedOutput?.results)) {
    rawSources = parsedOutput.results;
  } else if (Array.isArray(parsedOutput?.sources)) {
    rawSources = parsedOutput.sources;
  }
  return rawSources.filter(plainObject).map((source, index) => {
    const url = safeWebUrl(source.url || source.link);
    const domain = stringValue(source.domain).trim() || sourceDomain(url);
    return {
      key: `source:${stringValue(source.id).trim() || hashText(`${url}|${source.title || ''}|${index}`)}`,
      rank: Number.isFinite(Number(source.rank)) ?
        Number(source.rank) :
        index + 1,
      title: stringValue(
        source.title || domain || url || `source ${index + 1}`,
      ).trim(),
      url,
      domain,
      snippet: stringValue(
        source.snippet || source.content || source.description,
      ).trim(),
      publishedAt: stringValue(
        source.published_at || source.published_date || source.publishedAt,
      ).trim(),
    };
  });
};

const resultCountFromProgress = (progress) => {
  for (let index = progress.length - 1; index >= 0; index -= 1) {
    const countText = stringValue(progress[index]?.text).match(
      /(?:返回|共)\s*(?<count>\d+)\s*条/u,
    )?.groups.count;
    if (countText) {
      return Number(countText);
    }
  }
  return null;
};

const progressIdentity = (event, index) =>
  stringValue(event?.seq || event?.event_id || event?.ts).trim() ||
  `${stringValue(event?.type)}:${hashText(event?.content)}:${index}`;

const normalizeProgress = (events, kind, blockKey) => {
  const occurrences = new Map();
  return (Array.isArray(events) ? events : [])
    .map((event, index) => {
      const startedAt = timestampValue(event?.ts);
      const identity = progressIdentity(event, index);
      return {
        key: uniqueKey(`${blockKey}:progress:${identity}`, occurrences),
        kind,
        type: stringValue(event?.type).trim() || 'progress',
        text: stringValue(event?.content),
        sequence: Number.isFinite(Number(event?.seq)) ?
          Number(event.seq) :
          null,
        startedAt,
        endedAt: startedAt,
        terminal: ['final', 'completed', 'error'].includes(
          stringValue(event?.type).toLowerCase(),
        ),
      };
    })
    .filter((item) => item.text.trim());
};

const progressErrorText = (progress) => {
  for (let index = progress.length - 1; index >= 0; index -= 1) {
    if (stringValue(progress[index]?.type).trim().toLowerCase() === 'error') {
      return stringValue(progress[index]?.text).trim();
    }
  }
  return '';
};

const childEventKind = (event) => {
  if (event?.kind === 'tool') {
    return conversationKindForTool(event?.toolName);
  }
  if (event?.kind === 'token' && event?.channel === 'reasoning') {
    return 'reasoning';
  }
  if (event?.kind === 'token') {
    return 'content';
  }
  return stringValue(event?.kind).trim() || 'status';
};

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
const normalizeSubagentGroup = (group, parentKey) => {
  const groupId = stringValue(group?.groupId || group?.meta?.group_id).trim();
  const groupKey = `${parentKey}:group:${groupId || hashText(JSON.stringify(group?.meta || {}))}`;
  const orderedIds = Array.isArray(group?.taskOrder) ?
    group.taskOrder :
    Object.keys(group?.children || {});
  const children = orderedIds
    // eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
    .map((subagentId, index) => {
      const child = group?.children?.[subagentId];
      if (!child) {
        return null;
      }
      const childKey = `${groupKey}:agent:${stringValue(subagentId) || index}`;
      const eventOccurrences = new Map();
      const events = (Array.isArray(child.events) ? child.events : []).map(
        // eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
        (event, eventIndex) => {
          const kind = childEventKind(event);
          const identity =
            stringValue(
              event?.toolCallId ||
                event?.eventId ||
                event?.sourceOrder ||
                event?.seq,
            ).trim() ||
            `${kind}:${timestampValue(event?.startTs ?? event?.ts) ?? 'event'}:${eventIndex}`;
          const startedAt = timestampValue(event?.startTs ?? event?.ts);
          const endedAt = timestampValue(
            event?.endTs ?? (event?.finished ? event?.ts : null),
          );
          const eventKey = uniqueKey(
            `${childKey}:event:${identity}`,
            eventOccurrences,
          );
          let progress = [];
          if (kind === 'business_data') {
            progress = normalizeProgress(
              (event?.progress || [])
                .map((item) =>
                  item?.ontology ?
                    { ...item.ontology, ts: item.ontology.ts ?? item.ts } :
                    null,
                )
                .filter(Boolean),
              kind,
              eventKey,
            );
          } else if (kind === 'web_search') {
            progress = normalizeProgress(
              (event?.progress || [])
                .map((item) =>
                  item?.web ?
                    { ...item.web, ts: item.web.ts ?? item.ts } :
                    null,
                )
                .filter(Boolean),
              kind,
              eventKey,
            );
          }
          const progressError = progressErrorText(progress);
          let status = 'running';
          if (progressError) {
            status = 'error';
          } else if (event?.finished) {
            status = normalizeConversationStatus(event?.status || 'finished');
          }
          return {
            key: eventKey,
            kind,
            status,
            terminal: Boolean(event?.finished),
            text:
              event?.kind === 'tool' ?
                stringValue(event?.text || event?.message) :
                stringValue(event?.text || publicErrorMessage(event?.error)),
            toolName: stringValue(event?.toolName),
            toolCallId: stringValue(event?.toolCallId),
            traceId: stringValue(event?.traceId || event?.trace_id),
            spanId: stringValue(event?.spanId || event?.span_id),
            parentSpanId: stringValue(
              event?.parentSpanId || event?.parent_span_id,
            ),
            input: event?.input ?? null,
            output: stringValue(event?.output),
            outputRef: stringValue(event?.outputRef ?? event?.output_ref),
            outputSize: numberValue(event?.outputSize ?? event?.output_size),
            outputTruncated: Boolean(
              event?.outputTruncated ?? event?.output_truncated,
            ),
            outputProjection: plainObject(
              event?.outputProjection ?? event?.output_projection,
            ) ?
              (event?.outputProjection ?? event?.output_projection) :
              null,
            parsedOutput: plainObject(
              event?.outputProjection ?? event?.output_projection,
            ) ?
              (event?.outputProjection ?? event?.output_projection) :
              parseOutput(event?.output),
            error: publicErrorMessage(event?.error) || progressError,
            progress,
            startedAt,
            endedAt,
            durationMs:
              numberValue(event?.elapsedMs ?? event?.elapsed_ms) ??
              elapsedValue(startedAt, endedAt),
            sourceOrder: numberValue(event?.sourceOrder),
            order: eventIndex,
          };
        },
      );
      const groups = (
        Array.isArray(child?.childGroups) ? child.childGroups : []
      ).map((nested) => normalizeSubagentGroup(nested, childKey));
      const items = [
        ...events.map((item, sourceIndex) => ({
          item,
          sourceIndex,
          sourceType: 0,
        })),
        ...groups.map((item, sourceIndex) => ({
          item,
          sourceIndex,
          sourceType: 1,
        })),
      ]
        .sort((left, right) => {
          const leftSourceOrder = numberValue(left.item.sourceOrder);
          const rightSourceOrder = numberValue(right.item.sourceOrder);
          if (
            leftSourceOrder != null &&
            rightSourceOrder != null &&
            leftSourceOrder !== rightSourceOrder
          ) {
            return leftSourceOrder - rightSourceOrder;
          }
          const leftTs = left.item.startedAt ?? Number.POSITIVE_INFINITY;
          const rightTs = right.item.startedAt ?? Number.POSITIVE_INFINITY;
          return (
            leftTs - rightTs ||
            left.sourceType - right.sourceType ||
            left.sourceIndex - right.sourceIndex
          );
        })
        .map(({ item }) => item);
      const childStartedAt = timestampValue(child?.startTs);
      const childEndedAt = timestampValue(child?.endTs);
      return {
        key: childKey,
        kind: 'subagent',
        subtype: 'task',
        subagentId: stringValue(subagentId),
        taskId: stringValue(child?.taskId),
        task: stringValue(child?.task),
        taskIndex: Number.isFinite(Number(child?.taskIndex)) ?
          Number(child.taskIndex) :
          index,
        status: normalizeConversationStatus(child?.status, !child?.endTs),
        terminal:
          Boolean(child?.endTs) ||
          ['completed', 'error', 'cancelled'].includes(
            stringValue(child?.status),
          ),
        result: child?.result ?? null,
        error: publicErrorMessage(child?.error),
        traceId: stringValue(child?.traceId),
        spanId: stringValue(child?.spanId),
        parentSpanId: stringValue(child?.parentSpanId),
        startedAt: childStartedAt,
        endedAt: childEndedAt,
        durationMs: elapsedValue(childStartedAt, childEndedAt),
        events,
        groups,
        items,
      };
    })
    .filter(Boolean);
  const startedAt = timestampValue(group?.startTs);
  const endedAt = timestampValue(group?.endTs);
  return {
    key: groupKey,
    kind: 'subagent',
    subtype: 'group',
    groupId,
    traceId: stringValue(group?.traceId || group?.meta?.trace_id),
    spanId: stringValue(group?.spanId || group?.meta?.span_id),
    parentSpanId: stringValue(
      group?.parentSpanId || group?.meta?.parent_span_id,
    ),
    status: normalizeConversationStatus(group?.status, !group?.finished),
    terminal: Boolean(group?.finished),
    startedAt,
    endedAt,
    durationMs: elapsedValue(startedAt, endedAt),
    sourceOrder: numberValue(group?.sourceOrder),
    children,
  };
};

const segmentIdentity = (segment, kind, index) => {
  const meta = segment?.meta || {};
  return (
    stringValue(
      segment?.spanId ||
        meta.span_id ||
        segment?.spanKey ||
        meta.tool_call_id ||
        segment?.groupId ||
        meta.subagent_id ||
        meta.reasoning_block_id,
    ).trim() ||
    `${kind}:${stringValue(segment?.node).trim() || 'segment'}:${index}`
  );
};

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
const normalizeSegment = (segment, index, messageKey, occurrences) => {
  const kind = conversationKindForSegment(segment);
  const key = uniqueKey(
    `${messageKey}:block:${segmentIdentity(segment, kind, index)}`,
    occurrences,
  );
  const startedAt = timestampValue(segment?.startTs ?? segment?.ts);
  const endedAt = timestampValue(segment?.endTs);
  const meta = segment?.meta || {};
  const toolName = stringValue(meta.tool_name || meta.name);
  let progress = [];
  if (kind === 'business_data') {
    progress = normalizeProgress(meta.db_events, kind, key);
  } else if (kind === 'web_search') {
    progress = normalizeProgress(meta.web_search_events, kind, key);
  } else if (kind === 'solver') {
    progress = (Array.isArray(meta.solver_events) ? meta.solver_events : []).map(
      (event, progressIndex) => ({
        key: `${key}:solver-progress:${event?.job_id || 'job'}:${event?.event_seq ?? progressIndex}`,
        type: stringValue(event?.type),
        phase: stringValue(event?.phase),
        text: stringValue(event?.message),
        eventSeq: numberValue(event?.event_seq),
        status: stringValue(event?.status),
      }),
    );
  }
  const progressError = progressErrorText(progress);
  const toolKinds = ['tool', 'business_data', 'web_search', 'solver'];
  let status = toolKinds.includes(kind) ?
    normalizeToolStatus(segment) :
    normalizeConversationStatus(segment?.state, !segment?.finished);
  if (progressError) {
    status = 'error';
  }
  if (
    segment?.finished &&
    !['error', 'cancelled', 'awaiting'].includes(status)
  ) {
    status = 'finished';
  }
  const block = {
    key,
    kind,
    segmentType: stringValue(segment?.type) || 'unknown',
    order: index,
    status,
    terminal: Boolean(segment?.finished),
    text: stringValue(segment?.text),
    summary: stringValue(meta.reasoning_summary),
    node: stringValue(segment?.node),
    spanKey: stringValue(segment?.spanKey),
    parentKey: stringValue(segment?.parentKey),
    traceId: stringValue(segment?.traceId || meta.trace_id),
    spanId: stringValue(segment?.spanId || meta.span_id),
    parentSpanId: stringValue(segment?.parentSpanId || meta.parent_span_id),
    iteration: numberValue(meta?.iteration),
    startedAt,
    endedAt,
    durationMs: elapsedValue(startedAt, endedAt),
    progress,
  };

  const isToolKind =
    kind === 'tool' ||
    kind === 'business_data' ||
    kind === 'web_search' ||
    kind === 'solver';
  if (isToolKind) {
    block.toolName = toolName;
    block.toolCallId = stringValue(meta.tool_call_id);
    block.input = meta.input ?? null;
    block.output = stringValue(meta.output ?? meta.output_preview);
    block.outputRef = stringValue(meta.output_ref);
    block.outputSize = numberValue(meta.output_size);
    block.outputTruncated = Boolean(meta.output_truncated);
    block.outputProjection = plainObject(meta.output_projection) ?
      meta.output_projection :
      null;
    block.parsedOutput =
      block.outputProjection || parseOutput(meta.output ?? meta.output_preview);
    block.error = publicErrorMessage(meta.error) || progressError;
    block.durationMs = numberValue(meta.elapsed_ms) ?? block.durationMs;
  }
  if (kind === 'solver') {
    block.solverResult =
      plainObject(meta.solver_result) &&
      meta.solver_result.authoritative === true ?
        meta.solver_result :
        null;
    block.solverRejection = plainObject(meta.solver_rejection) ?
      meta.solver_rejection :
      null;
  }
  if (kind === 'artifacts') {
    block.artifacts = (Array.isArray(meta.artifacts) ? meta.artifacts : [])
      .filter(plainObject)
      .map((artifact, artifactIndex) => ({
        key: stringValue(artifact.output_ref || artifact.name || artifactIndex),
        name: stringValue(artifact.name || `artifact-${artifactIndex + 1}`),
        mediaType: stringValue(artifact.media_type || artifact.content_type),
        size: numberValue(artifact.size),
        sha256: stringValue(artifact.sha256),
        outputRef: stringValue(artifact.output_ref),
      }));
  }
  if (kind === 'web_search') {
    const parsedOutput = plainObject(block.parsedOutput) ?
      block.parsedOutput :
      {};
    block.query = stringValue(
      meta?.input?.query ??
        meta?.tool_args?.query ??
        meta?.tool_input?.query ??
        meta?.query ??
        parsedOutput.query,
    ).trim();
    block.sources = normalizeWebSources(parsedOutput);
    block.resultCount =
      numberValue(parsedOutput.result_count ?? parsedOutput.resultCount) ??
      (Array.isArray(parsedOutput.results) ||
      Array.isArray(parsedOutput.sources) ?
        block.sources.length :
        resultCountFromProgress(progress)) ??
      0;
    block.provider = stringValue(parsedOutput.provider).trim();
    block.responseTime = numberValue(
      parsedOutput.response_time ?? parsedOutput.responseTime,
    );
  }
  if (segment?.type === 'subagent_group') {
    block.subtype = 'group';
    block.group = normalizeSubagentGroup(segment, key);
  } else if (kind === 'subagent') {
    block.subtype = 'event';
    block.subagentId = stringValue(meta.subagent_id);
    block.taskId = stringValue(meta.task_id);
  }
  return block;
};

const lifecycleBlock = (message, messageKey, status, order) => {
  const awaiting = status === 'awaiting';
  const startedAt = timestampValue(message?.endTs ?? message?.ts);
  return {
    key: `${messageKey}:lifecycle`,
    kind: awaiting ? 'awaiting' : 'status',
    segmentType: 'lifecycle',
    order,
    status,
    terminal: TERMINAL_STATUSES.has(status),
    text: awaiting ?
      stringValue(message?.interaction?.prompt || message?.content) :
      publicErrorMessage(message?.meta?.error) ||
        stringValue(message?.meta?.cancel_reason),
    interaction: awaiting ? message?.interaction || null : null,
    startedAt,
    endedAt: startedAt,
    durationMs: 0,
    progress: [],
  };
};

const messageIdentity = (message, index) =>
  stringValue(message?.id).trim() ||
  (message?.runId ? `run:${message.runId}` : '') ||
  `${stringValue(message?.role) || 'message'}:${hashText(`${message?.content || ''}|${message?.ts || ''}`)}:${index}`;

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
const normalizeMessage = (message, index, occurrences) => {
  const key = uniqueKey(
    `message:${messageIdentity(message, index)}`,
    occurrences,
  );
  const role = stringValue(message?.role).trim() || 'assistant';
  const status = normalizeConversationStatus(
    message?.status,
    message?.isLoading,
  );
  const rawError = publicErrorMessage(message?.meta?.error);
  const errorCode = stringValue(message?.meta?.error_code).trim();
  const errorRetriable = message?.meta?.error_retriable === true;
  const cancelReason = stringValue(message?.meta?.cancel_reason);
  const errorPresentation =
    rawError || status === 'error' || status === 'cancelled' ?
      getWorkflowErrorPresentation(rawError, {
        status,
        errorCode,
        retriable: errorRetriable,
        cancelReason,
      }) :
      null;
  const recovery = normalizeWorkflowRecovery(message?.meta?.recovery);
  const blockOccurrences = new Map();
  const segments = Array.isArray(message?.streamSegments) ?
    message.streamSegments :
    [];
  const blocks = segments.map((segment, segmentIndex) =>
    normalizeSegment(segment, segmentIndex, key, blockOccurrences),
  );
  const hasContentBlock = blocks.some((block) => block.kind === 'content');
  if (stringValue(message?.content) && !hasContentBlock) {
    blocks.push({
      key: `${key}:content`,
      kind: 'content',
      segmentType: 'content',
      order: blocks.length,
      status: TERMINAL_STATUSES.has(status) ? 'finished' : status,
      terminal: TERMINAL_STATUSES.has(status),
      text: stringValue(message.content),
      summary: '',
      node: '',
      spanKey: '',
      parentKey: '',
      traceId: '',
      spanId: '',
      parentSpanId: '',
      startedAt: timestampValue(message?.ts),
      endedAt: timestampValue(message?.endTs),
      durationMs: null,
      progress: [],
    });
  }
  if (
    role === 'assistant' ||
    message?.runId ||
    message?.kind === 'agent_step'
  ) {
    blocks.push(lifecycleBlock(message, key, status, blocks.length));
  }

  const blockStarts = blocks
    .map((block) => block.startedAt)
    .filter((value) => value != null);
  const blockEnds = blocks
    .map((block) => block.endedAt)
    .filter((value) => value != null);
  const startedAt =
    timestampValue(message?.startTs ?? message?.ts) ??
    (blockStarts.length ? Math.min(...blockStarts) : null);
  const endedAt =
    timestampValue(message?.endTs) ??
    (TERMINAL_STATUSES.has(status) && blockEnds.length ?
      Math.max(...blockEnds) :
      null);
  return {
    key,
    id: stringValue(message?.id),
    role,
    kind: role === 'assistant' ? 'turn' : 'message',
    runId: stringValue(message?.runId),
    status,
    terminal: TERMINAL_STATUSES.has(status),
    isLoading: Boolean(message?.isLoading) && !TERMINAL_STATUSES.has(status),
    content: stringValue(message?.content),
    interaction: message?.interaction || null,
    options: Array.isArray(message?.options) ? message.options : [],
    error: rawError,
    errorCode,
    errorPresentation,
    recovery,
    cancelReason,
    startedAt,
    endedAt,
    durationMs: elapsedValue(startedAt, endedAt),
    iterationMetrics: Array.isArray(message?.iterationMetrics) ?
      message.iterationMetrics.slice() :
      [],
    blocks,
    order: index,
  };
};

const resolveMessages = (input) => {
  if (Array.isArray(input)) {
    return input;
  }
  if (Array.isArray(input?.messages) && input.messages.length > 0) {
    return input.messages;
  }
  return [];
};

/**
 * Canonical renderer input for a conversation. Hydration adapters must project
 * stored events into messages before they reach this renderer boundary.
 */
export const buildConversationViewModel = (input = []) => {
  const occurrences = new Map();
  const messages = resolveMessages(input).map((message, index) =>
    normalizeMessage(message, index, occurrences),
  );
  return {
    kind: 'conversation',
    messages,
    turns: messages.filter((message) => message.kind === 'turn'),
  };
};
