import { Activity, AlertCircle, BrainCircuit, Check, Circle, Clock3, Database, GitBranch, Globe2, Wrench, X } from 'lucide-react';

import { formatTime } from '../../../redesign/chatUtils';
import { formatDurationMs } from '../lib/chatPresentation';
import ToolOutput from './ToolOutput';

const TRACE_NODE_ICONS = {
  reasoning: BrainCircuit,
  tool: Wrench,
  business_data: Database,
  web_search: Globe2,
  subagent: GitBranch,
  iteration: Clock3,
  error: AlertCircle,
};

const labelForNode = (node) => {
  if (node.type === 'reasoning') return node.text || '模型思考';
  if (node.type === 'business_data') return node.toolName || '业务数据查询';
  if (node.type === 'web_search') return node.toolName || '联网搜索';
  if (node.type === 'tool') return node.toolName || '工具调用';
  if (node.type === 'subagent') return node.text || '子任务';
  if (node.type === 'awaiting') return '等待用户输入';
  if (node.type === 'status') return `运行${node.status || ''}`;
  return node.text || node.type;
};

const safeJson = (value) => {
  if (value == null || value === '') return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
};

export default function TracePanel({ model, width, onClose, sessionId = '' }) {
  return (
    <aside className="trace" style={width ? { width } : undefined} aria-label="运行轨迹">
      <div className="tr-head">
        <span className="trace-title"><Activity size={14} />运行轨迹</span>
        <span className="trace-caption">Trace</span>
        <button className="iconbtn trace-close" type="button" aria-label="关闭运行轨迹" onClick={onClose}><X size={16} /></button>
      </div>
      <div className="tr-body">
        {!model?.turns?.length && <div className="ch-empty trace-empty">发起对话后，这里按回合展示思考、工具、子任务和耗时。</div>}
        {(model?.turns || []).map((turn, index) => <TraceTurn key={turn.key} turn={turn} index={index + 1} sessionId={sessionId} defaultOpen={index === model.turns.length - 1} />)}
      </div>
    </aside>
  );
}

function TraceTurn({ turn, index, sessionId, defaultOpen }) {
  const hasIterationSteps = turn.iterations?.some((iteration) => iteration.children?.length > 0);
  return (
    <details className="trt" open={turn.status === 'running' || defaultOpen}>
      <summary>
        <span className="car">▶</span>
        <span className={`tr-dot ${turn.status}`} />
        <span className="trt-t">第 {index} 轮</span>
        <span className="trt-s">{turn.status}{turn.durationMs != null ? ` · ${formatDurationMs(turn.durationMs)}` : ''}{turn.startedAt != null ? ` · ${formatTime(turn.startedAt)}` : ''}</span>
      </summary>
      <div className="trt-kids">
        {turn.iterations?.length > 0 && (
          <div className="trace-iterations" aria-label="迭代耗时">
            {turn.iterations.map((iteration, iterationIndex) => (
              <details className="trace-iteration-group" key={iteration.key} open={iterationIndex === turn.iterations.length - 1}>
                <summary>
                  <span className="car">▶</span><Clock3 size={11} />
                  <span>迭代 {iteration.metrics?.iteration || iteration.order + 1}</span>
                  <strong>{formatDurationMs(iteration.durationMs) || '-'}</strong>
                </summary>
                <div className="trace-iteration-children">
                  {!iteration.children?.length && <div className="trn-plain">本次迭代没有可展示步骤</div>}
                  {(iteration.children || []).map((node) => <TraceNode key={node.key} node={node} sessionId={sessionId} runId={turn.runId} />)}
                </div>
              </details>
            ))}
          </div>
        )}
        {!turn.nodes.length && !hasIterationSteps && <div className="trn-plain">直接回答，未调用工具</div>}
        {turn.nodes.map((node) => <TraceNode key={node.key} node={node} sessionId={sessionId} runId={turn.runId} />)}
        {turn.runId && <div className="tr-run" title={turn.runId}>run · {turn.runId}</div>}
      </div>
    </details>
  );
}

function TraceNode({ node, sessionId, runId }) {
  const Icon = TRACE_NODE_ICONS[node.type] || Circle;
  const duration = formatDurationMs(node.durationMs);
  const hasDistinctDetail = Boolean(node.detail && node.detail !== node.text);
  const hasDetail = Boolean(
    hasDistinctDetail || node.input || node.output || node.error || node.outputRef || node.children?.length
  );
  const stateIcon = node.status === 'error' ? <AlertCircle size={11} /> : node.terminal ? <Check size={11} /> : <Circle size={11} />;

  if (!hasDetail) {
    return (
      <div className="trn-plain" data-source-key={node.sourceKey}>
        <span className="tr-ic"><Icon size={11} /></span>
        <span className="tr-lb">{labelForNode(node)}</span>
        <span className="tr-st">{duration || stateIcon}</span>
      </div>
    );
  }

  return (
    <details className={`trn status-${node.status}`} data-source-key={node.sourceKey} open={!node.terminal || node.status === 'error'}>
      <summary>
        <span className="car">▶</span>
        <span className="tr-ic"><Icon size={11} /></span>
        <span className={`tr-lb ${node.status === 'error' ? 'err' : ''}`}>{labelForNode(node)}</span>
        <span className="tr-st">{duration || stateIcon}</span>
      </summary>
      <div className="trn-detail">
        {node.input != null && <TraceBlock title="输入" text={safeJson(node.input)} />}
        {hasDistinctDetail && <TraceBlock title="详情" text={node.detail} />}
        {(node.output || node.outputRef || node.outputTruncated) && (
          <ToolOutput meta={{
            output_preview: node.output,
            output_truncated: node.outputTruncated,
            output_ref: node.outputRef,
            output_size: node.outputSize,
          }} sessionId={sessionId} runId={runId} />
        )}
        {node.error && <div className="conversation-error" role="alert"><AlertCircle size={12} />{node.error}</div>}
        {node.children?.length > 0 && (
          <div className="trace-node-children">
            {node.children.map((child) => <TraceNode key={child.key} node={child} sessionId={sessionId} runId={runId} />)}
          </div>
        )}
      </div>
    </details>
  );
}

function TraceBlock({ title, text }) {
  return <div className="trn-block"><div className="trn-bt">{title}</div><pre className="trn-pre">{text}</pre></div>;
}
