const QUERY_PREVIEW_LIMIT = 120;

const isPlainObject = (value) => (
  value != null
  && typeof value === 'object'
  && !Array.isArray(value)
);

const truncateText = (value, limit = QUERY_PREVIEW_LIMIT) => {
  if (value == null) return '';
  const text = String(value).trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
};

const formatCompactValue = (value) => {
  if (value == null) return 'null';
  if (typeof value === 'string') return truncateText(value, 64);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return `[${value.length} items]`;
  if (typeof value === 'object') return '{...}';
  return String(value);
};

const buildParamOnlyFallback = (params, rawText) => {
  if (isPlainObject(params)) {
    const keys = Object.keys(params);
    if (keys.length > 0) {
      const firstKey = keys[0];
      return `${firstKey}: ${formatCompactValue(params[firstKey])}`;
    }
  }

  const normalizedText = truncateText(rawText, 100);
  if (normalizedText) return normalizedText;
  return '无参数';
};

const buildDisplayParam = (toolType, params, rawText) => {
  if (toolType === 'ontology_context' && isPlainObject(params)) {
    const resource = String(params.resource || '').trim();
    const key = truncateText(params.key, QUERY_PREVIEW_LIMIT);
    if (resource === 'root') {
      return truncateText(params.ontology_name || params.ontology_label || params.name || params.ontology_id || '本体', QUERY_PREVIEW_LIMIT);
    }
    const resourceLabel = { operation: '逻辑', object: '实体', asset: '公共文件' }[resource] || truncateText(resource, 40);
    return [resourceLabel, key].filter(Boolean).join(': ') || truncateText(params.ontology_id, QUERY_PREVIEW_LIMIT);
  }

  if (toolType === 'skill' && isPlainObject(params)) {
    const resource = truncateText(params.resource, 40);
    const key = truncateText(params.key, QUERY_PREVIEW_LIMIT);
    return [params.skill_name, resource, key].filter(Boolean).join(' / ');
  }

  if (toolType === 'business_data_search' && isPlainObject(params)) {
    const query = truncateText(params.query, QUERY_PREVIEW_LIMIT);
    if (query) return query;
  }

  if (toolType === 'web_search' && isPlainObject(params)) {
    const query = truncateText(params.query, QUERY_PREVIEW_LIMIT);
    if (query) return query;
  }

  if (toolType === 'execute_script' && isPlainObject(params)) {
    const operationKey = truncateText(params.operation_key, QUERY_PREVIEW_LIMIT);
    if (operationKey) return operationKey;
  }
  if (toolType === 'memory' && isPlainObject(params)) {
    const content = truncateText(params.content, QUERY_PREVIEW_LIMIT);
    if (content) return content;
  }

  return buildParamOnlyFallback(params, rawText);
};

const buildFallbackSummary = (toolName, params, rawText) => {
  if (isPlainObject(params)) {
    const keys = Object.keys(params);
    if (keys.length > 0) {
      const firstKey = keys[0];
      return `${toolName}: ${firstKey}=${formatCompactValue(params[firstKey])}`;
    }
  }

  const normalizedText = truncateText(rawText, 100);
  if (normalizedText) return `${toolName}: ${normalizedText}`;
  return `${toolName}: 无参数`;
};

const buildSummary = (toolName, params, rawText) => {
  const safeName = toolName || 'unknown_tool';

  if (safeName === 'ontology_context' && isPlainObject(params)) {
    const resource = String(params.resource || '').trim();
    const key = truncateText(params.key, QUERY_PREVIEW_LIMIT);
    const ontologyLabel = truncateText(params.ontology_name || params.ontology_label || params.name || params.ontology_id, 80);
    if (resource === 'root') return ontologyLabel || '本体';
    const resourceLabel = { operation: '逻辑', object: '实体', asset: '公共文件' }[resource] || truncateText(resource, 40);
    return [ontologyLabel, resourceLabel, key].filter(Boolean).join(' / ');
  }

  if (safeName === 'skill' && isPlainObject(params)) {
    const resource = truncateText(params.resource, 40);
    const key = truncateText(params.key, QUERY_PREVIEW_LIMIT);
    const skillName = truncateText(params.skill_name, 80);
    return [skillName, resource, key].filter(Boolean).join(' / ');
  }

  if (safeName === 'business_data_search' && isPlainObject(params)) {
    const query = truncateText(params.query, QUERY_PREVIEW_LIMIT);
    if (query) return `${query}`;
  }

  if (safeName === 'web_search' && isPlainObject(params)) {
    const query = truncateText(params.query, QUERY_PREVIEW_LIMIT);
    if (query) return `${query}`;
  }

  if (safeName === 'execute_script' && isPlainObject(params)) {
    const operationKey = truncateText(params.operation_key, QUERY_PREVIEW_LIMIT);
    if (operationKey) return operationKey;
  }
  if (safeName === 'memory' && isPlainObject(params)) {
    const content = truncateText(params.content, 80);
    if (content) return content;
  }

  return buildFallbackSummary(safeName, params, rawText);
};

const resolveTargetRunId = (messages = [], activeRunId = null) => {
  if (typeof activeRunId === 'string' && activeRunId.trim()) {
    return activeRunId;
  }

  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg?.kind === 'agent_step' && typeof msg?.runId === 'string' && msg.runId.trim()) {
      return msg.runId;
    }
  }

  return null;
};

const normalizeToolParams = (segmentMeta = {}) => {
  if (isPlainObject(segmentMeta.input)) {
    return segmentMeta.input;
  }

  if (isPlainObject(segmentMeta.tool_args)) {
    return segmentMeta.tool_args;
  }

  return null;
};

const displayName_ZH = {
  'ontology_context': '加载本体',
  'skill': '技能',
  'business_data_search': '业务数据查询',
  'web_search': '联网搜索',
  'execute_script': '脚本执行',
  'memory': '记忆',
};

export const extractToolCallsForRun = (messages = [], activeRunId = null) => {
  const runId = resolveTargetRunId(messages, activeRunId);
  if (!runId) {
    return { runId: null, toolCalls: [] };
  }

  const toolCalls = [];
  let order = 0;
  let currentToolTurn = -1;
  let previousWasToolSegment = false;

  messages.forEach((msg) => {
    if (msg?.kind !== 'agent_step' || msg?.runId !== runId) return;
    const segments = Array.isArray(msg?.streamSegments) ? msg.streamSegments : [];

    segments.forEach((segment) => {
      if (segment?.type !== 'tool') {
        previousWasToolSegment = false;
        return;
      }

      if (!previousWasToolSegment) {
        currentToolTurn += 1;
      }
      previousWasToolSegment = true;

      const segmentMeta = isPlainObject(segment?.meta) ? segment.meta : {};
      const toolName = typeof segmentMeta.tool_name === 'string' && segmentMeta.tool_name.trim()
        ? segmentMeta.tool_name.trim()
        : 'unknown_tool';
      const params = normalizeToolParams(segmentMeta);
      const rawText = segment?.text == null ? '' : String(segment.text);
      const displayParam = buildDisplayParam(toolName, params, rawText);
      const toolCallId = typeof segmentMeta.tool_call_id === 'string' && segmentMeta.tool_call_id.trim()
        ? segmentMeta.tool_call_id
        : null;
      const output = typeof segmentMeta.output_preview === 'string' ? segmentMeta.output_preview : '';
      const error = typeof segmentMeta.error === 'string' ? segmentMeta.error : '';
      const rawStatus = typeof segmentMeta.status === 'string' ? segmentMeta.status.trim() : '';
      const elapsedMsRaw = Number(segmentMeta.elapsed_ms);
      const elapsedMs = Number.isFinite(elapsedMsRaw) ? Math.max(0, elapsedMsRaw) : null;

      toolCalls.push({
        id: toolCallId || `${runId}:tool:${order}`,
        runId,
        toolType: toolName,
        toolName: displayName_ZH[toolName] || toolName,
        params,
        summary: buildSummary(toolName, params, rawText),
        displayParam,
        rawText,
        output,
        error,
        status: rawStatus,
        elapsedMs,
        outputTruncated: Boolean(segmentMeta.output_truncated),
        order,
        toolTurn: currentToolTurn,
        paramsUnavailable: !params,
      });
      order += 1;
    });
  });

  return { runId, toolCalls };
};
