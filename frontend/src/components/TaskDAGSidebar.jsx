/**
 * TaskDAGSidebar - Right sidebar showing reasoning details for conversations.
 */

import { useMemo, useEffect, useState, useRef } from 'react';
import { PanelRightClose, PanelRightOpen, Maximize2, Minimize2, CircleCheck, CircleX, BrainCircuit, ChevronsDownUp, ChevronsUpDown, Loader2, ChevronDown } from 'lucide-react';
import {
  buildReasoningDetailGroups,
  getReasoningDetailEntryKey,
  resolveGroupIterationNumber,
} from '../utils/reasoningDetailGroups';
import { buildConversationTurns } from '../utils/conversationTurns';
import { buildWebSearchProcessModel } from '../utils/webSearchDisplay';

const isPlainObject = (value) => (
  value != null
  && typeof value === 'object'
  && !Array.isArray(value)
);

const TOOL_NAME_LABELS = {
  ontology_context: '加载本体',
  skill: '技能',
  business_data_search: '业务数据查询',
  web_search: '联网搜索',
  execute_script: '脚本执行',
  spawn_sub_agent: '子任务分发',
  memory: '记忆',
};

function getToolParams(meta = {}) {
  if (isPlainObject(meta?.input)) return meta.input;
  if (isPlainObject(meta?.tool_args)) return meta.tool_args;
  return null;
}

function formatCompactElapsed(elapsedMs) {
  const safeMs = Math.max(0, Number(elapsedMs) || 0);
  if (safeMs < 1000) return `${safeMs}ms`;
  const totalSeconds = Math.floor(safeMs / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function resolveSegmentElapsedMs(segment, anchorTs = null) {
  const metaElapsedRaw = Number(segment?.meta?.elapsed_ms);
  if (Number.isFinite(metaElapsedRaw) && metaElapsedRaw >= 0) {
    return Math.max(0, metaElapsedRaw);
  }

  const startTsRaw = typeof segment?.startTs === 'string' ? Number(segment.startTs) : segment?.startTs;
  const endTsRaw = typeof segment?.endTs === 'string' ? Number(segment.endTs) : segment?.endTs;
  const startTs = Number.isFinite(startTsRaw) ? startTsRaw : null;
  const endTs = Number.isFinite(endTsRaw) ? endTsRaw : null;
  if (startTs == null) return null;
  return Math.max(0, (endTs ?? anchorTs ?? Date.now()) - startTs);
}

function formatToolText(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (!(text.startsWith('{') || text.startsWith('['))) return text;
  try {
    const parsed = JSON.parse(text);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return text;
  }
}

function splitToolMessage(rawText, fallbackToolName = '') {
  const text = typeof rawText === 'string' ? rawText.trim() : '';
  const fallback = typeof fallbackToolName === 'string' ? fallbackToolName.trim() : '';
  if (!text) {
    return {
      toolHead: fallback || 'unknown_tool',
      toolRest: '',
    };
  }

  const strippedPrefix = text.replace(/^调用工具[:：]?\s*/u, '').trim();
  const normalized = strippedPrefix || text;
  const spaceIdx = normalized.indexOf(' ');
  if (spaceIdx === -1) {
    return {
      toolHead: normalized,
      toolRest: '',
    };
  }

  return {
    toolHead: normalized.slice(0, spaceIdx),
    toolRest: normalized.slice(spaceIdx + 1).trimStart(),
  };
}

function resolveOntologyDisplayName(params, ontologyLabelMap = null) {
  const safeParams = isPlainObject(params) ? params : {};
  const explicitName = String(
    safeParams.ontology_name
    || safeParams.ontology_label
    || safeParams.name
    || ''
  ).trim();
  if (explicitName) return explicitName;

  const ontologyId = String(safeParams.ontology_id || '').trim();
  if (!ontologyId) return '';
  return ontologyLabelMap?.get(ontologyId) || ontologyId;
}

function buildOntologyContextRest(params, ontologyLabelMap = null) {
  const safeParams = isPlainObject(params) ? params : {};
  const resource = String(safeParams.resource || '').trim();
  const key = String(safeParams.key || '').trim();
  const resourceLabels = {
    root: '',
    operation: '逻辑',
    object: '实体',
  };

  if (resource === 'root') {
    return resolveOntologyDisplayName(safeParams, ontologyLabelMap) || '本体';
  }

  const resourceLabel = resourceLabels[resource] || resource;
  return [resourceLabel, key].filter(Boolean).join(': ') || resolveOntologyDisplayName(safeParams, ontologyLabelMap);
}

function buildToolDisplay(toolName, params = null, rawText = '', ontologyLabelMap = null) {
  const canonicalToolName = TOOL_NAME_LABELS[toolName] || toolName || '工具调用';
  const safeParams = isPlainObject(params) ? params : {};

  if (toolName === 'ontology_context') {
    return {
      toolHead: canonicalToolName,
      toolRest: buildOntologyContextRest(safeParams, ontologyLabelMap),
    };
  }

  if (toolName === 'skill') {
    const resource = String(safeParams.resource || '').trim();
    const key = String(safeParams.key || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: [safeParams.skill_name, resource, key].filter(Boolean).join(' / '),
    };
  }

  if (toolName === 'business_data_search') {
    const query = String(safeParams.query || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: query || '',
    };
  }

  if (toolName === 'web_search') {
    const query = String(safeParams.query || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: query || '',
    };
  }

  if (toolName === 'execute_script') {
    const operationKey = String(safeParams.operation_key || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: operationKey || '',
    };
  }

  if (toolName === 'spawn_sub_agent') {
    const taskCount = Array.isArray(safeParams.tasks) ? safeParams.tasks.length : 0;
    return {
      toolHead: canonicalToolName,
      toolRest: taskCount > 0 ? `${taskCount} 个子任务` : '嵌套子任务',
    };
  }

  const parsed = splitToolMessage(rawText, canonicalToolName || toolName);
  const fallbackHead = canonicalToolName || toolName || 'unknown_tool';
  const normalizedHead = String(parsed.toolHead || '').trim().toLowerCase();
  const normalizedToolName = String(toolName || '').trim().toLowerCase();
  const toolHead = (
    normalizedHead === normalizedToolName
    || normalizedHead === '读取'
    || normalizedHead === '查找'
    || normalizedHead === '搜索'
  ) ? fallbackHead : (parsed.toolHead || fallbackHead);

  return {
    toolHead,
    toolRest: parsed.toolRest || '',
  };
}

function parseToolSegmentDisplay(segment, ontologyLabelMap = null) {
  const meta = segment?.meta || {};
  const rawToolName = typeof meta?.tool_name === 'string' ? meta.tool_name.trim() : '';
  const segmentText = typeof segment?.text === 'string' ? segment.text.trim() : '';
  const eventMessage = typeof meta?.message === 'string' ? meta.message.trim() : '';
  const rawText = eventMessage || segmentText;
  const { toolHead, toolRest } = buildToolDisplay(rawToolName, getToolParams(meta), rawText, ontologyLabelMap);

  return {
    rawText,
    toolHead,
    toolRest,
  };
}

function getOrderedSubagentChildren(segment) {
  const childrenMap = segment?.children && typeof segment.children === 'object' ? segment.children : {};
  const ordered = (Array.isArray(segment?.taskOrder) ? segment.taskOrder : [])
    .map((subagentId) => childrenMap?.[subagentId])
    .filter(Boolean);

  const seenIds = new Set(ordered.map((child) => child?.subagentId).filter(Boolean));
  Object.values(childrenMap).forEach((child) => {
    if (!child) return;
    const childId = child?.subagentId;
    if (childId && seenIds.has(childId)) return;
    ordered.push(child);
  });

  return ordered;
}

function getSubagentChildGroups(child) {
  return Array.isArray(child?.childGroups) ? child.childGroups : [];
}

function getSubagentTaskTitle(child) {
  return String(child?.task || '').split('\n')[0].trim() || child?.taskId || `子任务 ${(child?.taskIndex ?? 0) + 1}`;
}

function getSubagentTaskBody(child) {
  return String(child?.task || '').split('\n').slice(1).join('\n').trim();
}

function extractRawTextFromChildToolEvent(event) {
  if (typeof event?.output === 'string' && event.output.trim()) return event.output.trim();
  if (typeof event?.error === 'string' && event.error.trim()) return event.error.trim();
  const progressItems = Array.isArray(event?.progress) ? event.progress : [];
  for (let i = progressItems.length - 1; i >= 0; i -= 1) {
    const item = progressItems[i];
    const text = String(item?.ontology?.content || item?.text || '').trim();
    if (text) return text;
  }
  return '';
}

function normalizeToolEventStatus(status, finished = false) {
  const normalized = typeof status === 'string' ? status.trim().toLowerCase() : '';
  if (['error', 'failed', 'fail', 'cancelled', 'canceled', 'aborted'].includes(normalized)) return 'error';
  if (['completed', 'complete', 'done', 'success', 'succeeded', 'ok'].includes(normalized)) return 'completed';
  return finished ? 'completed' : 'running';
}

function normalizeSegmentToolStatus(segment) {
  const rawStatus = segment?.toolStatus || segment?.meta?.tool_status || segment?.meta?.status || segment?.state;
  return normalizeToolEventStatus(rawStatus, Boolean(segment?.finished));
}

function formatPreciseElapsed(elapsedMs) {
  const safeMs = Math.max(0, Number(elapsedMs) || 0);
  if (safeMs < 1000) return `${safeMs.toFixed(0)}ms`;
  if (safeMs < 60000) return `${(safeMs / 1000).toFixed(2)}s`;
  const minutes = Math.floor(safeMs / 60000);
  const seconds = ((safeMs % 60000) / 1000).toFixed(2);
  return `${minutes}m ${seconds}s`;
}

function buildLlmTimingDisplay(iterationMetric) {
  const streamElapsedMs = Number.isFinite(Number(iterationMetric?.llmStreamElapsedMs))
    ? Number(iterationMetric.llmStreamElapsedMs)
    : null;
  const elapsedMs = streamElapsedMs != null
    ? streamElapsedMs
    : (
      Number.isFinite(Number(iterationMetric?.llmElapsedMs))
        ? Number(iterationMetric.llmElapsedMs)
        : null
    );
  if (elapsedMs == null) return null;

  const details = [];
  if (Number.isFinite(Number(iterationMetric?.llmWallElapsedMs))) {
    details.push(`LLM等待 ${formatPreciseElapsed(Number(iterationMetric.llmWallElapsedMs))}`);
  }

  return {
    elapsedMs,
    title: streamElapsedMs != null
      ? ['从开始调用 LLM 到模型输出完成的时间。', ...details].join(' ')
      : 'LLM 耗时。',
  };
}

function sumToolElapsedMs(segments = [], anchorTs = null) {
  return segments.reduce((sum, segment) => {
    if (segment?.type !== 'tool') return sum;
    const elapsed = resolveSegmentElapsedMs(segment, anchorTs);
    return sum + (Number.isFinite(elapsed) ? elapsed : 0);
  }, 0);
}

function getIterationStatusLabel(status) {
  switch (String(status || '').trim()) {
    case 'continue':
      return '继续下一轮';
    case 'final_output':
      return '产出最终答案';
    case 'terminal_tool_batch':
      return '工具直接结束';
    case 'success':
    case 'terminal':
      return '成功结束';
    case 'cancelled':
      return '已取消';
    case 'error':
      return '失败';
    case 'iteration_limit_exceeded':
      return '超过轮次上限';
    default:
      return '';
  }
}

function renderSubagentToolEvent(event, key, ontologyLabelMap = null) {
  const toolName = typeof event?.toolName === 'string' ? event.toolName.trim() : '';
  const params = event?.input || null;
  const rawText = extractRawTextFromChildToolEvent(event);
  const { toolHead, toolRest } = buildToolDisplay(toolName, params, rawText, ontologyLabelMap);
  const progressList = Array.isArray(event?.progress) ? event.progress : [];
  const hasParams = isPlainObject(params) && Object.keys(params).length > 0;
  const hasDetail = progressList.length > 0 || Boolean(event?.output) || Boolean(event?.error) || hasParams;
  const toolStatus = normalizeToolEventStatus(event?.status, Boolean(event?.finished));

  return (
    <details
      key={key}
      className={`subagent-tool-call${toolStatus === 'error' ? ' is-error' : ''}${toolStatus === 'completed' ? ' is-done' : ''}`}
      open={toolStatus === 'running'}
    >
      <summary className="subagent-tool-call-header">
        <span className="subagent-tool-call-icon">
          {toolStatus === 'completed' && <CircleCheck size={14} />}
          {toolStatus === 'error' && <CircleX size={14} />}
          {toolStatus === 'running' && <Loader2 className="spin" size={14} />}
        </span>
        <span className="subagent-tool-call-name">{toolHead}</span>
        {toolRest && <span className="subagent-tool-call-arg">{toolRest}</span>}
        {hasDetail && <ChevronDown size={12} className="subagent-tool-call-chevron" />}
      </summary>
      {hasDetail && (
        <div className="subagent-tool-call-body">
          {hasParams && (
            <>
              <div className="tool-call-item-section-title">参数</div>
              <pre className="tool-call-json">{JSON.stringify(params, null, 2)}</pre>
            </>
          )}
          {progressList.map((progressEntry, idx) => (
            <div key={`${key}-progress-${idx}`} className="subagent-tool-call-progress">
              {progressEntry?.web?.content || progressEntry?.ontology?.content || progressEntry?.text || ''}
            </div>
          ))}
          {event?.output && <div className="subagent-tool-call-output">{event.output}</div>}
          {event?.error && <div className="subagent-tool-call-error">{event.error}</div>}
        </div>
      )}
    </details>
  );
}

function renderMainScriptToolEvent(segment, key, nowTs = null, ontologyLabelMap = null) {
  const meta = segment?.meta || {};
  const params = getToolParams(meta);
  const { toolHead, toolRest } = parseToolSegmentDisplay(segment, ontologyLabelMap);
  const toolStatus = normalizeSegmentToolStatus(segment);
  const outputPreview = typeof meta?.output_preview === 'string' ? meta.output_preview.trim() : '';
  const errorText = typeof meta?.error === 'string' ? meta.error.trim() : '';
  const uniqueErrorText = errorText && errorText !== outputPreview ? errorText : '';
  const outputTruncated = Boolean(meta?.output_truncated);
  const hasParams = isPlainObject(params) && Object.keys(params).length > 0;
  const hasDetail = hasParams || Boolean(outputPreview) || Boolean(errorText);
  const formattedOutput = formatToolText(outputPreview || (toolStatus === 'completed' ? '脚本执行成功，无输出。' : ''));
  const formattedError = formatToolText(uniqueErrorText);
  const elapsedMs = resolveSegmentElapsedMs(segment, nowTs);
  const elapsedLabel = elapsedMs == null ? '' : formatCompactElapsed(elapsedMs);

  return (
    <details
      key={key}
      className={`stream-reasoning-block script-process-block${toolStatus === 'error' ? ' is-error' : ''}${toolStatus === 'completed' ? ' is-done' : ''}`}
    >
      <summary className="stream-reasoning-header ontology-process-summary script-process-summary">
        <span className="stream-inline stream-inline-tools">
          <span className="stream-tool-head">{toolHead}</span>
          {toolRest && <span className="stream-tool-rest" title={toolRest}>{toolRest}</span>}
          <span className="stream-fold-arrow">▶</span>
        </span>
        <span className={`stream-tool-status-icon status-${toolStatus}`}>
          {elapsedLabel && (
            <span className="stream-tool-elapsed reasoning-detail-elapsed reasoning-detail-elapsed-right">
              {elapsedLabel}
            </span>
          )}
          {toolStatus === 'completed' && <CircleCheck size={14} />}
          {toolStatus === 'error' && <CircleX size={14} />}
          {toolStatus === 'running' && <Loader2 className="spin" size={14} />}
        </span>
      </summary>
      {hasDetail && (
        <div className="stream-reasoning-content ontology-process-content reasoning-detail-content">
          {hasParams && (
            <>
              <div className="tool-call-item-section-title">输入参数</div>
              <pre className="tool-call-json">{JSON.stringify(params, null, 2)}</pre>
            </>
          )}
          {formattedOutput && (
            <>
              <div className="tool-call-item-section-title">执行结果</div>
              <pre className="tool-call-raw script-tool-block">{formattedOutput}</pre>
              {outputTruncated && (
                <div className="tool-call-legacy-hint">结果过长，前端仅展示前 4096 个字符。</div>
              )}
            </>
          )}
          {formattedError && (
            <>
              <div className="tool-call-item-section-title">错误信息</div>
              <pre className="tool-call-raw script-tool-block script-tool-block-error">{formattedError}</pre>
            </>
          )}
        </div>
      )}
    </details>
  );
}

function renderMainWebSearchToolEvent(segment, key, nowTs = null, ontologyLabelMap = null) {
  const model = buildWebSearchProcessModel({
    segment,
    messageStatus: segment?.state || segment?.meta?.status || '',
    nowTs,
  });
  const { toolHead, toolRest } = parseToolSegmentDisplay(segment, ontologyLabelMap);
  if (!model) return null;

  const toolStatus = model.isFailed ? 'error' : model.isCompleted ? 'completed' : 'running';
  const elapsedLabel = model.elapsedMs == null ? '' : formatCompactElapsed(model.elapsedMs);
  const summaryTailFull = model.query || toolRest || '';
  const summaryTailRaw = model.query ? `正在搜索: ${model.query}` : summaryTailFull;
  const summaryTail = summaryTailRaw.length > 36
    ? `${summaryTailRaw.slice(0, 36)}...`
    : summaryTailRaw;
  const resultLabel = model.resultCount > 0 ? `返回 ${model.resultCount} 条结果` : '';
  const responseTimeLabel = model.responseTime == null ? '' : `API ${Number(model.responseTime).toFixed(2)}s`;

  return (
    <details
      key={key}
      className={`stream-reasoning-block ontology-process-block web-search-process-block${toolStatus === 'error' ? ' is-error' : ''}${toolStatus === 'completed' ? ' is-done' : ''}`}
    >
      <summary className="stream-reasoning-header ontology-process-summary web-search-process-summary">
        <span className="stream-inline stream-inline-tools">
          <span className="stream-tool-head">{toolHead}</span>
          {summaryTail && (
            <span className="stream-tool-rest ontology-query-rest" title={summaryTailFull}>
              {summaryTail}
            </span>
          )}
          <span className="stream-fold-arrow">▶</span>
        </span>
        <span className={`stream-tool-status-icon status-${toolStatus}`}>
          {elapsedLabel && (
            <span className="stream-tool-elapsed reasoning-detail-elapsed reasoning-detail-elapsed-right">
              {elapsedLabel}
            </span>
          )}
          {toolStatus === 'completed' && <CircleCheck size={14} />}
          {toolStatus === 'error' && <CircleX size={14} />}
          {toolStatus === 'running' && <Loader2 className="spin" size={14} />}
        </span>
      </summary>
      <div className="stream-reasoning-content ontology-process-content reasoning-detail-content web-search-content-wrapper">
        {model.query && (
          <div className="db-search-query">
            <span className="db-search-query-label">搜索词:</span>
            <span className="db-search-query-value">{model.query}</span>
          </div>
        )}
        {model.timeline.length > 0 && (
          <div className="db-search-events-timeline web-search-events-timeline">
            {model.timeline.map((event, evtIdx) => (
              <div key={`${key}-web-${evtIdx}`} className="db-search-event-item">
                <div className="db-search-event-timeline-dot web-search-event-timeline-dot" />
                <div className="db-search-event-card">
                  <span className="db-search-event-content">{event.content}</span>
                </div>
              </div>
            ))}
          </div>
        )}
        {(resultLabel || model.provider || responseTimeLabel) && (
          <div className="web-search-result-meta">
            {resultLabel && <span>{resultLabel}</span>}
            {model.provider && <span>{model.provider}</span>}
            {responseTimeLabel && <span>{responseTimeLabel}</span>}
          </div>
        )}
        {model.sources.length > 0 && (
          <div className="web-search-sources">
            {model.sources.slice(0, 5).map((source) => (
              <div key={`${key}-source-${source.rank}-${source.url || source.title}`} className="web-search-source-item">
                <div className="web-search-source-heading">
                  {source.url ? (
                    <a href={source.url} target="_blank" rel="noreferrer" className="web-search-source-title">
                      {source.rank}. {source.title}
                    </a>
                  ) : (
                    <span className="web-search-source-title">{source.rank}. {source.title}</span>
                  )}
                  {source.domain && <span className="web-search-source-domain">{source.domain}</span>}
                </div>
                {source.snippet && <div className="web-search-source-snippet">{source.snippet}</div>}
              </div>
            ))}
          </div>
        )}
        {model.errorText && (
          <div className="subagent-tool-call-error">{model.errorText}</div>
        )}
      </div>
    </details>
  );
}

function renderSubagentChildEvent(event, key, ontologyLabelMap = null) {
  if (event?.kind === 'tool') {
    return renderSubagentToolEvent(event, key, ontologyLabelMap);
  }

  if (event?.kind === 'token') {
    const isReasoning = event?.channel === 'reasoning';
    return (
      <div key={key} className="stream-state-line reasoning-detail-state-line">
        <span className="stream-state-node">{isReasoning ? '思考' : '输出'}</span>
        <span className="stream-state-text">{event?.text || ''}</span>
      </div>
    );
  }

  return null;
}

function renderSubagentGroupSegment(segment, key, level = 0, ontologyLabelMap = null) {
  const childrenList = getOrderedSubagentChildren(segment);
  const taskCount = segment?.taskCount || childrenList.length;
  const groupStatus = normalizeToolEventStatus(segment?.status, Boolean(segment?.finished));

  return (
    <div key={key} className={`subagent-group${level > 0 ? ' subagent-group-nested' : ''}`}>
      <div className="subagent-group-header">
        <span className="subagent-group-title">
          {groupStatus === 'completed' && <CircleCheck size={16} />}
          {groupStatus === 'error' && <CircleX size={16} />}
          {groupStatus === 'running' && <Loader2 className="spin" size={16} />}
          <span>{level > 0 ? `嵌套执行 ${taskCount} 个子任务` : `并行执行 ${taskCount} 个子任务`}</span>
        </span>
      </div>
      <div className="subagent-group-body">
        {childrenList.map((child) => {
          const childStatus = normalizeToolEventStatus(child?.status, Boolean(child?.endTs));
          const taskTitle = getSubagentTaskTitle(child);
          const taskBody = getSubagentTaskBody(child);
          const childEvents = Array.isArray(child?.events) ? child.events : [];
          const childGroups = getSubagentChildGroups(child);
          const shouldOpen = level < 2 || childStatus === 'running' || childStatus === 'error';

          return (
            <details
              key={`${key}-${child?.subagentId || taskTitle}`}
              className={`subagent-card subagent-card-level-${Math.min(level, 4)}`}
              open={shouldOpen}
            >
              <summary className="subagent-card-header">
                <span className="subagent-card-status">
                  {childStatus === 'completed' && <CircleCheck size={14} />}
                  {childStatus === 'error' && <CircleX size={14} />}
                  {childStatus === 'running' && <Loader2 className="spin" size={14} />}
                </span>
                <span className="subagent-card-title">{taskTitle}</span>
                <ChevronDown size={14} className="subagent-card-chevron" />
              </summary>
              <div className="subagent-card-body">
                {taskBody && (
                  <div className="subagent-card-task-body">
                    <pre className="tool-call-raw">{taskBody}</pre>
                  </div>
                )}
                {childEvents.length > 0 && (
                  <div className="subagent-card-events">
                    {childEvents.map((event, eventIndex) => renderSubagentChildEvent(event, `${key}-${child?.subagentId || 'subagent'}-${eventIndex}`, ontologyLabelMap))}
                  </div>
                )}
                {childGroups.length > 0 && (
                  <div className="subagent-nested-groups">
                    {childGroups.map((group, groupIndex) => renderSubagentGroupSegment(
                      group,
                      `${key}-${child?.subagentId || 'subagent'}-nested-${group?.groupId || groupIndex}`,
                      level + 1,
                      ontologyLabelMap,
                    ))}
                  </div>
                )}
                {child?.error && <div className="subagent-card-error">{child.error}</div>}
              </div>
            </details>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Main sidebar component
 */
function TaskDAGSidebar({ isOpen, onToggle, messages, ontologyOptions = [], activeReasoningSegment, onSegmentClick = null }) {
  const [isWide, setIsWide] = useState(true);
  const reasoningListRef = useRef(null);
  const ontologyLabelMap = useMemo(() => {
    const map = new Map();
    (Array.isArray(ontologyOptions) ? ontologyOptions : []).forEach((ontology) => {
      const id = String(ontology?.id || '').trim();
      if (!id) return;
      map.set(id, String(ontology?.name || id).trim() || id);
    });
    return map;
  }, [ontologyOptions]);

  // Build conversation turns for persistent per-turn display
  const conversationTurns = useMemo(() => buildConversationTurns(messages || []), [messages]);

  const [now, setNow] = useState(() => Date.now());
  const activeTurnIndex = activeReasoningSegment?.turnIndex ?? null;
  const activeGroupIndex = activeReasoningSegment?.globalIndex ?? null;

  useEffect(() => {
    const intervalId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(intervalId);
  }, []);


  // Expand the right accordion when a segment is clicked.
  useEffect(() => {
    if (!activeReasoningSegment || !isOpen) return undefined;
    const { turnIndex, globalIndex } = activeReasoningSegment;

    let retryTimer = null;
    let rafId = null;
    let cancelled = false;
    const maxRetries = 8;

    const tryOpenTarget = (attempt = 0) => {
      if (cancelled) return;
      const list = reasoningListRef.current;
      if (!list) {
        if (attempt < maxRetries) {
          retryTimer = setTimeout(() => tryOpenTarget(attempt + 1), 40);
        }
        return;
      }

      const turnEl = list.querySelector(`[data-turn-index="${turnIndex}"]`);
      if (!turnEl) {
        if (attempt < maxRetries) {
          retryTimer = setTimeout(() => tryOpenTarget(attempt + 1), 40);
        }
        return;
      }

      turnEl.open = true;

      // Group index repeats across turns; always scope lookup to the matched turn.
      const groupEl = turnEl.querySelector(`[data-group-index="${globalIndex}"]`);
      if (groupEl) {
        if ('open' in groupEl) {
          groupEl.open = true;
        }
        groupEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        return;
      }

      if (attempt < maxRetries) {
        retryTimer = setTimeout(() => tryOpenTarget(attempt + 1), 40);
      }
    };

    rafId = requestAnimationFrame(() => tryOpenTarget(0));

    return () => {
      cancelled = true;
      if (rafId) cancelAnimationFrame(rafId);
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [activeReasoningSegment, isOpen]);

  return (
    <div className={`task-dag-sidebar ${isOpen ? 'open' : 'collapsed'} ${isWide ? 'wide' : ''}`}>
      {/* Toggle button - always visible */}
      <button
        className="dag-sidebar-toggle"
        onClick={onToggle}
        title={isOpen ? 'Collapse DAG view' : 'Expand DAG view'}
        type="button"
      >
        {isOpen ? <PanelRightClose size={18} /> : <PanelRightOpen size={18} />}
      </button>

      {isOpen && (
        <button
          className="dag-sidebar-width-toggle"
          onClick={() => setIsWide((prev) => !prev)}
          title={isWide ? 'Normal width' : 'Expand sidebar width'}
          type="button"
        >
          {isWide ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
        </button>
      )}

      {/* Content only shown when open */}
      {isOpen && (
        <div className="dag-sidebar-content">
          <>
              <div className="dag-sidebar-header">
                <div className="dag-sidebar-title">
                  <BrainCircuit size={16} />
                  <span>推理细节</span>
                </div>
                <div className="dag-sidebar-info">
                  <span className="tool-call-count">{conversationTurns.length} 轮对话</span>
                  <div className="reasoning-expand-group">
                    <button
                      type="button"
                      className="reasoning-expand-btn"
                      title="全部展开"
                      onClick={() => {
                        reasoningListRef.current?.querySelectorAll('details').forEach((el) => { el.open = true; });
                      }}
                    ><ChevronsUpDown size={13} /></button>
                    <button
                      type="button"
                      className="reasoning-expand-btn"
                      title="全部折叠"
                      onClick={() => {
                        reasoningListRef.current?.querySelectorAll('details').forEach((el) => { el.open = false; });
                      }}
                    ><ChevronsDownUp size={13} /></button>
                  </div>
                </div>
              </div>
              <div className="reasoning-detail-list" ref={reasoningListRef}>
                {conversationTurns.length === 0 ? (
                  <div className="reasoning-detail-empty">暂无推理记录</div>
                ) : (
                  conversationTurns.map((turn) => {
                    let offset = 0;
                    const allGroups = [];
                    turn.agentMsgs.forEach((agentMsg) => {
                      const { groups, visibleGroupCount } = buildReasoningDetailGroups(agentMsg.streamSegments || []);
                      groups.forEach((group) => {
                        allGroups.push({
                          ...group,
                          globalIndex: group.localVisibleIndex == null ? null : (offset + group.localVisibleIndex),
                        });
                      });
                      offset += visibleGroupCount;
                    });
                    const iterationMetrics = [];
                    const seenIterationNumbers = new Set();
                    turn.agentMsgs.forEach((agentMsg) => {
                      const metrics = Array.isArray(agentMsg?.iterationMetrics) ? agentMsg.iterationMetrics : [];
                      metrics.forEach((metric) => {
                        const iterationValue = Number(metric?.iteration);
                        if (!Number.isFinite(iterationValue) || iterationValue <= 0) return;
                        const iteration = Math.floor(iterationValue);
                        if (seenIterationNumbers.has(iteration)) return;
                        seenIterationNumbers.add(iteration);
                        iterationMetrics.push(metric);
                      });
                    });
                    iterationMetrics.sort((a, b) => Number(a?.iteration || 0) - Number(b?.iteration || 0));
                    const iterationMetricMap = new Map(
                      iterationMetrics.map((metric) => [Number(metric?.iteration), metric])
                    );
                    const groupEntries = allGroups.map(({ globalIndex, processSegs, contentSegs, actualIteration }, groupIdx) => ({
                      globalIndex,
                      processSegs,
                      contentSegs,
                      groupIdx,
                      actualIteration: actualIteration ?? resolveGroupIterationNumber(processSegs, contentSegs),
                    }));
                    const representedIterations = new Set(
                      groupEntries
                        .map((entry) => entry.actualIteration)
                        .filter((value) => Number.isFinite(value) && value > 0)
                    );
                    const maxIteration = Math.max(
                      0,
                      ...iterationMetrics.map((metric) => Number(metric?.iteration || 0)),
                      ...groupEntries.map((entry) => Number(entry?.actualIteration || 0)),
                    );
                    const orderedDetailEntries = [];
                    for (let iterationNumber = 1; iterationNumber <= maxIteration; iterationNumber += 1) {
                      const metric = iterationMetricMap.get(iterationNumber) || null;
                      if (metric && !representedIterations.has(iterationNumber)) {
                        orderedDetailEntries.push({
                          kind: 'metric-only',
                          iteration: iterationNumber,
                          iterationMetric: metric,
                        });
                      }
                      groupEntries
                        .filter((entry) => entry.actualIteration === iterationNumber)
                        .forEach((entry) => {
                          orderedDetailEntries.push({
                            kind: 'group',
                            iteration: iterationNumber,
                            iterationMetric: metric,
                            ...entry,
                          });
                        });
                    }
                    groupEntries
                      .filter((entry) => !(Number.isFinite(entry.actualIteration) && entry.actualIteration > 0))
                      .forEach((entry) => {
                        orderedDetailEntries.push({
                          kind: 'group',
                          iteration: entry.groupIdx + 1,
                          iterationMetric: iterationMetricMap.get(entry.groupIdx + 1) || null,
                          ...entry,
                        });
                      });

                    const rawRequest = turn.userMsg?.content || '';
                    const turnLabel = rawRequest.length > 40
                      ? `${rawRequest.slice(0, 40)}…`
                      : rawRequest || `轮 ${turn.index}`;
                    const turnToolCount = allGroups.reduce(
                      (sum, group) => sum + group.processSegs.filter((s) => s.type === 'tool').length,
                      0
                    );
                    const turnReasoningCount = allGroups.reduce(
                      (sum, group) => sum + group.processSegs.filter((s) => s.type === 'reasoning').length,
                      0
                    );
                    const turnMetaParts = [];
                    if (turnReasoningCount > 0) turnMetaParts.push(`${turnReasoningCount}轮思考`);
                    if (turnToolCount > 0) turnMetaParts.push(`${turnToolCount}轮工具调用`);
                    const turnMetaLabel = turnMetaParts.length > 0 ? turnMetaParts.join('，') : '过程详情';
                    const isLastTurn = turn.index === conversationTurns.length;
                    const isActiveTurn = activeTurnIndex === turn.index && activeGroupIndex != null;

                    return (
                      <details
                        key={turn.index}
                        className={`turn-accordion-item${isActiveTurn ? ' turn-accordion-item-active' : ''}`}
                        open={isLastTurn}
                        data-turn-index={turn.index}
                      >
                        <summary className="turn-accordion-summary">
                          <span className="turn-accordion-preview turn-accordion-request">{turnLabel}</span>
                          <span className="turn-accordion-meta">{turnMetaLabel}</span>
                        </summary>
                        <div className="turn-accordion-body">
                          {orderedDetailEntries.length === 0 ? (
                            <div className="reasoning-detail-empty">暂无推理记录</div>
                          ) : (
                            orderedDetailEntries.map((entry, entryIdx) => {
                              if (entry.kind === 'metric-only') {
                                const llmElapsedMs = Number.isFinite(Number(entry?.iterationMetric?.llmElapsedMs))
                                  ? Number(entry.iterationMetric.llmElapsedMs)
                                  : null;
                                const llmTimingDisplay = buildLlmTimingDisplay(entry?.iterationMetric);
                                const displayedLlmElapsedMs = llmTimingDisplay?.elapsedMs ?? llmElapsedMs;
                                const toolsElapsedMs = Number.isFinite(Number(entry?.iterationMetric?.toolsElapsedMs))
                                  ? Number(entry.iterationMetric.toolsElapsedMs)
                                  : null;
                                const totalElapsedMs = Number.isFinite(Number(entry?.iterationMetric?.totalElapsedMs))
                                  ? Number(entry.iterationMetric.totalElapsedMs)
                                  : (
                                    displayedLlmElapsedMs != null || toolsElapsedMs != null
                                      ? Math.max(0, Number(displayedLlmElapsedMs || 0) + Number(toolsElapsedMs || 0))
                                      : null
                                  );
                                const iterationStatusLabel = getIterationStatusLabel(entry?.iterationMetric?.status);
                                return (
                                  <div key={`metric-only-${entry.iteration}`} className="reasoning-detail-part-wrap">
                                    <div className="reasoning-detail-part">
                                      <div className="reasoning-iteration-summary">
                                        <div className="reasoning-iteration-summary-main">
                                          <span className="reasoning-iteration-badge">第{entry.iteration}轮</span>
                                          {iterationStatusLabel && (
                                            <span className="reasoning-iteration-status">{iterationStatusLabel}</span>
                                          )}
                                        </div>
                                        <div className="reasoning-iteration-metrics">
                                          {llmTimingDisplay && (
                                            <span
                                              className="reasoning-iteration-metric"
                                              title={llmTimingDisplay.title}
                                            >
                                              LLM {formatPreciseElapsed(llmTimingDisplay.elapsedMs)}
                                            </span>
                                          )}
                                          {totalElapsedMs != null && (
                                            <span
                                              className="reasoning-iteration-metric reasoning-iteration-metric-total"
                                              title="本轮墙钟总时长，包含 prompt 构建和状态编排开销，因此不一定严格等于 LLM + 工具。"
                                            >
                                              总计 {formatPreciseElapsed(totalElapsedMs)}
                                            </span>
                                          )}
                                        </div>
                                      </div>
                                      <div className="reasoning-detail-empty reasoning-detail-empty-inline">
                                        这一轮已记录耗时，但当前没有可展示的过程细节。
                                      </div>
                                    </div>
                                    {entryIdx < orderedDetailEntries.length - 1 && (
                                      <div className="reasoning-detail-part-divider" aria-hidden="true" />
                                    )}
                                  </div>
                                );
                              }
                              const {
                                globalIndex,
                                processSegs,
                                contentSegs,
                                groupIdx,
                                actualIteration,
                                iterationMetric,
                                iteration,
                              } = entry;
                              const isActiveDetail = (
                                globalIndex != null
                                &&
                                activeTurnIndex === turn.index
                                && activeGroupIndex === globalIndex
                              );
                              const displayIteration = actualIteration || iteration || (groupIdx + 1);
                              const toolCount = processSegs.filter((seg) => seg?.type === 'tool').length;
                              const fallbackToolElapsedMs = sumToolElapsedMs(processSegs, now);
                              const llmElapsedMs = Number.isFinite(Number(iterationMetric?.llmElapsedMs))
                                ? Number(iterationMetric.llmElapsedMs)
                                : null;
                              const llmTimingDisplay = buildLlmTimingDisplay(iterationMetric);
                              const displayedLlmElapsedMs = llmTimingDisplay?.elapsedMs ?? llmElapsedMs;
                              const toolsElapsedMs = Number.isFinite(Number(iterationMetric?.toolsElapsedMs))
                                ? Number(iterationMetric.toolsElapsedMs)
                                : (fallbackToolElapsedMs > 0 ? fallbackToolElapsedMs : null);
                              const totalElapsedMs = Number.isFinite(Number(iterationMetric?.totalElapsedMs))
                                ? Number(iterationMetric.totalElapsedMs)
                                : (
                                  displayedLlmElapsedMs != null || toolsElapsedMs != null
                                    ? Math.max(0, Number(displayedLlmElapsedMs || 0) + Number(toolsElapsedMs || 0))
                                    : null
                                );
                              const iterationStatusLabel = getIterationStatusLabel(iterationMetric?.status);
                              const detailEntryKey = getReasoningDetailEntryKey(entry, entryIdx);
                              const renderedOntologyPanelKeys = new Set();
                              const renderedProcessSegs = processSegs.map((seg, si) => {
                                if (seg.type === 'reasoning') {
                                  const elapsedMs = (seg.endTs && seg.startTs) ? Math.max(0, seg.endTs - seg.startTs) : 0;
                                  const summaryText = typeof seg?.meta?.reasoning_summary === 'string'
                                    ? seg.meta.reasoning_summary.trim()
                                    : '';
                                  const previewSource = summaryText || (
                                    typeof seg.text === 'string' ? seg.text.trim() : ''
                                  );
                                  const reasoningPreview = previewSource.length > 0
                                    ? (previewSource.length > 36 ? `${previewSource.slice(0, 36)}...` : previewSource)
                                    : '';
                                  return (
                                    <details key={`reasoning-${detailEntryKey}-${si}`} className="stream-reasoning-block">
                                      <summary
                                        className={`stream-reasoning-header${onSegmentClick && globalIndex != null ? ' stream-reasoning-header-clickable' : ''}`}
                                        onClick={onSegmentClick && globalIndex != null ? () => onSegmentClick({ turnIndex: turn.index, globalIndex }) : undefined}
                                        title={onSegmentClick && globalIndex != null ? '查看对应结果流' : undefined}
                                      >
                                        <span className="reasoning-detail-thinking-label">
                                          {reasoningPreview && (
                                            <span className="reasoning-detail-preview">{reasoningPreview}</span>
                                          )}
                                          <span className="stream-fold-arrow">▶</span>
                                          {elapsedMs > 0 && (
                                            <span className="stream-tool-elapsed reasoning-detail-elapsed">
                                              {elapsedMs < 1000 ? `${elapsedMs}ms` : `${(elapsedMs / 1000).toFixed(1)}s`}
                                            </span>
                                          )}
                                        </span>
                                      </summary>
                                      <div className="stream-reasoning-content reasoning-detail-content">
                                        {seg.text}
                                      </div>
                                    </details>
                                  );
                                }
                                if (seg.type === 'tool') {
                                  const toolStatus = normalizeSegmentToolStatus(seg);
                                  const { toolHead, toolRest } = parseToolSegmentDisplay(seg, ontologyLabelMap);
                                  const toolCallId = String(seg?.meta?.tool_call_id ?? '').trim();
                                  const toolName = String(seg?.meta?.tool_name ?? '').trim();
                                  if (toolName === 'execute_script') {
                                    return renderMainScriptToolEvent(seg, `script-tool-${toolCallId || `${detailEntryKey}-${si}`}`, now, ontologyLabelMap);
                                  }
                                  if (toolName === 'web_search') {
                                    return renderMainWebSearchToolEvent(seg, `web-search-tool-${toolCallId || `${detailEntryKey}-${si}`}`, now, ontologyLabelMap);
                                  }
                                  const readQuery = (candidate) => String(
                                    candidate?.meta?.input?.query
                                    ?? candidate?.meta?.tool_input?.query
                                    ?? candidate?.meta?.tool_args?.query
                                    ?? candidate?.meta?.query
                                    ?? ''
                                  ).trim();
                                  const isDbSearchTool = toolName === 'business_data_search';
                                  const directQuery = readQuery(seg);
                                  const ontologyPanelKey = toolCallId
                                    ? `id:${toolCallId}`
                                    : (directQuery ? `query:${directQuery}` : '');

                                  if (isDbSearchTool && ontologyPanelKey && renderedOntologyPanelKeys.has(ontologyPanelKey)) {
                                    return null;
                                  }

                                  const peerSegs = (isDbSearchTool && ontologyPanelKey)
                                    ? processSegs.filter((candidate) => (
                                      candidate?.type === 'tool'
                                      && (
                                        toolCallId
                                          ? String(candidate?.meta?.tool_call_id ?? '').trim() === toolCallId
                                          : (
                                            !String(candidate?.meta?.tool_call_id ?? '').trim()
                                            && readQuery(candidate) === directQuery
                                          )
                                      )
                                    ))
                                    : [seg];
                                  if (isDbSearchTool && ontologyPanelKey) {
                                    renderedOntologyPanelKeys.add(ontologyPanelKey);
                                  }

                                  const siblingQuery = readQuery(peerSegs.find((candidate) => readQuery(candidate).length > 0));
                                  const query = directQuery || siblingQuery;
                                  const summaryTailFull = query || toolRest || '';
                                  const summaryTailRaw = query ? `正在检索: ${query}` : summaryTailFull;
                                  const summaryTail = summaryTailRaw.length > 36
                                    ? `${summaryTailRaw.slice(0, 36)}...`
                                    : summaryTailRaw;

                                  const elapsedMs = isDbSearchTool && ontologyPanelKey
                                    ? peerSegs.reduce((acc, candidate) => Math.max(acc, candidate?.meta?.elapsed_ms || 0), 0)
                                    : (seg?.meta?.elapsed_ms || 0);

                                  const intermediateContents = peerSegs
                                    .flatMap((candidate) => {
                                      const events = Array.isArray(candidate?.meta?.db_events)
                                        ? candidate.meta.db_events
                                        : [];
                                      return events
                                        .filter((evt) => evt?.type === 'intermediate')
                                        .map((evt) => String(evt?.content ?? '').trim())
                                        .filter((content) => content.length > 0);
                                    });
                                  const finalContents = peerSegs
                                    .flatMap((candidate) => {
                                      const events = Array.isArray(candidate?.meta?.db_events)
                                        ? candidate.meta.db_events
                                        : [];
                                      return events
                                        .filter((evt) => evt?.type === 'final')
                                        .map((evt) => String(evt?.content ?? '').trim())
                                        .filter((content) => content.length > 0);
                                    });
                                  const fallbackResult = String(
                                    peerSegs.find((candidate) => String(candidate?.meta?.output_preview ?? '').trim())?.meta?.output_preview ?? ''
                                  ).trim();
                                  const finalContent = finalContents.length > 0
                                    ? finalContents[finalContents.length - 1]
                                    : fallbackResult;
                                  if (isDbSearchTool && (query || intermediateContents.length > 0 || finalContent)) {
                                    return (
                                      <details key={`ontology-${ontologyPanelKey || `${detailEntryKey}-${si}`}`} className="stream-reasoning-block ontology-process-block">
                                        <summary className="stream-reasoning-header ontology-process-summary">
                                          <span className="stream-inline stream-inline-tools">
                                            <span className="stream-tool-head">{toolHead}</span>
                                            {summaryTail && (
                                              <span className="stream-tool-rest ontology-query-rest" title={summaryTailFull}>
                                                {summaryTail}
                                              </span>
                                            )}
                                            <span className="stream-fold-arrow">▶</span>
                                          </span>
                                          {toolStatus && (
                                            <span className={`stream-tool-status-icon status-${toolStatus}`}>
                                              {toolStatus === 'completed' && elapsedMs > 0 && (
                                                <span className="stream-tool-elapsed reasoning-detail-elapsed reasoning-detail-elapsed-right">
                                                  {elapsedMs < 1000 ? `${elapsedMs}ms` : `${(elapsedMs / 1000).toFixed(1)}s`}
                                                </span>
                                              )}
                                              {toolStatus === 'completed' ? <CircleCheck size={14} /> : <CircleX size={14} />}
                                            </span>
                                          )}
                                        </summary>
                                        <div className="stream-reasoning-content ontology-process-content reasoning-detail-content">
                                          {query && (
                                            <div className="db-search-query">
                                              <span className="db-search-query-label">正在检索:</span>
                                              <span className="db-search-query-value">{query}</span>
                                            </div>
                                          )}
                                          {intermediateContents.length > 0 && (
                                            <div className="db-search-events-timeline">
                                              {intermediateContents.map((content, evtIdx) => {
                                                const strippedContent = content.replace(/^步骤\s*\d+[:：]\s*/, '').trim();
                                                const mainText = strippedContent || content.trim();
                                                return (
                                                  <div key={`${ontologyPanelKey || `${detailEntryKey}-${si}`}-db-${evtIdx}`} className="db-search-event-item">
                                                    <div className="db-search-event-timeline-dot" />
                                                    <div className="db-search-event-card">
                                                      <span className="db-search-event-content">{mainText}</span>
                                                    </div>
                                                  </div>
                                                );
                                              })}
                                            </div>
                                          )}
                                          {finalContent && (
                                            <div className="db-search-result">
                                              <div className="db-search-result-label">查询结果:</div>
                                              <div className="db-search-result-value">{finalContent}</div>
                                            </div>
                                          )}
                                        </div>
                                      </details>
                                    );
                                  }
                                  return (
                                    <div key={`tool-${toolCallId || `${detailEntryKey}-${si}`}`} className="stream-tools-block">
                                      <div className="stream-tools-line">
                                        <div className="stream-inline stream-inline-tools">
                                          <span className="stream-tool-head">{toolHead}</span>
                                          {toolRest && <span className="stream-tool-rest">{toolRest}</span>}
                                        </div>
                                        {toolStatus && (
                                          <span className={`stream-tool-status-icon status-${toolStatus}`}>
                                            {toolStatus === 'completed' && elapsedMs > 0 && (
                                              <span className="stream-tool-elapsed reasoning-detail-elapsed reasoning-detail-elapsed-right">
                                                {elapsedMs < 1000 ? `${elapsedMs}ms` : `${(elapsedMs / 1000).toFixed(1)}s`}
                                              </span>
                                            )}
                                            {toolStatus === 'completed' ? <CircleCheck size={14} /> : <CircleX size={14} />}
                                          </span>
                                        )}
                                      </div>
                                    </div>
                                  );
                                }
                                if (seg.type === 'subagent_group') {
                                  return renderSubagentGroupSegment(seg, `subagent-group-${detailEntryKey}-${si}`, 0, ontologyLabelMap);
                                }
                                if (seg.type === 'subagent' || seg.type === 'state') {
                                  return (
                                    <div key={`state-${detailEntryKey}-${si}`} className="stream-state-line reasoning-detail-state-line">
                                      {seg.node && <span className="stream-state-node">{seg.node}</span>}
                                      <span className="stream-state-text">{seg.text || ''}</span>
                                    </div>
                                  );
                                }
                                return null;
                              }).filter(Boolean);

                              const emptyProcessHint = '该步骤没有可展示的过程细节（模型可能直接给出结果）。';
                              const hasIntermediateContent = (
                                entryIdx < orderedDetailEntries.length - 1
                                && contentSegs.some((seg) => (
                                  typeof seg?.text === 'string' && seg.text.trim().length > 0
                                ))
                              );

                              return (
                                <div key={detailEntryKey} className="reasoning-detail-part-wrap">
                                  <div
                                    className={`reasoning-detail-part${isActiveDetail ? ' reasoning-detail-part-active' : ''}`}
                                    data-group-index={globalIndex ?? undefined}
                                  >
                                    {(iterationMetric || toolsElapsedMs != null || toolCount > 0) && (
                                      <div
                                        className={`reasoning-iteration-summary${onSegmentClick && globalIndex != null ? ' reasoning-iteration-summary-clickable' : ''}`}
                                        onClick={onSegmentClick && globalIndex != null ? () => onSegmentClick({ turnIndex: turn.index, globalIndex }) : undefined}
                                        role={onSegmentClick && globalIndex != null ? 'button' : undefined}
                                        tabIndex={onSegmentClick && globalIndex != null ? 0 : undefined}
                                        onKeyDown={onSegmentClick && globalIndex != null ? (event) => {
                                          if (event.key === 'Enter' || event.key === ' ') {
                                            event.preventDefault();
                                            onSegmentClick({ turnIndex: turn.index, globalIndex });
                                          }
                                        } : undefined}
                                        title={onSegmentClick && globalIndex != null ? '查看对应结果流' : undefined}
                                      >
                                        <div className="reasoning-iteration-summary-main">
                                          <span className="reasoning-iteration-badge">第{displayIteration}轮</span>
                                          {iterationStatusLabel && (
                                            <span className="reasoning-iteration-status">{iterationStatusLabel}</span>
                                          )}
                                        </div>
                                        <div className="reasoning-iteration-metrics">
                                          {llmTimingDisplay && (
                                            <span className="reasoning-iteration-metric" title={llmTimingDisplay.title}>
                                              LLM {formatPreciseElapsed(llmTimingDisplay.elapsedMs)}
                                            </span>
                                          )}
                                          {totalElapsedMs != null && (
                                            <span className="reasoning-iteration-metric reasoning-iteration-metric-total">
                                              总计 {formatPreciseElapsed(totalElapsedMs)}
                                            </span>
                                          )}
                                          {toolCount > 0 && (
                                            <span className="reasoning-iteration-metric">
                                              {toolCount} 项调用
                                            </span>
                                          )}
                                        </div>
                                      </div>
                                    )}
                                    {renderedProcessSegs.length > 0 ? (
                                      renderedProcessSegs
                                    ) : (
                                      <div className="reasoning-detail-empty reasoning-detail-empty-inline">{emptyProcessHint}</div>
                                    )}
                                  </div>
                                  {hasIntermediateContent && (
                                    <div className="reasoning-detail-part-divider" aria-hidden="true" />
                                  )}
                                </div>
                              );
                            })
                          )}
                        </div>
                      </details>
                    );
                  })
                )}
              </div>
            </>
        </div>
      )}
    </div>
  );
}

export default TaskDAGSidebar;
