import { memo, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Boxes,
  Gauge,
  Maximize2,
  Network,
  PanelRightOpen,
  Pause,
  Scale,
  Workflow,
  X,
} from 'lucide-react';
import {
  applyNodeChanges,
  BaseEdge,
  Background,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
} from '@xyflow/react';

import {
  filterStudioGraph,
  KIND_META,
  studioGraphChain,
  studioGraphEdgeStyle,
  studioGraphNeighborhood,
  studioSummaryVisible,
  studioVisibleElementId,
} from './studio-model.js';
import { fallbackStudioLayout, layoutStudioGraph } from './studio-graph-layout.js';
import {
  buildStudioRoutePresentation,
  studioLabelPosition,
  studioPolylineLength,
  studioRoundedPath,
  studioRouteHandles,
  studioRouteForNodePositions,
  studioRoutePoints,
} from './studio-graph-geometry.js';

const KIND_ICON = { object: Boxes, operation: Workflow, rule: Scale, metric: Gauge, concept: Network };
const MAX_PARTICLE_EDGES = 20;
const PARTICLE_NODE_LIMIT = 500;
const PARTICLE_EDGE_LIMIT = 1500;
const ROUTE_PRESENTATION_EDGE_LIMIT = 400;
export const STUDIO_NODE_CLICK_DISTANCE = 6;
export const STUDIO_NODE_DRAG_THRESHOLD = 6;

export default function StudioGraph({ session, update, onOpen, focus }) {
  const instanceRef = useRef(null);
  const graphRef = useRef(null);
  const layoutRequestRef = useRef(0);
  const firstLayoutRef = useRef(true);
  const centeredFocusElementRef = useRef('');
  const initialViewportRef = useRef(session.graphViewport);
  const [zoomTier, setZoomTier] = useState(() => studioZoomTier(session.graphViewport?.zoom || 0.8));
  const [hoveredEdgeId, setHoveredEdgeId] = useState('');
  const [dismissedSummaryElementId, setDismissedSummaryElementId] = useState('');
  const [draggedNodePosition, setDraggedNodePosition] = useState(null);
  const [isViewportMoving, setIsViewportMoving] = useState(false);
  const [isNodeDragging, setIsNodeDragging] = useState(false);
  const reducedMotion = useReducedMotion();
  const motionEnabled = session.graphMotion ?? !reducedMotion;
  const filtered = useMemo(
    () => filterStudioGraph(session.graph, session.kind, session.query, session.elements, session.hiddenKinds),
    [session.elements, session.graph, session.hiddenKinds, session.kind, session.query],
  );
  const [layoutResult, setLayoutResult] = useState(() => ({
    nodes: fallbackStudioLayout(filtered.nodes),
    routes: {},
    ready: false,
  }));

  useEffect(() => {
    const requestId = layoutRequestRef.current + 1;
    layoutRequestRef.current = requestId;
    setLayoutResult({ nodes: fallbackStudioLayout(filtered.nodes), routes: {}, ready: false });
    layoutStudioGraph(filtered.nodes, filtered.edges).then((result) => {
      if (layoutRequestRef.current === requestId) setLayoutResult({ ...result, ready: true });
    }).catch(() => {
      if (layoutRequestRef.current === requestId) {
        setLayoutResult({ nodes: fallbackStudioLayout(filtered.nodes), routes: {}, ready: true });
      }
    });
    return () => { layoutRequestRef.current += 1; };
  }, [filtered.edges, filtered.nodes]);

  const arrangedNodes = layoutResult.nodes;
  const nodesById = useMemo(() => new Map(filtered.nodes.map((node) => [node.id, node])), [filtered.nodes]);
  const visibleSelectedElementId = useMemo(
    () => studioVisibleElementId(session.selectedElementId, filtered.nodes),
    [filtered.nodes, session.selectedElementId],
  );
  const arrangedNodesById = useMemo(
    () => new Map(arrangedNodes.map((node) => [node.id, node])),
    [arrangedNodes],
  );
  const positionedNodes = useMemo(() => arrangedNodes.map((node) => ({
    ...node,
    position: draggedNodePosition?.id === node.id
      ? draggedNodePosition.position
      : session.nodePositions[node.id] || node.position,
  })), [arrangedNodes, draggedNodePosition, session.nodePositions]);
  const positionedNodesById = useMemo(
    () => new Map(positionedNodes.map((node) => [node.id, node])),
    [positionedNodes],
  );
  const displayedRoutes = useMemo(() => Object.fromEntries(filtered.edges.map((edge) => {
    const sourceNode = arrangedNodesById.get(edge.source);
    const targetNode = arrangedNodesById.get(edge.target);
    return [edge.id, studioRouteForNodePositions(
      layoutResult.routes[edge.id] || [],
      sourceNode,
      targetNode,
      positionedNodesById.get(edge.source)?.position,
      positionedNodesById.get(edge.target)?.position,
    )];
  })), [arrangedNodesById, filtered.edges, layoutResult.routes, positionedNodesById]);
  const degreeById = useMemo(() => {
    const degree = new Map(filtered.nodes.map((node) => [node.id, 0]));
    filtered.edges.forEach((edge) => {
      degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    });
    return degree;
  }, [filtered.edges, filtered.nodes]);
  const active = useMemo(() => {
    if (visibleSelectedElementId) return studioGraphNeighborhood(visibleSelectedElementId, filtered.edges);
    if (session.graphHighlight === 'rule' || session.graphHighlight === 'metric') {
      return studioGraphChain(session.graphHighlight, filtered.nodes, filtered.edges);
    }
    return { nodes: new Set(), edges: new Set() };
  }, [filtered.edges, filtered.nodes, session.graphHighlight, visibleSelectedElementId]);
  const hasHighlight = active.nodes.size > 0;
  const routePresentation = useMemo(() => (
    filtered.edges.length <= ROUTE_PRESENTATION_EDGE_LIMIT
      ? buildStudioRoutePresentation(
        displayedRoutes,
        Object.fromEntries(filtered.edges.map((edge) => [
          edge.id,
          (!hasHighlight || active.edges.has(edge.id)) ? edge.label || '' : '',
        ])),
        positionedNodes,
      )
      : { labelAnchors: {}, sharedEdges: {} }
  ), [active.edges, displayedRoutes, filtered.edges, hasHighlight, positionedNodes]);
  const hoveredEdgeIds = useMemo(() => {
    if (!hoveredEdgeId) return new Set();
    return new Set([hoveredEdgeId, ...(routePresentation.sharedEdges[hoveredEdgeId] || [])]);
  }, [hoveredEdgeId, routePresentation.sharedEdges]);
  const particleEdgeIds = useMemo(() => {
    const result = new Set();
    const passivePaused = !visibleSelectedElementId && (zoomTier === 'compact' || isViewportMoving);
    const paused = !motionEnabled
      || passivePaused
      || isNodeDragging
      || filtered.nodes.length > PARTICLE_NODE_LIMIT
      || filtered.edges.length > PARTICLE_EDGE_LIMIT;
    if (paused) return result;
    const add = (edgeId) => {
      if (result.size < MAX_PARTICLE_EDGES) result.add(edgeId);
    };
    hoveredEdgeIds.forEach(add);
    if (visibleSelectedElementId) {
      filtered.edges
        .filter((edge) => edge.source === visibleSelectedElementId || edge.target === visibleSelectedElementId)
        .forEach((edge) => add(edge.id));
    } else if (session.graphHighlight === 'rule' || session.graphHighlight === 'metric') {
      filtered.edges.filter((edge) => active.edges.has(edge.id)).forEach((edge) => add(edge.id));
    }
    return result;
  }, [active.edges, filtered.edges, filtered.nodes.length, hoveredEdgeIds, isNodeDragging, isViewportMoving, motionEnabled, session.graphHighlight, visibleSelectedElementId, zoomTier]);

  const projectedNodes = useMemo(() => arrangedNodes.map((node) => {
    const meta = KIND_META[node.kind] || KIND_META.object;
    const selected = node.id === visibleSelectedElementId;
    return {
      ...node,
      id: node.id,
      type: 'studio',
      className: selected ? 'selected-node-glow' : '',
      position: session.nodePositions[node.id] || node.position,
      style: { width: node.width, height: node.height, '--node-glow': meta.glow },
      zIndex: selected ? 4 : active.nodes.has(node.id) ? 3 : 2,
      data: {
        id: node.id,
        label: node.label,
        kind: node.kind,
        relationCount: degreeById.get(node.id) || 0,
        selected,
        highlighted: active.nodes.has(node.id),
        dimmed: hasHighlight && !active.nodes.has(node.id),
        onOpen,
        onSelect: (elementId) => update({ selectedElementId: elementId }),
      },
      ariaLabel: `${meta.label || node.kind}：${node.label}`,
    };
  }), [active.nodes, arrangedNodes, degreeById, hasHighlight, onOpen, session.nodePositions, update, visibleSelectedElementId]);
  const [nodes, setNodes] = useState(projectedNodes);
  const edges = useMemo(() => filtered.edges.map((edge) => {
    const visual = studioGraphEdgeStyle(edge, nodesById);
    const dimmed = hasHighlight && !active.edges.has(edge.id);
    const route = displayedRoutes[edge.id] || [];
    return {
      ...edge,
      ...studioRouteHandles(route, positionedNodesById.get(edge.source), positionedNodesById.get(edge.target)),
      type: 'studio',
      selectable: true,
      focusable: false,
      markerEnd: { type: MarkerType.ArrowClosed, color: visual.color, width: 14, height: 14 },
      data: {
        points: route,
        label: edge.label,
        color: visual.color,
        labelColor: visual.labelColor,
        dash: visual.dash,
        active: active.edges.has(edge.id),
        hovered: hoveredEdgeIds.has(edge.id),
        labelAnchor: routePresentation.labelAnchors[edge.id],
        particleCount: particleEdgeIds.has(edge.id) ? (active.edges.has(edge.id) ? 2 : 1) : 0,
        dimmed,
      },
      zIndex: active.edges.has(edge.id) ? 1 : 0,
    };
  }), [active.edges, displayedRoutes, filtered.edges, hasHighlight, hoveredEdgeIds, nodesById, particleEdgeIds, positionedNodesById, routePresentation.labelAnchors]);
  const selectedNode = nodesById.get(visibleSelectedElementId);
  const selectedElement = session.elements.find((element) => element.id === session.selectedElementId);

  useEffect(() => setNodes(projectedNodes), [projectedNodes]);

  useEffect(() => {
    if (!visibleSelectedElementId) setDismissedSummaryElementId('');
  }, [visibleSelectedElementId]);

  useEffect(() => {
    if (!layoutResult.ready) return undefined;
    if (firstLayoutRef.current) {
      firstLayoutRef.current = false;
      if (initialViewportRef.current) return undefined;
    }
    const timer = window.setTimeout(() => instanceRef.current?.fitView({
      padding: 0.14,
      minZoom: graphRef.current?.clientWidth <= 520 ? 0.22 : 0.3,
      maxZoom: 1.08,
      duration: 360,
    }), 30);
    return () => window.clearTimeout(timer);
  }, [layoutResult.ready]);

  const requestedFocusElementId = focus?.view === 'graph' ? focus?.elementId || '' : '';
  useEffect(() => {
    if (!requestedFocusElementId) {
      centeredFocusElementRef.current = '';
      return undefined;
    }
    if (!layoutResult.ready || centeredFocusElementRef.current === requestedFocusElementId) return undefined;
    const node = projectedNodes.find((item) => item.id === requestedFocusElementId);
    if (!node) return undefined;
    const timer = window.setTimeout(() => {
      centeredFocusElementRef.current = requestedFocusElementId;
      const currentZoom = instanceRef.current?.getViewport().zoom || 0.85;
      instanceRef.current?.setCenter(
        node.position.x + node.width / 2,
        node.position.y + node.height / 2,
        { zoom: Math.min(1.2, Math.max(0.88, currentZoom)), duration: 460 },
      );
    }, 40);
    return () => window.clearTimeout(timer);
  }, [layoutResult.ready, projectedNodes, requestedFocusElementId]);

  const toggleLegendKind = (kind) => update((current) => {
    const hidden = new Set(current.hiddenKinds || []);
    if (hidden.has(kind)) hidden.delete(kind);
    else hidden.add(kind);
    const selectedKind = current.graph?.nodes?.find((node) => node.id === current.selectedElementId)?.kind;
    return {
      ...current,
      hiddenKinds: [...hidden],
      selectedElementId: selectedKind && hidden.has(selectedKind) ? '' : current.selectedElementId,
    };
  });

  const toggleFullscreen = async () => {
    if (document.fullscreenElement === graphRef.current) await document.exitFullscreen();
    else await graphRef.current?.requestFullscreen();
  };

  if (!projectedNodes.length) {
    return <div className="studio-state"><b>没有匹配的知识元素</b><span>调整搜索或筛选条件后重试。</span></div>;
  }
  return (
    <div ref={graphRef} className={`studio-graph zoom-${zoomTier}${motionEnabled ? ' motion-enabled' : ''}`}
      aria-label="知识包关系图" aria-busy={!layoutResult.ready} data-layout-ready={layoutResult.ready} data-zoom-tier={zoomTier}>
      <div className="studio-graph-tools" aria-label="图谱工具">
        <div className="seg" aria-label="关系高亮">
          <button type="button" className={session.graphHighlight === 'all' ? 'on' : ''}
            onClick={() => update({ graphHighlight: 'all', selectedElementId: '' })}>全部</button>
          <button type="button" className={session.graphHighlight === 'rule' ? 'on' : ''}
            onClick={() => update({ graphHighlight: 'rule', selectedElementId: '' })}><Scale size={13} />规则链</button>
          <button type="button" className={session.graphHighlight === 'metric' ? 'on' : ''}
            onClick={() => update({ graphHighlight: 'metric', selectedElementId: '' })}><Gauge size={13} />指标链</button>
        </div>
        <button type="button" className={`studio-icon-button${motionEnabled ? ' on' : ''}`}
          title={motionEnabled ? '暂停动态效果' : '开启动态效果'} aria-label={motionEnabled ? '暂停动态效果' : '开启动态效果'}
          aria-pressed={motionEnabled} onClick={() => update({ graphMotion: !motionEnabled })}>
          {motionEnabled ? <Activity size={15} /> : <Pause size={15} />}
        </button>
        <button type="button" className="studio-icon-button" title="全屏查看" aria-label="全屏查看"
          onClick={() => void toggleFullscreen()}><Maximize2 size={15} /></button>
      </div>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        nodesDraggable
        nodesConnectable={false}
        nodesFocusable={false}
        elementsSelectable
        nodeClickDistance={STUDIO_NODE_CLICK_DISTANCE}
        nodeDragThreshold={STUDIO_NODE_DRAG_THRESHOLD}
        onlyRenderVisibleElements
        minZoom={0.2}
        maxZoom={2.2}
        defaultViewport={session.graphViewport || { x: 0, y: 0, zoom: 0.8 }}
        onInit={(instance) => { instanceRef.current = instance; }}
        onMoveStart={() => setIsViewportMoving(true)}
        onMoveEnd={(_event, viewport) => {
          setIsViewportMoving(false);
          setZoomTier(studioZoomTier(viewport.zoom));
          update({ graphViewport: viewport });
        }}
        onNodesChange={(changes) => setNodes((current) => applyNodeChanges(changes, current))}
        onNodeDragStart={(_event, node) => {
          setIsNodeDragging(true);
          setDraggedNodePosition({ id: node.id, position: node.position });
        }}
        onNodeDrag={(_event, node) => setDraggedNodePosition({ id: node.id, position: node.position })}
        onNodeDragStop={(_event, node) => {
          setIsNodeDragging(false);
          setDraggedNodePosition(null);
          update((current) => ({
            ...current,
            nodePositions: { ...current.nodePositions, [node.id]: node.position },
          }));
        }}
        onEdgeMouseEnter={(_event, edge) => setHoveredEdgeId(edge.id)}
        onEdgeMouseLeave={(_event, edge) => setHoveredEdgeId((current) => current === edge.id ? '' : current)}
        onNodeClick={(_event, node) => {
          setDismissedSummaryElementId('');
          update({ selectedElementId: node.id });
        }}
        onNodeDoubleClick={(_event, node) => onOpen(node.id, 'graph')}
        onPaneClick={() => update({ selectedElementId: '' })}
      >
        <Background color="#dfe4ec" gap={24} size={1} />
        <Controls showInteractive={false} position="top-right" />
        {nodes.length <= 80 && <MiniMap pannable zoomable nodeColor={(node) => KIND_META[node.data.kind]?.color || '#98a2b1'} />}
      </ReactFlow>
      <div className="studio-graph-legend" aria-label="图例">
        {Object.entries(KIND_META).map(([kind, meta]) => {
          const visible = !(session.hiddenKinds || []).includes(kind);
          return (
            <button type="button" key={kind} className={visible ? '' : 'off'} aria-pressed={visible}
              onClick={() => toggleLegendKind(kind)}>
              <i className={`studio-swatch ${meta.className}`} style={{ background: meta.color, borderColor: meta.color }} />
              {meta.label}<small>{session.graph?.nodes?.filter((node) => node.kind === kind).length || 0}</small>
            </button>
          );
        })}
      </div>
      {selectedNode && studioSummaryVisible(visibleSelectedElementId, dismissedSummaryElementId) && (
        <aside className="studio-graph-summary" aria-label="节点摘要">
          <button type="button" className="studio-summary-close" title="关闭摘要" aria-label="关闭摘要"
            onClick={() => setDismissedSummaryElementId(visibleSelectedElementId)}><X size={14} /></button>
          <span>{KIND_META[selectedNode.kind]?.label || selectedNode.kind}</span>
          <strong>{selectedNode.label}</strong>
          <code>{selectedNode.id}</code>
          {selectedElement?.description && <p>{selectedElement.description}</p>}
          <small>{degreeById.get(selectedNode.id) || 0} 条关联关系</small>
          <button type="button" className="btn btn-p" onClick={() => onOpen(selectedNode.id, 'graph')}>
            <PanelRightOpen size={14} />打开条目
          </button>
        </aside>
      )}
    </div>
  );
}

const StudioNode = memo(function StudioNode({ data }) {
  const Icon = KIND_ICON[data.kind] || Boxes;
  const meta = KIND_META[data.kind] || KIND_META.object;
  return (
    <div className={`studio-node ${meta.className}${data.selected ? ' selected' : ''}${data.highlighted ? ' highlighted' : ''}${data.dimmed ? ' dimmed' : ''}`}
      style={{ '--node-color': meta.color, '--node-soft': meta.soft, '--node-glow': meta.glow }}
      role="button" tabIndex={0} aria-label={`${meta.label}：${data.label}`}
      onKeyDown={(event) => {
        if (event.key === 'Enter') data.onOpen(data.id, 'graph');
        if (event.key === ' ') {
          event.preventDefault();
          data.onSelect(data.id);
        }
      }}>
      <Handle id="target-left" type="target" position={Position.Left} isConnectable={false} />
      <Handle id="source-left" type="source" position={Position.Left} isConnectable={false} />
      <div className="studio-node-meta"><Icon size={13} /><span>{meta.label}</span><small>{data.relationCount} 条关系</small></div>
      <strong>{data.label}</strong>
      <code>{data.id}</code>
      <Handle id="target-right" type="target" position={Position.Right} isConnectable={false} />
      <Handle id="source-right" type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
});

const StudioEdge = memo(function StudioEdge({ id, sourceX, sourceY, targetX, targetY, markerEnd, data }) {
  const points = studioRoutePoints(data.points, sourceX, sourceY, targetX, targetY);
  const path = studioRoundedPath(points);
  const { x: labelX, y: labelY } = studioLabelPosition(points, data.labelAnchor);
  const emphasized = data.active || data.hovered;
  const dimmed = data.dimmed && !data.hovered;
  const particleDuration = Math.min(3.2, Math.max(1.6, studioPolylineLength(points) / 135));
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={18}
      className={`studio-edge${data.active ? ' active' : ''}${data.hovered ? ' hovered' : ''}`}
      style={{
        stroke: data.color,
        strokeWidth: emphasized ? 2.6 : 1.35,
        strokeDasharray: data.dash,
        opacity: dimmed ? 0.1 : emphasized ? 1 : 0.72,
      }} />
    {Array.from({ length: data.particleCount || 0 }, (_, index) => {
      const begin = -(particleDuration / data.particleCount) * index;
      return <circle key={`${id}:particle:${index}`} className="studio-edge-particle"
        r={index ? 2.5 : 3} fill={data.color} stroke="#fff" strokeWidth={1} aria-hidden="true">
        <animateMotion path={path} dur={`${particleDuration}s`} begin={`${begin}s`} repeatCount="indefinite" />
        <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.12;0.84;1"
          dur={`${particleDuration}s`} begin={`${begin}s`} repeatCount="indefinite" />
      </circle>;
    })}
    {data.label && emphasized && (
      <EdgeLabelRenderer>
        <span className={`studio-edge-label${data.active ? ' active' : ''}${data.hovered ? ' hovered' : ''}`}
          style={{ color: data.labelColor, transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}>
          {data.label}
        </span>
      </EdgeLabelRenderer>
    )}
  </>;
});

const NODE_TYPES = { studio: StudioNode };
const EDGE_TYPES = { studio: StudioEdge };

function studioZoomTier(zoom) {
  if (zoom < 0.52) return 'compact';
  if (zoom < 0.86) return 'standard';
  return 'detail';
}

function useReducedMotion() {
  const canMatch = typeof window !== 'undefined' && typeof window.matchMedia === 'function';
  const [reduced, setReduced] = useState(() => canMatch && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    if (!canMatch) return undefined;
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [canMatch]);
  return reduced;
}
