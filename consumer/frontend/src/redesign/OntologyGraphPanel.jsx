import { useCallback, useEffect, useMemo, useState } from 'react';

import { authFetch } from '../shared/api/auth';
import { EmptyState, SectionCard, StatusTag } from './ui';

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

export default function OntologyGraphPanel({ ontologyId, version, onOpenSource }) {
  const [graph, setGraph] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [fullScreen, setFullScreen] = useState(false);

  const load = useCallback(async () => {
    if (!ontologyId) return;
    setLoading(true);
    setError('');
    const query = version ? `?version=${encodeURIComponent(version)}` : '';
    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/graph${query}`);
      const payload = await response.json().catch(() => ({}));
      if (response.ok) setGraph(payload);
      else { setGraph(null); setError(payload.error || payload.detail || `加载失败：${response.status}`); }
    } finally {
      setLoading(false);
    }
  }, [ontologyId, version]);

  useEffect(() => { const timer = setTimeout(load, 0); return () => clearTimeout(timer); }, [load]);
  useEffect(() => {
    if (!fullScreen) return undefined;
    const onKeyDown = (event) => { if (event.key === 'Escape') setFullScreen(false); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [fullScreen]);

  const layout = useMemo(() => layoutGraph(graph?.nodes || []), [graph?.nodes]);
  const nodeById = useMemo(() => new Map((graph?.nodes || []).map((node) => [node.id, node])), [graph?.nodes]);

  if (loading) return <div className="kb-tab-body"><SectionCard><div className="empty">图谱加载中…</div></SectionCard></div>;
  if (error) return <div className="kb-tab-body"><SectionCard><EmptyState title="本体图加载失败" description={error} action={<button className="btn" onClick={load}>重试</button>} /></SectionCard></div>;
  if (!graph?.nodes?.length) return <div className="kb-tab-body"><SectionCard><EmptyState title="暂无本体图节点" description="当前版本尚未生成对象或操作节点。" /></SectionCard></div>;

  return (
    <div className={`ontology-graph-shell ${fullScreen ? 'fullscreen' : ''}`}>
      <SectionCard title="本体结构图" description={`${graph.name || ontologyId} · ${graph.version || version || '当前版本'}`}
        actions={<><StatusTag>{graph.stats?.operation_count || 0} 个操作</StatusTag><StatusTag>{graph.stats?.object_count || 0} 个对象</StatusTag><button className="btn" onClick={() => setFullScreen((value) => !value)}>{fullScreen ? '退出全屏' : '全屏查看'}</button></>}>
        <div className="ontology-graph-legend"><span><i className="operation" />操作</span><span><i className="object" />业务对象</span><span>点击节点可打开来源文件</span></div>
        <div className="ontology-graph-canvas">
          <svg viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" aria-label="本体结构图">
            <defs><marker id="mwr-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#b9b7ae" /></marker></defs>
            {(graph.edges || []).map((edge, index) => {
              const source = layout.positions.get(edge.source);
              const target = layout.positions.get(edge.target);
              if (!source || !target) return null;
              const leftToRight = source.x < target.x;
              const x1 = source.x + (leftToRight ? NODE_W : 0);
              const x2 = target.x + (leftToRight ? 0 : NODE_W);
              const y1 = source.y + NODE_H / 2;
              const y2 = target.y + NODE_H / 2;
              const bend = (x1 + x2) / 2;
              return <path key={`${edge.source}-${edge.target}-${edge.type}-${index}`} d={`M${x1},${y1} C${bend},${y1} ${bend},${y2} ${x2},${y2}`} className={edge.confidence === 'explicit' ? 'explicit' : 'inferred'} markerEnd="url(#mwr-arrow)" />;
            })}
            {(graph.nodes || []).map((node) => {
              const position = layout.positions.get(node.id);
              if (!position) return null;
              return (
                <g key={node.id} className={`graph-node ${node.type}`} tabIndex="0" role="button"
                  aria-label={`${node.type} ${node.label}`} onClick={() => node.source && onOpenSource?.(node.source)}>
                  <title>{node.description || node.source || node.label}</title>
                  <rect x={position.x} y={position.y} width={NODE_W} height={NODE_H} rx="9" />
                  <text x={position.x + 13} y={position.y + 18}>{String(node.label || node.key).slice(0, 25)}</text>
                  <text className="source" x={position.x + 13} y={position.y + 33}>{String(node.source || node.id).slice(0, 32)}</text>
                </g>
              );
            })}
          </svg>
        </div>
        <div className="ontology-graph-edge-list">
          {(graph.edges || []).slice(0, 30).map((edge, index) => (
            <span key={`${edge.source}-${edge.target}-${index}`}>
              {nodeById.get(edge.source)?.label || edge.source} → {nodeById.get(edge.target)?.label || edge.target} · {edge.label || edge.type}
            </span>
          ))}
        </div>
      </SectionCard>
    </div>
  );
}
