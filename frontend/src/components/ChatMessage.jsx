import { useRef, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Loader2, Sparkles, ChevronDown, Wrench, CircleCheck, CircleX, Copy, Check } from 'lucide-react';
import { getNodeDisplayName, getStatusDisplayText } from '../utils/nodeNames';
import { buildIndexedSegmentGroups } from '../utils/segmentGroups';
import { buildWebSearchProcessModel } from '../utils/webSearchDisplay';
import RunFeedback from './RunFeedback';

const HTML_BREAK_TAG_PATTERN = /<br\s*\/?>/gi;
const HTML_BREAK_ONLY_PATTERN = /^(?:\s*<br\s*\/?>\s*)+$/i;

function replaceHtmlBreakNodes(node) {
  if (!node || typeof node !== 'object' || !Array.isArray(node.children)) {
    return;
  }

  const nextChildren = [];

  node.children.forEach((child) => {
    if (
      child?.type === 'html'
      && typeof child.value === 'string'
      && HTML_BREAK_ONLY_PATTERN.test(child.value)
    ) {
      const matches = child.value.match(HTML_BREAK_TAG_PATTERN) || [];
      matches.forEach(() => {
        nextChildren.push({ type: 'break' });
      });
      return;
    }

    replaceHtmlBreakNodes(child);
    nextChildren.push(child);
  });

  node.children = nextChildren;
}

function remarkHtmlBreaks() {
  return (tree) => {
    replaceHtmlBreakNodes(tree);
  };
}

const CHAT_MARKDOWN_REMARK_PLUGINS = [remarkGfm, remarkHtmlBreaks];

const TOOL_NAME_LABELS = {
  ontology_context: '加载本体',
  skill: '技能',
  business_data_search: '业务数据查询',
  web_search: '联网搜索',
  execute_script: '脚本执行',
  spawn_sub_agent: '子任务分发',
  memory: '记忆',
};

const MESSAGE_SUCCESS_STATUSES = new Set(['completed', 'finished', 'success']);
const MESSAGE_ERROR_STATUSES = new Set(['aborted', 'cancelled', 'canceled', 'error', 'failed']);
const MESSAGE_AWAITING_STATUSES = new Set(['awaiting', 'awaiting_input']);

function isPlainObject(value) {
  return value != null && typeof value === 'object' && !Array.isArray(value);
}

function getToolParams(meta = {}) {
  if (isPlainObject(meta?.input)) return meta.input;
  if (isPlainObject(meta?.tool_args)) return meta.tool_args;
  return null;
}

function normalizeMessageOption(option) {
  if (isPlainObject(option)) {
    const label = String(option.label || '').trim();
    const description = String(option.description || '').trim();
    const hasExplicitValue = Object.prototype.hasOwnProperty.call(option, 'value');
    return {
      id: String(option.id || '').trim(),
      value: hasExplicitValue ? option.value : (label || description || JSON.stringify(option)),
      label: label || description || '选项',
      description,
      interactionId: String(option.interaction_id || '').trim(),
      type: String(option.type || '').trim(),
      action: option.action,
    };
  }

  const text = String(option ?? '').trim();
  return {
    value: text,
    label: text,
    description: '',
  };
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

function resolveSegmentElapsedMs(segment, anchorTs) {
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

function buildToolDisplay(segment) {
  const meta = segment?.meta || {};
  const rawToolName = typeof meta?.tool_name === 'string' ? meta.tool_name.trim() : '';
  const canonicalToolName = TOOL_NAME_LABELS[rawToolName] || rawToolName || '工具调用';
  const params = getToolParams(meta) || {};
  const eventMessage = typeof meta?.message === 'string' ? meta.message.trim() : '';
  const segmentText = typeof segment?.text === 'string' ? segment.text.trim() : '';
  const rawText = eventMessage || segmentText;

  if (rawToolName === 'ontology_context') {
    const resource = String(params?.resource || '').trim();
    const key = String(params?.key || '').trim();
    const resourceLabels = {
      root: '',
      operation: '逻辑',
      object: '实体',
      asset: '公共文件',
    };
    const ontologyName = String(params?.ontology_name || params?.ontology_label || params?.name || '').trim();
    const ontologyId = String(params?.ontology_id || '').trim();
    const toolRest = resource === 'root'
      ? (ontologyName || ontologyId || '本体')
      : [resourceLabels[resource] || resource, key].filter(Boolean).join(': ') || ontologyName || ontologyId;
    return {
      toolHead: canonicalToolName,
      toolRest: toolRest || (rawText.startsWith(canonicalToolName) ? rawText.slice(canonicalToolName.length).trim() : rawText),
    };
  }

  if (rawToolName === 'skill') {
    const resource = String(params?.resource || '').trim();
    const key = String(params?.key || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: [params?.skill_name, resource, key].filter(Boolean).join(' / ') || (rawText.startsWith(canonicalToolName) ? rawText.slice(canonicalToolName.length).trim() : rawText),
    };
  }

  if (rawToolName === 'business_data_search') {
    const query = String(params?.query || meta?.query || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: query || (rawText.startsWith(canonicalToolName) ? rawText.slice(canonicalToolName.length).trim() : rawText),
    };
  }

  if (rawToolName === 'web_search') {
    const query = String(params?.query || meta?.query || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: query || (rawText.startsWith(canonicalToolName) ? rawText.slice(canonicalToolName.length).trim() : rawText),
    };
  }

  if (rawToolName === 'execute_script') {
    const operationKey = String(params?.operation_key || '').trim();
    return {
      toolHead: canonicalToolName,
      toolRest: operationKey || (rawText.startsWith(canonicalToolName) ? rawText.slice(canonicalToolName.length).trim() : rawText),
    };
  }

  const normalized = rawText.replace(/^调用工具[:：]?\s*/u, '').trim();
  const spaceIdx = normalized.indexOf(' ');
  if (spaceIdx === -1) {
    return {
      toolHead: canonicalToolName,
      toolRest: normalized && normalized !== rawToolName ? normalized : '',
    };
  }

  return {
    toolHead: canonicalToolName,
    toolRest: normalized.slice(spaceIdx + 1).trim(),
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

function ChatMessage({ msg, isInTimeline = false, isFirst = false, isLast = false, isLastMessage = false, shouldAutoScroll = true, onOptionClick = null, segmentIndexOffset = 0, hasUserReplyAfter = false, turnIndex = null, activeReasoningSegment = null, onSegmentClick = null }) {
  const loadingRef = useRef(null);
  const detailsRef = useRef(null);
  const copyResetTimerRef = useRef(null);
  const [timerNowTs, setTimerNowTs] = useState(() => Date.now());
  const [copiedOutputIndex, setCopiedOutputIndex] = useState(null);

  useEffect(() => {
    if (!shouldAutoScroll || !isLastMessage) return;
    if (msg.status !== 'running' || !loadingRef.current) return;

    const timeoutId = setTimeout(() => {
      loadingRef.current?.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
        inline: 'nearest'
      });
    }, 600);

    return () => clearTimeout(timeoutId);
  }, [msg.status, msg.content, msg.reasoning, msg.tools, shouldAutoScroll, isLastMessage]);

  useEffect(() => {
    const startTsRaw = typeof msg?.startTs === 'string' ? Number(msg.startTs) : msg?.startTs;
    const startTs = Number.isFinite(startTsRaw) ? startTsRaw : null;
    if (startTs === null) return undefined;

    if (msg.status !== 'running' && msg.status !== 'streaming') return undefined;

    const timer = setInterval(() => setTimerNowTs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [msg.startTs, msg.endTs, msg.status]);

  useEffect(() => () => {
    if (copyResetTimerRef.current) {
      clearTimeout(copyResetTimerRef.current);
    }
  }, []);

  const handleTimelineNodeClick = (e) => {
    if (!isInTimeline || !detailsRef.current) return;

    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const clickY = e.clientY - rect.top;

    const nodeLeft = -54;
    const nodeTop = 11;
    const nodeWidth = 44;
    const nodeHeight = 44;

    if (clickX >= nodeLeft && clickX <= nodeLeft + nodeWidth &&
      clickY >= nodeTop && clickY <= nodeTop + nodeHeight) {
      e.preventDefault();
      e.stopPropagation();
      detailsRef.current.open = !detailsRef.current.open;
    }
  };

  const customH2 = ({ children, ...props }) => {
    const extractText = (node) => {
      if (typeof node === 'string') return node;
      if (typeof node === 'number') return String(node);
      if (Array.isArray(node)) return node.map(extractText).join('');
      if (node && typeof node === 'object' && node.props && node.props.children) {
        return extractText(node.props.children);
      }
      return '';
    };

    const textContent = extractText(children);
    const isConclusion = textContent.trim() === '综合结论';
    const className = isConclusion && isLastMessage ? 'h2-conclusion' : '';

    return <h2 {...props} className={className}>{children}</h2>;
  };

  const customP = ({ children, ...props }) => {
    return (
      <div className="message-content-wrapper">
        <p {...props}>{children}</p>
      </div>
    );
  };

  const markdownComponents = {
    h2: customH2,
    p: customP,
  };

  const renderMarkdown = (text) => (
    <ReactMarkdown remarkPlugins={CHAT_MARKDOWN_REMARK_PLUGINS} components={markdownComponents}>
      {text == null ? '' : String(text)}
    </ReactMarkdown>
  );

  const renderPlainUserText = (text) => (
    <div className="user-message-text">
      {text == null ? '' : String(text)}
    </div>
  );

  const renderNonAssistantContent = (text) => (
    msg.role === 'user' ? renderPlainUserText(text) : renderMarkdown(text)
  );

  const hasTools = Boolean(msg.tools && msg.tools.trim());
  const isAgentStep = msg.kind === 'agent_step';
  const hasStreamContent = Array.isArray(msg.streamSegments) && msg.streamSegments.length > 0;
  const isRunningStatus = msg.status === 'running' || msg.status === 'streaming';
  const isCompletedStatus = msg.status === 'finished' || msg.status === 'completed';
  const isErrorStatus = msg.status === 'error' || msg.status === 'failed';
  const isAwaitingStatus = MESSAGE_AWAITING_STATUSES.has(msg.status);
  const shouldShowStatusMeta = msg.node !== 'llm' && msg.stepLabel !== 'llm' && msg.nodeFirst !== 'llm';
  const nodeDisplayName = getNodeDisplayName(msg.stepLabel, msg.displayName);
  const isTurnStreamStep = msg.stepLabel === 'turn_stream';
  const progressHeaderStateClass = isRunningStatus
    ? 'state-running'
    : (isCompletedStatus ? 'state-completed' : (isErrorStatus ? 'state-error' : (isAwaitingStatus ? 'state-awaiting' : 'state-default')));
  const startTsRaw = typeof msg.startTs === 'string' ? Number(msg.startTs) : msg.startTs;
  const startTs = Number.isFinite(startTsRaw) ? startTsRaw : null;
  const endTsRaw = typeof msg.endTs === 'string' ? Number(msg.endTs) : msg.endTs;
  const endTs = Number.isFinite(endTsRaw) ? endTsRaw : null;
  const shouldShowTiming = Boolean(startTs) && (isRunningStatus || isCompletedStatus || isErrorStatus || isAwaitingStatus);
  const timerAnchorTs = isRunningStatus ? timerNowTs : (endTs || timerNowTs);
  const durationMs = startTs ? Math.max(0, timerAnchorTs - startTs) : 0;
  const showBubbleLoading = msg.status === 'running' || msg.status === 'streaming';
  const shouldShowRunFeedback = Boolean(msg.runId) && isCompletedStatus;
  const interaction = isPlainObject(msg.interaction) ? msg.interaction : null;
  const shouldShowMessageOptions = msg.options && msg.options.length > 0 && !hasUserReplyAfter && !interaction;
  const messageErrorText = typeof msg?.meta?.error === 'string' ? msg.meta.error.trim() : '';
  const shouldShowMessageError = Boolean(
    isErrorStatus
    && messageErrorText
    && !String(msg.content || '').includes(messageErrorText)
  );

  const renderMessageErrorDetail = () => (
    shouldShowMessageError ? (
      <div className="message-error-detail">
        <div className="message-error-detail-title">错误信息</div>
        <div className="message-error-detail-body">{messageErrorText}</div>
      </div>
    ) : null
  );

  const buildOptionMeta = (normalizedOption) => ({
    action: normalizedOption.action,
    interaction: normalizedOption.interactionId
      ? { id: normalizedOption.interactionId, type: normalizedOption.type || 'single_choice' }
      : null,
    optionId: normalizedOption.id || null,
    option: normalizedOption,
  });

  const formatDuration = (ms) => {
    const s = Math.round(Math.max(0, ms) / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
  };
  const startTimeLabel = startTs
    ? new Date(startTs).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    })
    : '--:--:--';

  const extractCopyTextFromContentSegs = (contentSegs = []) => {
    if (!Array.isArray(contentSegs) || contentSegs.length === 0) return '';
    return contentSegs
      .map((seg) => (typeof seg?.text === 'string' ? seg.text : ''))
      .join('\n')
      .trim();
  };

  const writeTextToClipboard = async (text) => {
    if (!text) return false;
    if (navigator?.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        // Fallback to execCommand path below when clipboard API fails.
      }
    }

    try {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '-9999px';
      textarea.style.left = '-9999px';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const success = document.execCommand('copy');
      document.body.removeChild(textarea);
      return success;
    } catch {
      return false;
    }
  };

  const handleCopyOutputBlock = async (event, text, globalIndex) => {
    event.preventDefault();
    event.stopPropagation();
    const copied = await writeTextToClipboard(text);
    if (!copied) return;
    setCopiedOutputIndex(globalIndex);
    if (copyResetTimerRef.current) {
      clearTimeout(copyResetTimerRef.current);
    }
    copyResetTimerRef.current = setTimeout(() => {
      setCopiedOutputIndex(null);
    }, 1500);
  };

  const renderBusinessDataSearchEvents = (segment, keyPrefix) => {
    const meta = segment?.meta || {};
    const toolName = typeof meta?.tool_name === 'string' ? meta.tool_name.trim() : '';
    if (toolName !== 'business_data_search') return null;

    const dbEvents = Array.isArray(meta?.db_events) ? meta.db_events : [];
    const query = String(meta?.input?.query ?? meta?.tool_args?.query ?? meta?.query ?? '').trim();
    const summaryQueryMaxLen = 36;
    const summaryQueryText = query
      ? `正在检索: ${query.length > summaryQueryMaxLen ? `${query.slice(0, summaryQueryMaxLen)}...` : query}`
      : '';
    const intermediateContents = dbEvents
      .filter((evt) => evt?.type === 'intermediate')
      .map((evt) => String(evt?.content ?? '').trim())
      .filter((content) => content.length > 0);
    const finalContents = dbEvents
      .filter((evt) => evt?.type === 'final')
      .map((evt) => String(evt?.content ?? '').trim())
      .filter((content) => content.length > 0);
    const fallbackResult = String(meta?.output_preview ?? '').trim();
    const finalContent = finalContents.length > 0
      ? finalContents[finalContents.length - 1]
      : fallbackResult;

    const normalizedMessageStatus = String(msg.status || '').toLowerCase();
    const normalizedToolStatus = String(segment?.toolStatus || segment?.state || meta?.status || '').toLowerCase();
    const segmentEndTsRaw = typeof segment?.endTs === 'string' ? Number(segment.endTs) : segment?.endTs;
    const segmentEndTs = Number.isFinite(segmentEndTsRaw) ? segmentEndTsRaw : null;
    const messageEndTsRaw = typeof msg?.endTs === 'string' ? Number(msg.endTs) : msg?.endTs;
    const messageEndTs = Number.isFinite(messageEndTsRaw) ? messageEndTsRaw : null;
    const isFailed = MESSAGE_ERROR_STATUSES.has(normalizedMessageStatus) || normalizedToolStatus === 'error';
    const isCompleted = finalContents.length > 0
      || MESSAGE_SUCCESS_STATUSES.has(normalizedMessageStatus)
      || (Boolean(segment?.finished) && !isFailed);
    const isTerminal = isCompleted || isFailed || Boolean(segment?.finished);

    const toolElapsedRaw = Number(meta?.elapsed_ms);
    const toolElapsedMs = Number.isFinite(toolElapsedRaw) ? Math.max(0, toolElapsedRaw) : null;
    const eventTsList = dbEvents
      .map((evt) => Number(evt?.ts))
      .filter((value) => Number.isFinite(value));
    const firstEventTs = eventTsList.length > 0 ? eventTsList[0] : null;
    const lastEventTs = eventTsList.length > 0 ? eventTsList[eventTsList.length - 1] : null;
    const fallbackElapsedMs = firstEventTs == null
      ? null
      : Math.max(0, (
        isTerminal
          ? (segmentEndTs ?? messageEndTs ?? lastEventTs ?? firstEventTs)
          : timerNowTs
      ) - firstEventTs);
    const elapsedMs = toolElapsedMs ?? fallbackElapsedMs;
    const elapsedLabel = elapsedMs == null ? null : formatCompactElapsed(elapsedMs);

    if (!query && intermediateContents.length === 0 && !finalContent) return null;

    return (
      <div key={keyPrefix} className="db-search-events-panel">
        <details className="db-search-events-collapsible">
          <summary className="db-search-events-summary">
            <span className="db-search-agent-badge">本体探索智能体</span>
            {summaryQueryText && (
              <span className="db-search-summary-query" title={query}>
                {summaryQueryText}
              </span>
            )}
            <span className="stream-fold-arrow">▶</span>
            <span className={`stream-tool-status ${isCompleted ? 'status-completed' : isFailed ? 'status-error' : 'status-running'}`}>
              {elapsedLabel && (
                <span className="db-search-elapsed">{elapsedLabel}</span>
              )}
              {isCompleted ? (
                <CircleCheck size={20} />
              ) : isFailed ? (
                <CircleX size={20} />
              ) : (
                <Loader2 className="spin" size={20} />
              )}
            </span>
          </summary>
          <div className="db-search-events-content-wrapper">
            {query && (
              <div className="db-search-query">
                <span className="db-search-query-label">正在检索:</span>
                <span className="db-search-query-value">{query}</span>
              </div>
            )}
            {intermediateContents.length > 0 && (
              <div className="db-search-events-timeline">
                {intermediateContents.map((content, idx) => {
                  // Remove leading "步骤 X:" label and only show step content.
                  const strippedContent = content.replace(/^步骤\s*\d+[:：]\s*/, '').trim();
                  const mainText = strippedContent || content.trim();

                  // If it's the last item and the tool is not completed, it might still be generating
                  const isLastAndRunning = idx === intermediateContents.length - 1 && !isTerminal;

                  return (
                    <div key={`${keyPrefix}-evt-${idx}`} className={`db-search-event-item ${isLastAndRunning ? 'event-pulsing' : ''}`}>
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
                <div className="db-search-result-value">{renderMarkdown(finalContent)}</div>
              </div>
            )}
          </div>
        </details>
      </div>
    );
  };

  const renderWebSearchEvents = (segment, keyPrefix) => {
    const model = buildWebSearchProcessModel({
      segment,
      messageStatus: msg.status,
      nowTs: timerNowTs,
    });
    if (!model) return null;

    const summaryQueryMaxLen = 36;
    const summaryQueryText = model.query
      ? `正在搜索: ${model.query.length > summaryQueryMaxLen ? `${model.query.slice(0, summaryQueryMaxLen)}...` : model.query}`
      : '';
    const elapsedLabel = model.elapsedMs == null ? null : formatCompactElapsed(model.elapsedMs);
    const statusClass = model.isCompleted ? 'status-completed' : model.isFailed ? 'status-error' : 'status-running';
    const resultLabel = model.resultCount > 0 ? `返回 ${model.resultCount} 条结果` : '';
    const responseTimeLabel = model.responseTime == null ? '' : `API ${Number(model.responseTime).toFixed(2)}s`;

    return (
      <div key={keyPrefix} className="db-search-events-panel web-search-events-panel">
        <details className="db-search-events-collapsible">
          <summary className="db-search-events-summary">
            <span className="db-search-agent-badge web-search-agent-badge">联网搜索</span>
            {summaryQueryText && (
              <span className="db-search-summary-query" title={model.query}>
                {summaryQueryText}
              </span>
            )}
            <span className="stream-fold-arrow">▶</span>
            <span className={`stream-tool-status ${statusClass}`}>
              {elapsedLabel && (
                <span className="db-search-elapsed">{elapsedLabel}</span>
              )}
              {model.isCompleted ? (
                <CircleCheck size={20} />
              ) : model.isFailed ? (
                <CircleX size={20} />
              ) : (
                <Loader2 className="spin" size={20} />
              )}
            </span>
          </summary>
          <div className="db-search-events-content-wrapper web-search-content-wrapper">
            {model.query && (
              <div className="db-search-query">
                <span className="db-search-query-label">搜索词:</span>
                <span className="db-search-query-value">{model.query}</span>
              </div>
            )}
            {model.timeline.length > 0 && (
              <div className="db-search-events-timeline web-search-events-timeline">
                {model.timeline.map((event, idx) => {
                  const isLastAndRunning = idx === model.timeline.length - 1 && !model.isTerminal;
                  return (
                    <div key={`${keyPrefix}-evt-${idx}`} className={`db-search-event-item ${isLastAndRunning ? 'event-pulsing' : ''}`}>
                      <div className="db-search-event-timeline-dot web-search-event-timeline-dot" />
                      <div className="db-search-event-card">
                        <span className="db-search-event-content">{event.content}</span>
                      </div>
                    </div>
                  );
                })}
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
                  <div key={`${keyPrefix}-source-${source.rank}-${source.url || source.title}`} className="web-search-source-item">
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
                    {source.snippet && (
                      <div className="web-search-source-snippet">{source.snippet}</div>
                    )}
                  </div>
                ))}
              </div>
            )}
            {model.errorText && (
              <div className="subagent-tool-call-error">{model.errorText}</div>
            )}
          </div>
        </details>
      </div>
    );
  };

  if (msg.role === 'assistant') {
    const status = msg.status || (msg.kind === 'agent_step' ? 'completed' : null);

    return (
      <div
        className={`bubble ${msg.role} ${isInTimeline ? 'timeline-item' : ''} ${isFirst ? 'timeline-first' : ''} ${isLast ? 'timeline-last' : ''}`}
        data-status={status}
        onClick={isInTimeline ? handleTimelineNodeClick : undefined}
      >
        <details ref={detailsRef} className="bubble-collapsible" open>
          <summary className="bubble-header">
            {msg.kind === 'agent_step' ? (
              <>
                <div className="progress-top">
                  <div className={`progress-top-shell ${isTurnStreamStep ? `turn-stream ${progressHeaderStateClass}` : ''}`}>
                    <div className="progress-top-left">
                      <span className="pill">{nodeDisplayName}</span>
                    </div>
                    <div className="progress-top-right">
                      {shouldShowTiming && (
                        <span className="meta-ts">
                          <span className="meta-chip-label">开始</span>
                          {startTimeLabel}
                        </span>
                      )}
                      {shouldShowTiming && (
                        <span className="meta-duration">
                          <span className="meta-chip-label">耗时</span>
                          {formatDuration(durationMs)}
                        </span>
                      )}
                      {shouldShowStatusMeta && (
                        <span className={`meta ${isRunningStatus ? 'meta-running' : ''} ${isCompletedStatus ? 'meta-completed' : ''} ${isErrorStatus ? 'meta-error' : ''} ${isAwaitingStatus ? 'meta-awaiting' : ''}`}>
                          {isCompletedStatus && <CircleCheck size={14} className="meta-status-icon" />}
                          {isErrorStatus && <CircleX size={14} className="meta-status-icon" />}
                          {isRunningStatus && <Loader2 className="spin" size={14} />}
                          <span className="meta-status-text">{getStatusDisplayText(msg.status)}</span>
                        </span>
                      )}
                      <span className="progress-chevron-wrap">
                        <ChevronDown size={16} className="bubble-chevron" />
                      </span>
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <ChevronDown size={16} className="bubble-chevron" />
            )}
          </summary>
          <div className="bubble-body">
            <div className="bubble-content-wrapper">
              {msg.kind === 'agent_step' ? (
                <>
                  {msg.streamSegments && msg.streamSegments.length > 0 ? (
                    <div className="stream-flow">
                      {(() => {
                        const renderContentSeg = (segment, key, renderOptions = {}) => {
                          const inlineClassName = typeof renderOptions.inlineClassName === 'string'
                            ? renderOptions.inlineClassName.trim()
                            : '';
                          return (
                            <div
                              key={key}
                              className={`stream-inline stream-inline-content${inlineClassName ? ` ${inlineClassName}` : ''}`}
                            >
                              {renderMarkdown(segment.text)}
                            </div>
                          );
                        };

                        const renderSubagentToolEvent = (evt, key) => {
                          const toolName = evt.toolName || 'tool';
                          const label = TOOL_NAME_LABELS[toolName] || toolName;
                          const finished = Boolean(evt.finished);
                          const errored = finished && evt.status === 'error';
                          const params = evt.input || {};
                          let subtitle = '';
                          if (toolName === 'ontology_context') {
                            const resource = String(params?.resource || '').trim();
                            const key = String(params?.key || '').trim();
                            const resourceLabels = { root: '', operation: '逻辑', object: '实体', asset: '公共文件' };
                            const ontologyName = String(params?.ontology_name || params?.ontology_label || params?.name || '').trim();
                            const ontologyId = String(params?.ontology_id || '').trim();
                            subtitle = resource === 'root'
                              ? (ontologyName || ontologyId || '本体')
                              : [resourceLabels[resource] || resource, key].filter(Boolean).join(': ') || ontologyName || ontologyId;
                          }
                          else if (toolName === 'skill') subtitle = [params?.skill_name, params?.resource, params?.key].filter(Boolean).join(' / ');
                          else if (toolName === 'business_data_search') subtitle = String(params?.query || '').trim();
                          else if (toolName === 'web_search') subtitle = String(params?.query || '').trim();
                          else if (toolName === 'execute_script') subtitle = String(params?.operation_key || '').trim();
                          else if (toolName === 'spawn_sub_agent') subtitle = '嵌套子任务';
                          const progressList = Array.isArray(evt.progress) ? evt.progress : [];
                          const hasDetail = progressList.length > 0 || evt.output || evt.error;
                          return (
                            <details key={key} className={`subagent-tool-call${errored ? ' is-error' : ''}${finished && !errored ? ' is-done' : ''}`} open={!finished}>
                              <summary className="subagent-tool-call-header">
                                <span className="subagent-tool-call-icon">
                                  {finished && !errored && <CircleCheck size={14} />}
                                  {errored && <CircleX size={14} />}
                                  {!finished && <Loader2 className="spin" size={14} />}
                                </span>
                                <span className="subagent-tool-call-name">{label}</span>
                                {subtitle && <span className="subagent-tool-call-arg">{subtitle}</span>}
                                {hasDetail && <ChevronDown size={12} className="subagent-tool-call-chevron" />}
                              </summary>
                              {hasDetail && (
                                <div className="subagent-tool-call-body">
                                  {progressList.map((p, pi) => (
                                    <div key={`${key}-progress-${pi}`} className="subagent-tool-call-progress">
                                      {p.web?.content || p.ontology?.content || p.text || ''}
                                    </div>
                                  ))}
                                  {evt.output && (
                                    <div className="subagent-tool-call-output">{evt.output}</div>
                                  )}
                                  {evt.error && (
                                    <div className="subagent-tool-call-error">{evt.error}</div>
                                  )}
                                </div>
                              )}
                            </details>
                          );
                        };

                        const renderSubagentChildEvent = (evt, key) => {
                          if (evt.kind === 'tool') return renderSubagentToolEvent(evt, key);
                          if (evt.kind === 'token') {
                            if (evt.channel === 'reasoning') {
                              return (
                                <details key={key} className="subagent-card-reasoning">
                                  <summary className="subagent-card-reasoning-header">
                                    <Sparkles size={12} />
                                    <span>思考</span>
                                    <ChevronDown size={12} />
                                  </summary>
                                  <div className="subagent-card-reasoning-body">{evt.text}</div>
                                </details>
                              );
                            }
                            return (
                              <div key={key} className="subagent-card-content">
                                {renderMarkdown(evt.text)}
                              </div>
                            );
                          }
                          return null;
                        };

                        const renderSubagentChild = (child, key, level = 0) => {
                          const status = child.status || 'running';
                          const isErr = status === 'error';
                          const isDone = status === 'completed';
                          const taskTitle = getSubagentTaskTitle(child);
                          const taskBody = getSubagentTaskBody(child);
                          const childEvents = Array.isArray(child.events) ? child.events : [];
                          const childGroups = getSubagentChildGroups(child);
                          const shouldOpen = level < 2 || status === 'running' || status === 'error';
                          return (
                            <details key={key} className={`subagent-card subagent-card-level-${Math.min(level, 4)}`} open={shouldOpen}>
                              <summary className="subagent-card-header">
                                <span className="subagent-card-status">
                                  {isDone && <CircleCheck size={14} />}
                                  {isErr && <CircleX size={14} />}
                                  {!isDone && !isErr && <Loader2 className="spin" size={14} />}
                                </span>
                                <span className="subagent-card-title">{taskTitle}</span>
                                <ChevronDown size={14} className="subagent-card-chevron" />
                              </summary>
                              <div className="subagent-card-body">
                                {taskBody && (
                                  <div className="subagent-card-task-body">
                                    {renderMarkdown(taskBody)}
                                  </div>
                                )}
                                {childEvents.length > 0 && (
                                  <div className="subagent-card-events">
                                    {childEvents.map((evt, ei) => renderSubagentChildEvent(evt, `${key}-event-${ei}`))}
                                  </div>
                                )}
                                {childGroups.length > 0 && (
                                  <div className="subagent-nested-groups">
                                    {childGroups.map((group, groupIndex) => renderSubagentGroup(
                                      group,
                                      `${key}-nested-${group.groupId || groupIndex}`,
                                      level + 1,
                                    ))}
                                  </div>
                                )}
                                {child.error && (
                                  <div className="subagent-card-error">{child.error}</div>
                                )}
                              </div>
                            </details>
                          );
                        };

                        const renderSubagentGroup = (segment, key, level = 0) => {
                          const childrenList = getOrderedSubagentChildren(segment);
                          const taskCount = segment.taskCount || childrenList.length;
                          const groupStatus = segment.status || (segment.finished ? 'completed' : 'running');
                          return (
                            <div key={key} className={`subagent-group${level > 0 ? ' subagent-group-nested' : ''}`}>
                              <div className="subagent-group-header">
                                <span className="subagent-group-title">
                                  {groupStatus === 'completed' && <CircleCheck size={16} />}
                                  {groupStatus === 'error' && <CircleX size={16} />}
                                  {groupStatus === 'running' && <Loader2 className="spin" size={16} />}
                                  <span>{level > 0 ? `Nested ${taskCount} subtasks` : `Executing ${taskCount} subtasks in parallel`}</span>
                                </span>
                              </div>
                              <div className="subagent-group-body">
                                {childrenList.map((child) => renderSubagentChild(
                                  child,
                                  `${key}-${child.subagentId || child.taskId}`,
                                  level,
                                ))}
                              </div>
                            </div>
                          );
                        };

                        const renderLiveSeg = (segment, key) => {
                          if (segment.type === 'reasoning') {
                            const isReasoningFinished = Boolean(segment?.finished);
                            const reasoningSummary = typeof segment?.meta?.reasoning_summary === 'string'
                              ? segment.meta.reasoning_summary.trim()
                              : '';
                            const reasoningPreviewSource = reasoningSummary || (
                              typeof segment?.text === 'string' ? segment.text : ''
                            );
                            const reasoningPreview = reasoningPreviewSource.length > 50
                              ? `${reasoningPreviewSource.slice(0, 50)}...`
                              : reasoningPreviewSource;
                            return (
                              <details key={key} className="stream-reasoning-block stream-live-reasoning-fold">
                                <summary className="stream-reasoning-header">
                                  {!reasoningPreview && !isReasoningFinished && <span className="stream-reasoning-label">{'思考中'}</span>}
                                  {reasoningPreview && (
                                    <span className="stream-reasoning-preview">
                                      {reasoningPreview}
                                    </span>
                                  )}
                                  <span className="stream-fold-arrow">▶</span>
                                  <span className={`stream-tool-status ${isReasoningFinished ? 'status-completed' : 'status-running'}`}>
                                    {isReasoningFinished ? <CircleCheck size={20} /> : <Loader2 className="spin" size={20} />}
                                  </span>
                                </summary>
                                <div className="stream-reasoning-content" style={{ whiteSpace: 'pre-wrap' }}>
                                  {segment.text}
                                </div>
                              </details>
                            );
                          }
                          if (segment.type === 'tool') {
                            const { toolHead, toolRest } = buildToolDisplay(segment);
                            const toolState = typeof segment?.state === 'string' ? segment.state.toLowerCase() : '';
                            const toolStatus = typeof segment?.toolStatus === 'string' ? segment.toolStatus.toLowerCase() : '';
                            const isToolErrored = toolStatus === 'error' || toolState === 'error' || toolState === 'failed';
                            const isToolCompleted = !isToolErrored && (
                              Boolean(segment?.finished)
                              || ['completed', 'complete', 'done', 'success', 'succeeded', 'ok'].includes(toolState)
                              || toolStatus === 'completed'
                            );

                            // Check if this tool has business data search events to prevent duplicate text
                            const meta = segment?.meta || {};
                            const toolName = typeof meta?.tool_name === 'string' ? meta.tool_name.trim() : '';
                            const isOntologyTool = toolName === 'business_data_search';
                            const webSearchPanel = renderWebSearchEvents(segment, `${key}-web-events`);
                            const elapsedMs = resolveSegmentElapsedMs(segment, timerNowTs);
                            const elapsedLabel = elapsedMs == null ? '' : formatCompactElapsed(elapsedMs);

                            return (
                              <div key={key} className="stream-live-tool-wrap">
                                {!isOntologyTool && !webSearchPanel && (
                                  <div className="stream-live-tool">
                                    <span className={`stream-tool-head${isToolCompleted ? ' stream-tool-head-completed' : ''}`}>{toolHead}</span>
                                    {toolRest && <span className="stream-tool-rest"> {toolRest}</span>}
                                    {(isToolCompleted || isToolErrored || (!isToolCompleted && !isToolErrored && !segment?.finished)) && (
                                      <span className={`stream-tool-status ${isToolCompleted ? 'status-completed' : isToolErrored ? 'status-error' : 'status-running'}`}>
                                        {elapsedLabel && <span className="stream-tool-elapsed">{elapsedLabel}</span>}
                                        {isToolCompleted && <CircleCheck size={20} />}
                                        {isToolErrored && <CircleX size={20} />}
                                        {!isToolCompleted && !isToolErrored && <Loader2 className="spin" size={20} />}
                                      </span>
                                    )}
                                  </div>
                                )}
                                {renderBusinessDataSearchEvents(segment, `${key}-db-events`)}
                                {webSearchPanel}
                              </div>
                            );
                          }
                          if (segment.type === 'subagent' || segment.type === 'state') {
                            return (
                              <div key={key} className="stream-live-reasoning">
                                {segment.text || ''}
                              </div>
                            );
                          }
                          if (segment.type === 'subagent_group') {
                            return renderSubagentGroup(segment, key);
                          }
                          return null;
                        };

                        const { completedGroups, liveProcessSegs } = buildIndexedSegmentGroups(msg.streamSegments);

                        return (
                          <>
                            {completedGroups.map(({ index, contentSegs }) => {
                              const globalIndex = index + segmentIndexOffset;
                              const firstTextContentSegIndex = contentSegs.findIndex((seg) => (
                                typeof seg?.text === 'string' && seg.text.trim().length > 0
                              ));
                              const copyText = extractCopyTextFromContentSegs(contentSegs);
                              const hasCopyText = copyText.length > 0;
                              const isCopied = copiedOutputIndex === globalIndex;
                              const isActiveOutput = (
                                activeReasoningSegment?.turnIndex === turnIndex
                                && activeReasoningSegment?.globalIndex === globalIndex
                              );
                              return (
                                <div
                                  key={index}
                                  className={`content-output-block${onSegmentClick && turnIndex != null ? ' content-output-block-clickable' : ''}${hasCopyText ? ' content-output-block-with-copy' : ''}${isActiveOutput ? ' content-output-block-active' : ''}`}
                                  onClick={onSegmentClick && turnIndex != null ? () => onSegmentClick({ turnIndex, globalIndex }) : undefined}
                                  data-turn-index={turnIndex ?? undefined}
                                  data-output-group-index={globalIndex}
                                >
                                  {hasCopyText && (
                                    <button
                                      type="button"
                                      className={`content-output-copy-btn${isCopied ? ' is-copied' : ''}`}
                                      onClick={(event) => handleCopyOutputBlock(event, copyText, globalIndex)}
                                      aria-label="复制此内容"
                                      title={isCopied ? '已复制' : '复制'}
                                    >
                                      {isCopied ? <Check size={20} /> : <Copy size={20} />}
                                      <span>{isCopied ? '已复制' : '复制'}</span>
                                    </button>
                                  )}
                                  <div className="content-output-body">
                                    {contentSegs.map((seg, si) => renderContentSeg(seg, `${index}-${si}`, {
                                      inlineClassName: si === firstTextContentSegIndex ? 'stream-inline-content-first-line' : '',
                                    }))}
                                  </div>
                                </div>
                              );
                            })}
                            {liveProcessSegs.length > 0 && (
                              <div className="stream-live-section">
                                {liveProcessSegs.map((seg, si) => renderLiveSeg(seg, `live-${si}`))}
                              </div>
                            )}
                          </>
                        );
                      })()}
                    </div>
                  ) : (
                    <>
                      {msg.reasoning && (
                        <details className="reasoning-block" open>
                          <summary className="reasoning-header">
                            <Sparkles size={12} /> Reasoning
                          </summary>
                          <div className="reasoning-content">
                            {renderMarkdown(msg.reasoning)}
                          </div>
                        </details>
                      )}
                      {hasTools && (
                        <details className="tools-block" open>
                          <summary className="tools-header">
                            <Wrench size={12} /> 工具使用记录
                          </summary>
                          <div className="tools-content">{renderMarkdown(msg.tools)}</div>
                        </details>
                      )}
                      {renderMarkdown(msg.content)}
                      {renderMessageErrorDetail()}
                    </>
                  )}
                  {isAgentStep && hasStreamContent && renderMessageErrorDetail()}
                  {shouldShowMessageOptions && (
                    <div className="chat-options">
                      {msg.options.map((option, idx) => {
                        const normalizedOption = normalizeMessageOption(option);
                        return (
                          <button
                            key={idx}
                            className="chat-option-btn"
                            title={normalizedOption.description || undefined}
                            onClick={() => onOptionClick && onOptionClick(
                              normalizedOption.value,
                              buildOptionMeta(normalizedOption),
                            )}
                          >
                            {normalizedOption.label}
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {showBubbleLoading && (
                    <div ref={loadingRef} className="bubble-loading-indicator">
                      <Loader2 className="spin" size={16} />
                      <span>正在处理中...</span>
                    </div>
                  )}
                </>
              ) : (
                <>
                  {msg.reasoning && (
                    <details className="reasoning-block" open>
                      <summary className="reasoning-header">
                        <Sparkles size={12} /> Reasoning
                      </summary>
                      <div className="reasoning-content">
                        {renderMarkdown(msg.reasoning)}
                      </div>
                    </details>
                  )}
                  {msg.role === 'assistant' && msg.isLoading && !msg.content && (
                    <div className="loading-indicator">
                      <Loader2 className="spin" size={16} />
                      <span>思考中...</span>
                    </div>
                  )}
                  {renderMarkdown(msg.content)}
                  {renderMessageErrorDetail()}
                  {/* Render Options Buttons if present */}
                  {shouldShowMessageOptions && (
                    <div className="chat-options">
                      {msg.options.map((option, idx) => {
                        const normalizedOption = normalizeMessageOption(option);
                        return (
                          <button
                            key={idx}
                            className="chat-option-btn"
                            title={normalizedOption.description || undefined}
                            onClick={() => onOptionClick && onOptionClick(
                              normalizedOption.value,
                              buildOptionMeta(normalizedOption),
                            )}
                          >
                            {normalizedOption.label}
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {showBubbleLoading && (
                    <div ref={loadingRef} className="bubble-loading-indicator">
                      <Loader2 className="spin" size={16} />
                      <span>正在处理中...</span>
                    </div>
                  )}
                </>
              )}
              {shouldShowRunFeedback && (
                <RunFeedback runId={msg.runId} status={msg.status} />
              )}
            </div>
          </div>
        </details>
      </div>
    );
  }

  return (
    <div className={`bubble ${msg.role}`}>
      <div className="bubble-body">
        {msg.kind === 'agent_step' ? (
          <>
            <div className="progress-top progress-top-margin">
              <span className="pill">{getNodeDisplayName(msg.stepLabel, msg.displayName)}</span>
              {msg.node !== 'llm' && msg.stepLabel !== 'llm' && msg.nodeFirst !== 'llm' && (
                <span className={`meta ${isRunningStatus ? 'meta-running' : ''} ${isCompletedStatus ? 'meta-completed' : ''} ${isErrorStatus ? 'meta-error' : ''}`}>
                  {getStatusDisplayText(msg.status)}
                  {isRunningStatus && (
                    <Loader2 className="spin loader-margin" size={14} />
                  )}
                </span>
              )}
            </div>
            {msg.reasoning && (
              <details className="reasoning-block" open>
                <summary className="reasoning-header">
                  <Sparkles size={12} /> Reasoning
                </summary>
                <div className="reasoning-content">
                  {renderMarkdown(msg.reasoning)}
                </div>
              </details>
            )}
            {hasTools && (
              <details className="tools-block" open>
                <summary className="tools-header">
                  <Wrench size={12} /> Tools
                </summary>
                <div className="tools-content">{renderMarkdown(msg.tools)}</div>
              </details>
            )}
            {renderNonAssistantContent(msg.content)}
            {renderMessageErrorDetail()}
            {showBubbleLoading && (
              <div ref={loadingRef} className="bubble-loading-indicator">
                <Loader2 className="spin" size={16} />
                <span>正在处理中...</span>
              </div>
            )}
          </>
        ) : (
          <>
            {msg.reasoning && (
              <details className="reasoning-block" open>
                <summary className="reasoning-header">
                  <Sparkles size={12} /> Reasoning
                </summary>
                <div className="reasoning-content">
                  {renderMarkdown(msg.reasoning)}
                </div>
              </details>
            )}
            {msg.role === 'assistant' && msg.isLoading && !msg.content && (
              <div className="loading-indicator">
                <Loader2 className="spin" size={16} />
                <span>思考中...</span>
              </div>
            )}
            {renderNonAssistantContent(msg.content)}
            {renderMessageErrorDetail()}
            {showBubbleLoading && (
              <div ref={loadingRef} className="bubble-loading-indicator">
                <Loader2 className="spin" size={16} />
                <span>正在处理中...</span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default ChatMessage;
