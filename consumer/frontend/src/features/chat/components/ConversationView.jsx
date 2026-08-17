import { useEffect, useState } from 'react';
import {
  AlertCircle, BrainCircuit, Check, Circle, Clock3, Database, FileText, GitBranch,
  Globe2, Loader2, Search, Wrench,
} from 'lucide-react';

import Markdown from '../../../shared/ui/Markdown';
import { formatDurationMs, formatTime } from '../lib/chatPresentation';
import { resolveRunProgressView } from '../lib/streamLifecycle';
import { CopyButton, RunFeedback } from './RunActions';
import ToolOutput from './ToolOutput';

const safeJson = (value) => {
  if (value == null || value === '') return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
};

const statusLabel = (status) => ({
  queued: '排队中',
  running: '执行问答任务中',
  finished: '已完成',
  error: '出错',
  cancelled: '已停止',
  awaiting: '等待输入',
}[status] || status || '');

function BlockMeta({ status, durationMs }) {
  const duration = formatDurationMs(durationMs);
  return (
    <span className={`conversation-block-meta status-${status || 'idle'}`}>
      {status === 'running' ? <Loader2 className="spin" size={11} /> : status === 'error' ? <AlertCircle size={11} /> : status === 'cancelled' ? <Circle size={11} /> : <Check size={11} />}
      {statusLabel(status)}{duration ? ` · ${duration}` : ''}
    </span>
  );
}

export default function ConversationView({ model, sessionId = '', runState = null }) {
  return (
    <div className="conversation-view" role="log" aria-live="off" aria-relevant="additions">
      {(model?.messages || []).map((message) => (
        message.role === 'user'
          ? <UserMessage key={message.key} message={message} />
          : message.role === 'system'
            ? <SystemMessage key={message.key} message={message} />
            : <AssistantTurn key={message.key} turn={message} sessionId={sessionId}
                runState={message.isLoading ? runState : null} />
      ))}
    </div>
  );
}

export function UserMessage({ message }) {
  return (
    <article className="msg u" data-message-key={message.key}>
      <div className="bubwrap">
        <div className="bub">{message.content}</div>
        {message.startedAt != null && <div className="time">{formatTime(message.startedAt)}</div>}
      </div>
    </article>
  );
}

export function SystemMessage({ message }) {
  return (
    <div className="conversation-system-message" role="status" data-message-key={message.key}>
      <span>{message.content}</span>
    </div>
  );
}

export function AssistantTurn({ turn, sessionId = '', runState = null }) {
  const answerText = (turn.blocks || [])
    .filter((block) => block.kind === 'content')
    .map((block) => block.text)
    .join('\n')
    .trim() || turn.content;
  const hasVisibleBlock = (turn.blocks || []).some((block) => block.kind !== 'status');
  const hasLiveProgress = Boolean(turn.isLoading && runState?.isLoading);

  return (
    <article className="msg a assistant-turn" data-run-id={turn.runId || undefined} data-message-key={turn.key}>
      <div className="av">知</div>
      <div className="bubwrap">
        <div className={`bub assistant-turn-body status-${turn.status}`}>
          {(turn.blocks || []).map((block) => (
            <ConversationBlock key={block.key} block={block} turn={turn} answerText={answerText}
              sessionId={sessionId} runState={runState} />
          ))}
          {turn.isLoading && !hasVisibleBlock && !hasLiveProgress && (
            <span className="assistant-thinking"><span className="tdots"><span /><span /><span /></span>分析问题中…</span>
          )}
        </div>
        {turn.startedAt != null && <div className="time">{formatTime(turn.startedAt)}</div>}
      </div>
    </article>
  );
}

function ConversationBlock({ block, turn, answerText, sessionId, runState }) {
  if (block.kind === 'content') return <AgentContent block={block} />;
  if (block.kind === 'reasoning') return <ReasoningBlock block={block} />;
  if (block.kind === 'business_data') return <BusinessDataResult block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'web_search') return <WebSearchSources block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'tool') return <ToolCallBlock block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'artifacts') return <ArtifactBlock block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'subagent') return <SubagentGroup block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'status' || block.kind === 'awaiting') {
    return <RunStatusBlock block={block} turn={turn} answerText={answerText} runState={runState} />;
  }
  return block.text ? <div className="conversation-state-block"><Circle size={11} />{block.text}</div> : null;
}

export function ArtifactBlock({ block, sessionId = '', runId = '' }) {
  const artifacts = Array.isArray(block.artifacts) ? block.artifacts : [];
  if (artifacts.length === 0) return null;
  return (
    <details className="conversation-process tool-process status-finished" open data-block-key={block.key}>
      <summary><FileText size={14} /><span>执行制品（{artifacts.length}）</span><BlockMeta status="finished" /></summary>
      <div className="conversation-process-body">
        {artifacts.map((artifact) => (
          <section className="tool-input" key={artifact.key}>
            <strong>{artifact.name}</strong>
            {(artifact.mediaType || artifact.size != null) && (
              <div>{[artifact.mediaType, artifact.size != null ? `${artifact.size} bytes` : ''].filter(Boolean).join(' · ')}</div>
            )}
            <ToolOutput
              meta={{
                output_ref: artifact.outputRef,
                output_size: artifact.size,
                sha256: artifact.sha256,
              }}
              sessionId={sessionId}
              runId={runId}
              rawOutput
            />
          </section>
        ))}
      </div>
    </details>
  );
}

export function AgentContent({ block }) {
  if (!block.text?.trim()) return null;
  return <div className="agent-content" data-block-key={block.key}><Markdown>{block.text}</Markdown></div>;
}

export function ReasoningBlock({ block }) {
  const summary = block.summary || '模型思考';
  return (
    <details className="conversation-process reasoning-process" open={block.status === 'running'} data-block-key={block.key}>
      <summary><BrainCircuit size={14} /><span>{summary}</span><BlockMeta status={block.status} durationMs={block.durationMs} /></summary>
      {block.text && <div className="conversation-process-body"><Markdown>{block.text}</Markdown></div>}
    </details>
  );
}

export function ToolCallBlock({ block, sessionId = '', runId = '' }) {
  const input = safeJson(block.input);
  return (
    <details className={`conversation-process tool-process status-${block.status}`} open={block.status === 'running' || block.status === 'error'} data-block-key={block.key}>
      <summary><Wrench size={14} /><span>{block.toolName || '工具调用'}</span><BlockMeta status={block.status} durationMs={block.durationMs} /></summary>
      <div className="conversation-process-body">
        {input && <section className="tool-input"><strong>输入</strong><pre>{input}</pre></section>}
        <ToolOutput meta={{
          output_preview: block.output,
          output_truncated: block.outputTruncated,
          output_ref: block.outputRef,
          output_size: block.outputSize,
        }} fallbackText={block.text} sessionId={sessionId} runId={runId} />
        {block.error && <div className="conversation-error" role="alert"><AlertCircle size={13} />{block.error}</div>}
      </div>
    </details>
  );
}

export function BusinessDataResult({ block, sessionId = '', runId = '' }) {
  const query = block.input?.query || block.input?.question || '';
  return (
    <details className={`conversation-process business-data-process status-${block.status}`} open={block.status !== 'finished'} data-block-key={block.key}>
      <summary><Database size={14} /><span>{query ? `业务数据：${query}` : '业务数据查询'}</span><BlockMeta status={block.status} durationMs={block.durationMs} /></summary>
      <div className="conversation-process-body">
        {block.progress?.length > 0 && (
          <ol className="tool-progress-list">
            {block.progress.map((item) => <li key={item.key}><span>{item.text}</span></li>)}
          </ol>
        )}
        <ToolOutput meta={{
          output_preview: block.output,
          output_truncated: block.outputTruncated,
          output_ref: block.outputRef,
          output_size: block.outputSize,
        }} sessionId={sessionId} runId={runId} />
        {block.error && <div className="conversation-error" role="alert"><AlertCircle size={13} />{block.error}</div>}
      </div>
    </details>
  );
}

export function WebSearchSources({ block, sessionId = '', runId = '' }) {
  const query = block.query || block.input?.query || '';
  const sources = Array.isArray(block.sources) ? block.sources : [];
  return (
    <details className={`conversation-process web-search-process status-${block.status}`} open={block.status === 'running' || block.status === 'error'} data-block-key={block.key}>
      <summary><Globe2 size={14} /><span>{query ? `联网搜索：${query}` : '联网搜索'}</span><BlockMeta status={block.status} durationMs={block.durationMs} /></summary>
      <div className="conversation-process-body">
        {block.progress?.length > 0 && (
          <ol className="tool-progress-list">
            {block.progress.map((item) => <li key={item.key}><Search size={11} /><span>{item.text}</span></li>)}
          </ol>
        )}
        {sources.length > 0 && (
          <div className="web-source-list">
            {sources.map((source) => (
              source.url
                ? (
                  <a className="web-source-card" key={source.key || source.url} href={source.url} target="_blank" rel="noreferrer">
                    <WebSourceContent source={source} />
                  </a>
                )
                : (
                  <div className="web-source-card" key={source.key}>
                    <WebSourceContent source={source} />
                  </div>
                )
            ))}
          </div>
        )}
        <ToolOutput meta={{
          output_preview: block.output,
          output_truncated: block.outputTruncated,
          output_ref: block.outputRef,
          output_size: block.outputSize,
        }} sessionId={sessionId} runId={runId} />
        {block.error && <div className="conversation-error" role="alert"><AlertCircle size={13} />{block.error}</div>}
      </div>
    </details>
  );
}

function WebSourceContent({ source }) {
  return (
    <>
      <strong>{source.title || source.domain || source.url}</strong>
      {source.domain && <span>{source.domain}</span>}
      {source.snippet && <p>{source.snippet}</p>}
    </>
  );
}

export function SubagentGroup({ block, sessionId = '', runId = '' }) {
  const group = block.group;
  if (!group) {
    return <div className="conversation-state-block"><GitBranch size={12} />{block.text || '子任务运行'}</div>;
  }
  return (
    <details className={`conversation-process subagent-process status-${group.status}`} open={!group.terminal} data-block-key={block.key}>
      <summary><GitBranch size={14} /><span>多智能体协作 · {group.children.length} 个子任务</span><BlockMeta status={group.status} durationMs={group.durationMs} /></summary>
      <div className="conversation-process-body subagent-task-list">
        {group.children.map((child) => <SubagentTask key={child.key} task={child} sessionId={sessionId} runId={runId} />)}
      </div>
    </details>
  );
}

function SubagentTask({ task, sessionId, runId }) {
  const items = task.items?.length ? task.items : [...task.events, ...task.groups];
  return (
    <details className={`subagent-task status-${task.status}`} open={!task.terminal}>
      <summary><span>{task.task || task.subagentId || '子任务'}</span><BlockMeta status={task.status} durationMs={task.durationMs} /></summary>
      <div className="subagent-events">
        {items.map((item) => (
          item.subtype === 'group'
            ? <SubagentGroup key={item.key} block={{ key: item.key, group: item }} sessionId={sessionId} runId={runId} />
            : (
              <div className={`subagent-event kind-${item.kind}`} key={item.key}>
                <strong>{item.toolName || (item.kind === 'reasoning' ? '思考' : item.kind === 'content' ? '输出' : '步骤')}</strong>
                {item.progress?.length > 0 && (
                  <ol className="tool-progress-list">
                    {item.progress.map((progress) => <li key={progress.key}><span>{progress.text}</span></li>)}
                  </ol>
                )}
                {item.text && <pre>{item.text}</pre>}
                {['tool', 'business_data', 'web_search'].includes(item.kind) && (
                  <ToolOutput meta={{
                    output_preview: item.output,
                    output_truncated: item.outputTruncated,
                    output_ref: item.outputRef,
                    output_size: item.outputSize,
                  }} sessionId={sessionId} runId={runId} />
                )}
                {item.error && <span className="conversation-error">{item.error}</span>}
              </div>
            )
        ))}
      </div>
    </details>
  );
}

export function RunStatusBlock({ block, turn, answerText, runState = null }) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const tracksActiveRun = Boolean(
    (turn.status === 'running' || turn.status === 'queued') && runState?.isLoading,
  );
  useEffect(() => {
    if (!tracksActiveRun) return undefined;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [runState?.startedAt, tracksActiveRun]);

  const duration = formatDurationMs(turn.durationMs);
  const isFailure = turn.status === 'error';
  const isCancelled = turn.status === 'cancelled';
  const isAwaiting = turn.status === 'awaiting';
  const activeProgress = tracksActiveRun ? resolveRunProgressView(runState, nowMs) : null;
  return (
    <div className={`statusline run-status status-${turn.status}`} data-block-key={block.key}>
      <span className="run-status-summary">
        {activeProgress
          ? <span className="tdots" aria-hidden="true"><span /><span /><span /></span>
          : turn.status === 'running' ? <Loader2 className="spin" size={12} /> : isFailure ? <AlertCircle size={12} /> : isCancelled ? <Circle size={12} /> : isAwaiting ? <Clock3 size={12} /> : <Check size={12} />}
        {activeProgress?.summary || `${statusLabel(turn.status)}${duration ? ` · 总耗时 ${duration}` : ''}`}
      </span>
      {(block.text || turn.error) && (isFailure || isCancelled || isAwaiting) && (
        <span className="run-status-detail">{block.text || turn.error}</span>
      )}
      {turn.terminal && (
        <span className="run-status-actions">
          <RunFeedback runId={turn.runId} status={turn.status} />
          <CopyButton text={answerText} label="复制回答" />
        </span>
      )}
    </div>
  );
}
