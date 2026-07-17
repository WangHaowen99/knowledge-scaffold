import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { authFetch } from '../../../shared/api/auth';
import {
  deriveGraphRuntimeState,
  graphEdgeIdentity,
} from './graphRuntime';
import './ontologyGraphPanel.css';
import { EmptyState, SectionCard, StatusTag } from '../../../shared/ui/primitives';

const NODE_W = 210;
const NODE_H = 42;
const ROW_GAP = 62;

const layoutGraph = (nodes) => {
  const operations = nodes.filter((node) => node.type === 'operation');
  const objects = nodes.filter((node) => node.type === 'object');
  const other = nodes.filter((node) => !['operation', 'object'].includes(node.type));
  const positions = new Map();
  operations.forEach((node, index) => positions.set(node.id, { x: 60, y: 58 + index * ROW_GAP }));
  objects.forEach((node, index) => positions.set(node.id, { x: 520, y: 58 + index * ROW_GAP }));
  other.forEach((node, index) => positions.set(node.id, { x: 290, y: 18 + index * ROW_GAP }));
  return {
    positions,
    width: 800,
    height: Math.max(360, (Math.max(operations.length, objects.length, other.length) + 1) * ROW_GAP),
  };
};

const hitLabel = (hit) => {
  if (hit?.state === 'live') return '运行中命中';
  if (hit?.state === 'recent') return '最近命中';
  if (hit?.state === 'history') return '历史命中';
  return '';
};

const nodeAriaLabel = (node, hit) => [
  node.type === 'operation' ? '操作' : (node.type === 'object' ? '业务对象' : node.type),
  node.label || node.key,
  hitLabel(hit),
  node.source ? '按 Enter 或空格打开来源文件' : '',
].filter(Boolean).join('，');

export default function OntologyGraphPanel({
  ontologyId,
  version,
  onOpenSource,
  traceMessages = [],
  runtimeNow,
}) {
  const [graph, setGraph] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [fullScreen, setFullScreen] = useState(false);
  const [resetStatus, setResetStatus] = useState('');
  const abortRef = useRef(null);
  const canvasRef = useRef(null);
  const resetTimerRef = useRef(null);
  const shellRef = useRef(null);
  const fullscreenOriginRef = useRef(null);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    if (!ontologyId) {
      setGraph(null);
      setLoading(false);
      setError('');
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError('');
    const query = version ? `?version=${encodeURIComponent(version)}` : '';
    try {
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/graph${query}`,
        { signal: controller.signal },
      );
      const payload = await response.json().catch(() => ({}));
      if (controller.signal.aborted) return;
      if (response.ok) {
        setGraph(payload);
      } else {
        setGraph(null);
        setError(payload.error || payload.detail || `加载失败：HTTP ${response.status}`);
      }
    } catch (requestError) {
      if (controller.signal.aborted) return;
      setGraph(null);
      setError(requestError instanceof Error && requestError.message
        ? `请求失败：${requestError.message}`
        : '请求失败：无法连接本体图服务');
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [ontologyId, version]);

  useEffect(() => {
    const timer = setTimeout(load, 0);
    return () => {
      clearTimeout(timer);
      abortRef.current?.abort();
      window.clearTimeout(resetTimerRef.current);
    };
  }, [load]);
  useEffect(() => {
    if (!fullScreen) return undefined;
    const shell = shellRef.current;
    const focusable = () => Array.from(shell?.querySelectorAll(
      'button:not([disabled]), [role="button"][tabindex="0"], a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])',
    ) || []);
    (focusable()[0] || shell)?.focus();
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setFullScreen(false);
        return;
      }
      if (event.key !== 'Tab') return;
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        shell?.focus();
        return;
      }
      const first = items[0];
      const last = items.at(-1);
      if (!shell?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      const origin = fullscreenOriginRef.current;
      if (origin instanceof HTMLElement && origin.isConnected) origin.focus();
      fullscreenOriginRef.current = null;
    };
  }, [fullScreen]);

  const layout = useMemo(() => layoutGraph(graph?.nodes || []), [graph?.nodes]);
  const nodeById = useMemo(() => new Map((graph?.nodes || []).map((node) => [node.id, node])), [graph?.nodes]);
  const runtime = useMemo(() => deriveGraphRuntimeState(
    graph,
    traceMessages,
    { ontologyId, ...(runtimeNow == null ? {} : { now: runtimeNow }) },
  ), [graph, ontologyId, runtimeNow, traceMessages]);

  const toggleFullScreen = useCallback(() => {
    if (!fullScreen) fullscreenOriginRef.current = document.activeElement;
    setFullScreen((value) => !value);
  }, [fullScreen]);

  const resetView = useCallback(() => {
    canvasRef.current?.scrollTo({ top: 0, left: 0, behavior: 'smooth' });
    window.clearTimeout(resetTimerRef.current);
    setResetStatus('本体图视图已重置');
    resetTimerRef.current = window.setTimeout(() => setResetStatus(''), 1200);
  }, []);

  const openSource = useCallback((node) => {
    if (node?.source) onOpenSource?.(node.source);
  }, [onOpenSource]);

  const onNodeKeyDown = useCallback((event, node) => {
    if (!node?.source || !['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    openSource(node);
  }, [openSource]);

  if (loading) return <div className="kb-tab-body"><SectionCard><div className="empty">图谱加载中…</div></SectionCard></div>;
  if (error) return <div className="kb-tab-body"><SectionCard><EmptyState title="本体图加载失败" description={error} action={<button className="btn" onClick={load}>重试</button>} /></SectionCard></div>;
  if (!graph?.nodes?.length) return <div className="kb-tab-body"><SectionCard><EmptyState title="暂无本体图节点" description="当前版本尚未生成对象或操作节点。" /></SectionCard></div>;

  return (
    <div ref={shellRef} className={`ontology-runtime-graph-shell ${fullScreen ? 'fullscreen' : ''}`}
      role={fullScreen ? 'dialog' : undefined} aria-modal={fullScreen || undefined}
      aria-label={fullScreen ? '全屏本体结构图' : undefined} tabIndex={fullScreen ? -1 : undefined}>
      <SectionCard title="本体结构图" description={`${graph.name || ontologyId} · ${graph.version || version || '当前版本'}`}
        actions={<div className="ontology-runtime-graph-toolbar"><StatusTag>{graph.stats?.operation_count || 0} 个操作</StatusTag><StatusTag>{graph.stats?.object_count || 0} 个对象</StatusTag>{runtime.counts.live > 0 && <StatusTag>{runtime.counts.live} 个运行中命中</StatusTag>}<button type="button" className="btn" onClick={resetView}>重置视图</button><button type="button" className="btn" aria-pressed={fullScreen} onClick={toggleFullScreen}>{fullScreen ? '退出全屏' : '全屏查看'}</button></div>}>
        <div className="ontology-runtime-graph-legend" aria-label="本体图图例"><span><i />操作</span><span><i className="object" />业务对象</span><span><i className="edge" />明确关系</span><span><i className="edge inferred" />推断关系</span><span><i className="live" />运行中命中</span><span><i className="recent" />最近命中</span><span><i className="history" />历史命中</span><span>节点比例固定；可用 Enter 或空格打开来源</span></div>
        <div className="ontology-runtime-graph-canvas" ref={canvasRef}>
          <svg viewBox={`0 0 ${layout.width} ${layout.height}`} width={layout.width} height={layout.height} preserveAspectRatio="xMinYMin meet" role="group" aria-label="本体结构与运行命中图">
            <defs>
              <marker id="mwr-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#b9b7ae" /></marker>
              <marker id="mwr-arrow-live" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#2878d0" /></marker>
              <marker id="mwr-arrow-recent" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#c58211" /></marker>
              <marker id="mwr-arrow-history" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#807d76" /></marker>
            </defs>
            {(graph.edges || []).map((edge, index) => {
              const source = layout.positions.get(edge.source);
              const target = layout.positions.get(edge.target);
              if (!source || !target) return null;
              const hit = runtime.edges[graphEdgeIdentity(edge, index)];
              const leftToRight = source.x < target.x;
              const x1 = source.x + (leftToRight ? NODE_W : 0);
              const x2 = target.x + (leftToRight ? 0 : NODE_W);
              const y1 = source.y + NODE_H / 2;
              const y2 = target.y + NODE_H / 2;
              const bend = (x1 + x2) / 2;
              return <path key={`${edge.source}-${edge.target}-${edge.type}-${index}`} d={`M${x1},${y1} C${bend},${y1} ${bend},${y2} ${x2},${y2}`} className={`ontology-runtime-edge ${edge.confidence === 'explicit' ? 'explicit' : 'inferred'} ${hit ? `hit-${hit.state}` : ''}`} markerEnd={`url(#mwr-arrow${hit ? `-${hit.state}` : ''})`} vectorEffect="non-scaling-stroke" />;
            })}
            {(graph.nodes || []).map((node) => {
              const position = layout.positions.get(node.id);
              if (!position) return null;
              const hit = runtime.nodes[node.id];
              const interactive = Boolean(node.source);
              return (
                <g key={node.id} className={`ontology-runtime-node ${node.type} ${interactive ? 'has-source' : ''} ${hit ? `hit-${hit.state}` : ''}`} tabIndex={interactive ? 0 : undefined} role={interactive ? 'button' : undefined}
                  aria-label={nodeAriaLabel(node, hit)} onClick={() => openSource(node)} onKeyDown={(event) => onNodeKeyDown(event, node)}>
                  <title>{[node.description || node.source || node.label, hitLabel(hit)].filter(Boolean).join(' · ')}</title>
                  <rect x={position.x} y={position.y} width={NODE_W} height={NODE_H} rx="9" vectorEffect="non-scaling-stroke" />
                  <text x={position.x + 13} y={position.y + 18}>{String(node.label || node.key).slice(0, 25)}</text>
                  <text className="source" x={position.x + 13} y={position.y + 33}>{String(node.source || node.id).slice(0, 32)}</text>
                </g>
              );
            })}
          </svg>
        </div>
        <div className="ontology-runtime-graph-edge-list" aria-label="关系列表">
          {(graph.edges || []).slice(0, 30).map((edge, index) => (
            <span key={`${edge.source}-${edge.target}-${index}`} className={runtime.edges[graphEdgeIdentity(edge, index)] ? `hit-${runtime.edges[graphEdgeIdentity(edge, index)].state}` : ''}>
              {nodeById.get(edge.source)?.label || edge.source} → {nodeById.get(edge.target)?.label || edge.target} · {edge.label || edge.type}
            </span>
          ))}
        </div>
        <div className="ontology-runtime-graph-sr-status" role="status" aria-live="polite">{resetStatus}</div>
      </SectionCard>
    </div>
  );
}
