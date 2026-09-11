import { Fragment, useEffect, useState } from 'react';
import {
  AlertCircle, BrainCircuit, Check, Circle, Clock3, Database, FileText, GitBranch,
  Globe2, Loader2, Play, Search, ShieldCheck, ShieldX, Workflow, Wrench,
} from 'lucide-react';

import Markdown from '../../../shared/ui/Markdown';
import { CandidateResultsPanel } from '../../solver/index.js';
import { formatDurationMs, formatTime } from '../lib/chatPresentation';
import { resolveRunProgressView } from '../lib/streamLifecycle';
import { findVisibleWorkflowRecoveryIndex } from '../lib/workflowErrorPresentation';
import { TurnEvidencePanel } from './EvidencePanel';
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

export default function ConversationView({
  model,
  sessionId = '',
  runState = null,
  evidenceByTurnKey = {},
  onEvidenceSelect,
  onRecoveryAction = null,
  recoveryActionDisabled = false,
}) {
  const messages = model?.messages || [];
  const visibleRecoveryIndex = findVisibleWorkflowRecoveryIndex(messages);
  return (
    <div className="conversation-view" role="log" aria-live="off" aria-relevant="additions">
      {messages.map((message, index) => (
        message.role === 'user'
          ? <UserMessage key={message.key} message={message} />
          : message.role === 'system'
            ? <SystemMessage key={message.key} message={message} />
            : (
              <AssistantTurn key={message.key} turn={message} sessionId={sessionId}
                runState={message.isLoading ? runState : null}
                evidence={evidenceByTurnKey[message.key] || null}
                onEvidenceSelect={(hit) => onEvidenceSelect?.(hit, message.key)}
                onRecoveryAction={onRecoveryAction}
                recoveryActionDisabled={recoveryActionDisabled}
                showRecoveryAction={index === visibleRecoveryIndex} />
            )
      ))}
    </div>
  );
}

export function UserMessage({ message }) {
  return (
    <article className="msg u" data-message-key={message.key}>
      <div className="bubwrap">
        <div className="bub">{message.content}</div>
        <div className="user-message-actions">
          {message.startedAt != null && <div className="time">{formatTime(message.startedAt)}</div>}
          <CopyButton text={message.content} label="复制问题" />
        </div>
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

export function AssistantTurn({
  turn,
  sessionId = '',
  runState = null,
  evidence = null,
  onEvidenceSelect,
  onRecoveryAction = null,
  recoveryActionDisabled = false,
  showRecoveryAction = false,
}) {
  const answerText = (turn.blocks || [])
    .filter((block) => block.kind === 'content')
    .map((block) => block.text)
    .join('\n')
    .trim() || turn.content;
  const hasVisibleBlock = (turn.blocks || []).some((block) => block.kind !== 'status');
  const hasLiveProgress = Boolean(turn.isLoading && runState?.isLoading);
const hasSolverResult = (turn.blocks || []).some(
    (block) => block.kind === 'solver' && Boolean(block.solverResult),
  );
  const firstContentIndex = (turn.blocks || []).findIndex((block) => block.kind === 'content');

  return (
    <article className={`msg a assistant-turn${hasSolverResult ? ' has-solver-result' : ''}`}
      data-run-id={turn.runId || undefined} data-message-key={turn.key}>
      <div className="av">知</div>
      <div className="bubwrap">
        <div className={`bub assistant-turn-body status-${turn.status}`}>
          {(turn.blocks || []).map((block, index) => (
            <Fragment key={block.key}>
              {evidence && index === firstContentIndex && (
                <TurnEvidencePanel evidence={evidence} onSelect={onEvidenceSelect} />
              )}
              <ConversationBlock block={block} turn={turn} answerText={answerText}
                sessionId={sessionId} runState={runState}
                onRecoveryAction={onRecoveryAction}
                recoveryActionDisabled={recoveryActionDisabled}
                showRecoveryAction={showRecoveryAction} />
            </Fragment>
          ))}
          {evidence && firstContentIndex === -1 && (
            <TurnEvidencePanel evidence={evidence} onSelect={onEvidenceSelect} />
          )}
          {turn.isLoading && !hasVisibleBlock && !hasLiveProgress && (
            <span className="assistant-thinking"><span className="tdots"><span /><span /><span /></span>分析问题中…</span>
          )}
        </div>
        {turn.startedAt != null && <div className="time">{formatTime(turn.startedAt)}</div>}
      </div>
    </article>
  );
}

function ConversationBlock({
  block,
  turn,
  answerText,
  sessionId,
  runState,
  onRecoveryAction,
  recoveryActionDisabled,
  showRecoveryAction,
}) {
  if (block.kind === 'content') return <AgentContent block={block} />;
  if (block.kind === 'reasoning') return <ReasoningBlock block={block} />;
  if (block.kind === 'business_data') return <BusinessDataResult block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'web_search') return <WebSearchSources block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'solver') return <SolverCallBlock block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'tool') return <ToolCallBlock block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'artifacts') return <ArtifactBlock block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'subagent') return <SubagentGroup block={block} sessionId={sessionId} runId={turn.runId} />;
  if (block.kind === 'status' || block.kind === 'awaiting') {
    return <RunStatusBlock block={block} turn={turn} answerText={answerText} runState={runState}
      onRecoveryAction={onRecoveryAction} recoveryActionDisabled={recoveryActionDisabled}
      showRecoveryAction={showRecoveryAction} />;
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

const solverSemanticLabel = (value) => ({
  feasible: '可行',
  infeasible: '已证明不可行',
  not_found: '未找到可行解',
  proven_optimal: '已证明最优',
  not_proven: '未证明最优',
  not_applicable: '不适用',
}[value] || value || '—');

const solverProofKindLabel = (value) => ({
  constraint_recalculation: '约束重新计算',
  independent_domain_constraint_check: '独立业务约束校验',
  verifier_exhaustive_check: '全候选独立校验',
  exhaustive: '全候选校验',
  verifier_infeasibility_certificate: '不可行性证明',
}[value] || value || '—');

function SolverAuthoritativeResult({ result }) {
  return (
    <section className="solver-authoritative-result" aria-label="已验证求解结果">
      <header>
        <span className="solver-verified-mark"><ShieldCheck size={15} />独立校验通过</span>
        <span className="solver-authority-label">权威结果</span>
      </header>
      <dl className="solver-result-semantics">
        <div><dt>可行性</dt><dd>{solverSemanticLabel(result.feasibility)}</dd></div>
        <div><dt>最优性</dt><dd>{solverSemanticLabel(result.optimality)}</dd></div>
        <div><dt>证明方式</dt><dd>{solverProofKindLabel(result.proof_kind)}</dd></div>
      </dl>
      <CandidateResultsPanel result={result} />
      <details className="solver-result-evidence">
        <summary>版本与证据</summary>
        <dl>
          {result.knowledge_revision_id && <div><dt>知识版本</dt><dd><code>{result.knowledge_revision_id}</code></dd></div>}
          {result.fact_snapshot_digest && <div><dt>事实快照</dt><dd><code>{result.fact_snapshot_digest}</code></dd></div>}
          {result.result_artifact_ref && <div><dt>结果制品</dt><dd><code>{result.result_artifact_ref}</code></dd></div>}
          {result.verification_evidence_ref && <div><dt>验证证据</dt><dd><code>{result.verification_evidence_ref}</code></dd></div>}
          {result.candidate_results_artifact_ref && <div><dt>候选明细</dt><dd><code>{result.candidate_results_artifact_ref}</code></dd></div>}
          {result.candidate_results_artifact_digest && <div><dt>候选摘要</dt><dd><code>{result.candidate_results_artifact_digest}</code></dd></div>}
        </dl>
      </details>
    </section>
  );
}

export function SolverCallBlock({ block, sessionId = '', runId = '' }) {
  const input = safeJson(block.input);
  const rejection = block.solverRejection;
  return (
    <details className={`conversation-process solver-process status-${block.status}`}
      open={block.status === 'running' || block.status === 'error' || Boolean(block.solverResult)}
      data-block-key={block.key}>
      <summary><Workflow size={14} /><span>求解任务</span><BlockMeta status={block.status} durationMs={block.durationMs} /></summary>
      <div className="conversation-process-body">
        {block.progress?.length > 0 && (
          <ol className="solver-progress-list">
            {block.progress.map((item) => (
              <li key={item.key} data-phase={item.phase || 'unknown'}>
                <span>{item.text || item.phase || item.type}</span>
              </li>
            ))}
          </ol>
        )}
        {block.solverResult && <SolverAuthoritativeResult result={block.solverResult} />}
        {rejection && (
          <div className="solver-verification-rejected" role="alert">
            <ShieldX size={14} />
            <span>{rejection.message || '求解结果未通过独立校验，未发布为业务结论。'}</span>
          </div>
        )}
        {!block.solverResult && !rejection && block.status !== 'running' && (
          <ToolOutput meta={{
            output_preview: block.output,
            output_truncated: block.outputTruncated,
            output_ref: block.outputRef,
            output_size: block.outputSize,
          }} fallbackText={block.text} sessionId={sessionId} runId={runId} />
        )}
        {input && block.status === 'error' && <section className="tool-input"><strong>输入</strong><pre>{input}</pre></section>}
        {block.error && <div className="conversation-error" role="alert"><AlertCircle size={13} />{block.error}</div>}
      </div>
    </details>
  );
}

export function BusinessDataResult({ block, sessionId = '', runId = '' }) {
  const query = block.input?.query || block.input?.question || '';
  const action = typeof block.input?.action === 'string'
    ? block.input.action.trim().toLowerCase()
    : '';
  const isLookup = action === 'lookup_values';
  const label = query
    ? `${isLookup ? '字段值域' : '业务数据'}：${query}`
    : (isLookup ? '查询字段值域' : '查询业务数据');
  return (
    <details className={`conversation-process business-data-process status-${block.status}`} open={block.status !== 'finished'} data-block-key={block.key}>
      <summary><Database size={14} /><span>{label}</span><BlockMeta status={block.status} durationMs={block.durationMs} /></summary>
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

export function RunStatusBlock({
  block,
  turn,
  answerText,
  runState = null,
  onRecoveryAction = null,
  recoveryActionDisabled = false,
  showRecoveryAction = false,
}) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [recoveryRequested, setRecoveryRequested] = useState(false);
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
  const isIncomplete = turn.incomplete || turn.errorCode === 'MODEL_OUTPUT_TRUNCATED';
  const activeProgress = tracksActiveRun ? resolveRunProgressView(runState, nowMs) : null;
  const shouldShowRecoveryAction = Boolean(
    showRecoveryAction
    && isFailure
    && turn.recovery
    && typeof onRecoveryAction === 'function',
  );
  const requestRecovery = async () => {
    if (!shouldShowRecoveryAction || recoveryActionDisabled || recoveryRequested) return;
    setRecoveryRequested(true);
    try {
      const accepted = await onRecoveryAction(turn.recovery.message, turn.recovery);
      if (accepted === false) setRecoveryRequested(false);
    } catch {
      setRecoveryRequested(false);
    }
  };
  return (
    <div className={`statusline run-status status-${turn.status}`} data-block-key={block.key}>
      <span className="run-status-summary">
        {activeProgress
          ? <span className="tdots" aria-hidden="true"><span /><span /><span /></span>
          : turn.status === 'running' ? <Loader2 className="spin" size={12} /> : isFailure ? <AlertCircle size={12} /> : isCancelled ? <Circle size={12} /> : isAwaiting ? <Clock3 size={12} /> : <Check size={12} />}
        {activeProgress?.summary || `${isIncomplete ? '回答未完成' : statusLabel(turn.status)}${duration ? ` · 总耗时 ${duration}` : ''}`}
      </span>
      {(turn.errorPresentation?.message || block.text || turn.error)
        && (isFailure || isCancelled || isAwaiting) && (
        <span className="run-status-detail">
          {turn.errorPresentation?.message || block.text || turn.error}
        </span>
      )}
      {shouldShowRecoveryAction && (
        <span className="run-status-recovery">
          <span>已保留可恢复的任务上下文，可从中断处继续。</span>
          <button type="button" onClick={requestRecovery}
            disabled={recoveryActionDisabled || recoveryRequested}>
            {recoveryRequested ? <Loader2 className="spin" size={13} /> : <Play size={13} />}
            {recoveryRequested ? '正在继续…' : turn.recovery.label}
          </button>
        </span>
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
