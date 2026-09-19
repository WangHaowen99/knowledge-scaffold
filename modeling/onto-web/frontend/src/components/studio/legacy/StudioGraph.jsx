import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Boxes,
  FileCode2,
  Gauge,
  Maximize2,
  Minimize2,
  PanelRightOpen,
  Pause,
  Play,
  RotateCcw,
  Scale,
  Search,
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
  clearStudioSearch,
  KIND_META,
  projectStudioLargeGraph,
  shouldSubmitStudioSearch,
  stageStudioSearch,
  studioSearchDraft,
  studioGraphChain,
  studioGraphEdgeStyle,
  studioGraphNeighborhood,
  studioGraphScrollSurface,
  studioSummaryVisible,
  submitStudioSearch,
  studioVisibleElementId,
} from './studio-model.js';
import { fallbackStudioLayout, layoutStudioGraph } from './studio-graph-layout.js';
// onto-web 适配：OperationFlowPanel 依赖 modeling 后端 API，不随图模块搬迁
import StudioWebGLGraph from './webgl/StudioWebGLGraph.jsx';
import {
  buildStudioRoutePresentation,
  studioLabelPosition,
  studioPolylineLength,
  studioRoundedPath,
  studioRouteHandles,
  studioRouteForNodePositions,
  studioRoutePoints,
} from './studio-graph-geometry.js';

const KIND_ICON = { object: Boxes, operation: Workflow, rule: Scale, metric: Gauge, script: FileCode2 };
const CLUSTER_META = {
  label: '关系簇', className: 'cluster', color: '#176b87', soft: '#b9e3ed', glow: 'rgba(23, 107, 135, .46)',
};
const studioGraphKindMeta = (kind) => kind === 'cluster' ? CLUSTER_META : KIND_META[kind] || KIND_META.object;
const MAX_PARTICLE_EDGES = 20;
const PARTICLE_NODE_LIMIT = 500;
const PARTICLE_EDGE_LIMIT = 1500;
const ROUTE_PRESENTATION_EDGE_LIMIT = 400;
export const STUDIO_NODE_CLICK_DISTANCE = 6;
export const STUDIO_NODE_DRAG_THRESHOLD = 6;

export function studioGraphNodeAttributes(node, details = {}) {
  if (node?.kind !== 'object') return [];
  const fields = details?.[node.id]?.values?.fields;
  return Array.isArray(fields) ? fields : [];
}

export default function StudioGraph({ projectId, session, update, onOpen, focus }) {
  const instanceRef = useRef(null);
  const graphRef = useRef(null);
  const graphCanvasRef = useRef(null);
  const fallbackFullscreenRef = useRef(false);
  const layoutRequestRef = useRef(0);
  const firstLayoutRef = useRef(true);
  const centeredFocusElementRef = useRef('');
  const initialViewportRef = useRef(session.graphViewport);
  const [zoomTier, setZoomTier] = useState(() => studioZoomTier(session.graphViewport?.zoom || 0.8));
  const [hoveredEdgeId, setHoveredEdgeId] = useState('');
  const [dismissedSummaryElementId, setDismissedSummaryElementId] = useState('');
  const [dismissedOperationFlowElementId, setDismissedOperationFlowElementId] = useState('');
  const [draggedNodePosition, setDraggedNodePosition] = useState(null);
  const [isViewportMoving, setIsViewportMoving] = useState(false);
  const [isNodeDragging, setIsNodeDragging] = useState(false);
  const [graphFullscreen, setGraphFullscreen] = useState(false);
  const [graphFullscreenZoom, setGraphFullscreenZoom] = useState(1);
  const [graphCanvasViewport, setGraphCanvasViewport] = useState({ width: 0, height: 0 });
  const [graphScrollPosition, setGraphScrollPosition] = useState({ left: 0, top: 0 });
  const [webglUnavailable, setWebglUnavailable] = useState(false);
  const [forceStatus, setForceStatus] = useState('initializing');
  const reducedMotion = useReducedMotion();
  const motionEnabled = true;
  const searchDraft = studioSearchDraft(session);
  const submitSearch = useCallback(() => update(submitStudioSearch), [update]);
  const clearSearch = useCallback(() => update(clearStudioSearch), [update]);
  const selectElement = useCallback((elementId) => {
    setDismissedSummaryElementId('');
    setDismissedOperationFlowElementId('');
    update({ selectedElementId: elementId });
  }, [update]);
  const rawFiltered = useMemo(
    () => filterStudioGraph(session.graph, session.kind, session.query, session.elements, session.hiddenKinds),
    [session.elements, session.graph, session.hiddenKinds, session.kind, session.query],
  );
  const filtered = useMemo(() => projectStudioLargeGraph(rawFiltered, {
    expandedClusterIds: session.expandedClusterIds,
    query: session.query,
    selectedElementId: session.selectedElementId,
  }), [rawFiltered, session.expandedClusterIds, session.query, session.selectedElementId]);
  const selectGraphElement = useCallback((elementId) => {
    if (filtered.clusters[elementId]) {
      update((current) => ({
        ...current,
        selectedElementId: '',
        expandedClusterIds: [...new Set([...(current.expandedClusterIds || []), elementId])],
      }));
      return;
    }
    selectElement(elementId);
  }, [filtered.clusters, selectElement, update]);
  const [layoutResult, setLayoutResult] = useState(() => ({
    nodes: fallbackStudioLayout(filtered.nodes, session.graph?.edges || filtered.edges),
    routes: {},
    ready: false,
  }));

  useEffect(() => {
    const requestId = layoutRequestRef.current + 1;
    layoutRequestRef.current = requestId;
    const seededNodes = fallbackStudioLayout(filtered.nodes, session.graph?.edges || filtered.edges);
    if (!webglUnavailable) {
      setLayoutResult({ nodes: seededNodes, routes: {}, ready: true });
      return () => { layoutRequestRef.current += 1; };
    }
    setLayoutResult({ nodes: seededNodes, routes: {}, ready: false });
    layoutStudioGraph(filtered.nodes, filtered.edges, session.graph?.edges || filtered.edges).then((result) => {
      if (layoutRequestRef.current === requestId) setLayoutResult({ ...result, ready: true });
    }).catch(() => {
      if (layoutRequestRef.current === requestId) {
        setLayoutResult({ nodes: fallbackStudioLayout(filtered.nodes, session.graph?.edges || filtered.edges), routes: {}, ready: true });
      }
    });
    return () => { layoutRequestRef.current += 1; };
  }, [filtered.edges, filtered.nodes, session.graph?.edges, webglUnavailable]);

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
  const webglNodes = positionedNodes;
  const displayedRoutes = useMemo(() => webglUnavailable ? Object.fromEntries(filtered.edges.map((edge) => {
    const sourceNode = arrangedNodesById.get(edge.source);
    const targetNode = arrangedNodesById.get(edge.target);
    return [edge.id, studioRouteForNodePositions(
      layoutResult.routes[edge.id] || [],
      sourceNode,
      targetNode,
      positionedNodesById.get(edge.source)?.position,
      positionedNodesById.get(edge.target)?.position,
    )];
  })) : {}, [arrangedNodesById, filtered.edges, layoutResult.routes, positionedNodesById, webglUnavailable]);
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
    webglUnavailable && filtered.edges.length <= ROUTE_PRESENTATION_EDGE_LIMIT
      ? buildStudioRoutePresentation(
        displayedRoutes,
        Object.fromEntries(filtered.edges.map((edge) => [
          edge.id,
          (!hasHighlight || active.edges.has(edge.id)) ? edge.label || '' : '',
        ])),
        positionedNodes,
      )
      : { labelAnchors: {}, sharedEdges: {} }
  ), [active.edges, displayedRoutes, filtered.edges, hasHighlight, positionedNodes, webglUnavailable]);
  const hoveredEdgeIds = useMemo(() => {
    if (!hoveredEdgeId) return new Set();
    return new Set([hoveredEdgeId, ...(routePresentation.sharedEdges[hoveredEdgeId] || [])]);
  }, [hoveredEdgeId, routePresentation.sharedEdges]);
  const particleEdgeIds = useMemo(() => {
    const result = new Set();
    if (!webglUnavailable) return result;
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
  }, [active.edges, filtered.edges, filtered.nodes.length, hoveredEdgeIds, isNodeDragging, isViewportMoving, motionEnabled, session.graphHighlight, visibleSelectedElementId, webglUnavailable, zoomTier]);

  const projectedNodes = useMemo(() => (() => { if (webglUnavailable) return arrangedNodes.map((node) => {
    const meta = studioGraphKindMeta(node.kind);
    const selected = node.id === visibleSelectedElementId;
    return {
      ...node,
      id: node.id,
      type: 'studio',
      className: (() => { if (selected) return 'selected-node-glow'; return ''; })(),
      position: session.nodePositions[node.id] || node.position,
      style: { width: node.width, height: node.height, '--node-glow': meta.glow },
      zIndex: (() => { if (selected) return 14; return active.nodes.has(node.id) ? 13 : 12; })(),
      data: {
        id: node.id,
        label: node.label,
        kind: node.kind,
        relationCount: degreeById.get(node.id) || 0,
        selected,
        highlighted: active.nodes.has(node.id),
        dimmed: hasHighlight && !active.nodes.has(node.id),
        onOpen,
        onSelect: selectGraphElement,
      },
      ariaLabel: `${meta.label || node.kind}：${node.label}`,
    };
  }); return []; })(), [active.nodes, arrangedNodes, degreeById, hasHighlight, onOpen, selectGraphElement, session.nodePositions, visibleSelectedElementId, webglUnavailable]);
  const [nodes, setNodes] = useState(projectedNodes);
  const graphScrollDecorations = useMemo(() => {
    if (!webglUnavailable) return [];
    const bounds = [];
    Object.values(displayedRoutes).forEach((points) => {
      points.forEach((point) => bounds.push({ x: point.x - 4, y: point.y - 4, width: 8, height: 8 }));
    });
    filtered.edges.forEach((edge) => {
      const points = displayedRoutes[edge.id] || [];
      if (!edge.label || points.length < 2) return;
      const position = studioLabelPosition(points, routePresentation.labelAnchors[edge.id]);
      bounds.push({ x: position.x - 60, y: position.y - 14, width: 120, height: 28 });
    });
    return bounds;
  }, [displayedRoutes, filtered.edges, routePresentation.labelAnchors, webglUnavailable]);
  const graphScrollSurface = useMemo(() => studioGraphScrollSurface(
    nodes,
    graphFullscreenZoom,
    graphCanvasViewport,
    80,
    graphScrollDecorations,
  ), [graphCanvasViewport, graphFullscreenZoom, graphScrollDecorations, nodes]);
  const centerFullscreenGraph = useCallback(() => {
    const canvas = graphCanvasRef.current;
    if (!canvas) return;
    canvas.scrollLeft = Math.max(0, (canvas.scrollWidth - canvas.clientWidth) / 2);
    canvas.scrollTop = Math.max(0, (canvas.scrollHeight - canvas.clientHeight) / 2);
  }, []);
  const edges = useMemo(() => (() => { if (webglUnavailable) return filtered.edges.map((edge) => {
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
        particleCount: (() => { if (particleEdgeIds.has(edge.id)) return ((() => { if (active.edges.has(edge.id)) return 2; return 1; })()); return 0; })(),
        dimmed,
      },
      zIndex: (() => { if (active.edges.has(edge.id)) return 1; return 0; })(),
    };
  }); return []; })(), [active.edges, displayedRoutes, filtered.edges, hasHighlight, hoveredEdgeIds, nodesById, particleEdgeIds, positionedNodesById, routePresentation.labelAnchors, webglUnavailable]);
  const selectedNode = nodesById.get(visibleSelectedElementId);
  const selectedElement = session.details?.[session.selectedElementId]
    || session.elements.find((element) => element.id === session.selectedElementId);
  const selectedObjectAttributes = selectedNode?.kind === 'object'
    ? studioGraphNodeAttributes(selectedNode, session.details)
    : [];
  // onto-web 适配：操作流面板不迁移，恒不可见
  const operationFlowVisible = false;

  useEffect(() => setNodes(projectedNodes), [projectedNodes]);

  useEffect(() => {
    const onFullscreenChange = () => {
      if (document.fullscreenElement === graphCanvasRef.current) setGraphFullscreen(true);
      else if (!fallbackFullscreenRef.current) {
        setGraphFullscreen(false);
        setGraphFullscreenZoom(1);
      }
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, []);

  useEffect(() => {
    if (!graphFullscreen || !graphCanvasRef.current) return undefined;
    const canvas = graphCanvasRef.current;
    const measure = () => setGraphCanvasViewport({
      width: canvas.clientWidth,
      height: canvas.clientHeight,
    });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [graphFullscreen]);

  useEffect(() => {
    const canvas = graphCanvasRef.current;
    if (!canvas) return undefined;
    if (!webglUnavailable) {
      const timer = window.setTimeout(() => {
        if (visibleSelectedElementId) instanceRef.current?.focusNode(visibleSelectedElementId);
        else instanceRef.current?.fitView();
      }, 100);
      return () => window.clearTimeout(timer);
    }
    if (!graphFullscreen) {
      canvas.scrollLeft = 0;
      canvas.scrollTop = 0;
      const timer = window.setTimeout(() => instanceRef.current?.fitView({
        padding: 0.14,
        minZoom: canvas.clientWidth <= 520 ? 0.22 : 0.3,
        maxZoom: 1.08,
      }), 80);
      return () => window.clearTimeout(timer);
    }
    let innerFrame = 0;
    const frame = window.requestAnimationFrame(() => {
      innerFrame = window.requestAnimationFrame(() => {
        centerFullscreenGraph();
      });
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (innerFrame) window.cancelAnimationFrame(innerFrame);
    };
  }, [centerFullscreenGraph, graphFullscreen, graphScrollSurface.height, graphScrollSurface.width, visibleSelectedElementId, webglUnavailable]);

  useEffect(() => {
    if (!visibleSelectedElementId) setDismissedSummaryElementId('');
  }, [visibleSelectedElementId]);

  useEffect(() => {
    if (!visibleSelectedElementId) setDismissedOperationFlowElementId('');
  }, [visibleSelectedElementId]);

  useEffect(() => {
    if (session.kind === 'operation') setDismissedOperationFlowElementId('');
  }, [session.kind]);

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
    if (graphFullscreen) {
      fallbackFullscreenRef.current = false;
      if (document.fullscreenElement === graphCanvasRef.current) await document.exitFullscreen();
      setGraphFullscreen(false);
      setGraphFullscreenZoom(1);
      return;
    }
    setGraphFullscreenZoom(1);
    try {
      await graphCanvasRef.current?.requestFullscreen();
      fallbackFullscreenRef.current = false;
      setGraphFullscreen(true);
    } catch {
      fallbackFullscreenRef.current = true;
      setGraphFullscreen(true);
    }
  };
  const changeGraphFullscreenZoom = (value) => setGraphFullscreenZoom(
    Math.min(2, Math.max(0.5, Math.round(value * 4) / 4)),
  );
  const forcePaused = forceStatus === 'paused' || forceStatus === 'settled';
  const resetFullscreenGraph = () => {
    setGraphFullscreenZoom(1);
    window.requestAnimationFrame(() => window.requestAnimationFrame(centerFullscreenGraph));
  };
  const navigateFullscreenGraph = (x, y) => {
    const canvas = graphCanvasRef.current;
    if (!canvas) return;
    canvas.scrollLeft = Math.max(0, Math.min(canvas.scrollWidth - canvas.clientWidth, x - canvas.clientWidth / 2));
    canvas.scrollTop = Math.max(0, Math.min(canvas.scrollHeight - canvas.clientHeight, y - canvas.clientHeight / 2));
  };

  return (
    <div ref={graphRef} className={`studio-graph zoom-${zoomTier}${motionEnabled ? ' motion-enabled' : ''}${operationFlowVisible ? ' has-operation-flow' : ''}`}
      aria-label="知识包关系图" aria-busy={!layoutResult.ready} data-layout-ready={layoutResult.ready} data-zoom-tier={zoomTier}>
      <div ref={graphCanvasRef}
        className={`studio-graph-canvas${graphFullscreen ? ` is-fullscreen${webglUnavailable ? ' is-scrollable' : ''}` : ''}`}
        onWheel={(event) => {
          if (!graphFullscreen || !webglUnavailable) return;
          event.preventDefault();
          changeGraphFullscreenZoom(graphFullscreenZoom + (event.deltaY < 0 ? 0.25 : -0.25));
        }}
        onScroll={(event) => {
          if (!graphFullscreen || !webglUnavailable) return;
          setGraphScrollPosition({ left: event.currentTarget.scrollLeft, top: event.currentTarget.scrollTop });
        }}>
        {graphFullscreen && webglUnavailable && <StudioGraphScrollMiniMap nodes={nodes} routes={displayedRoutes} zoom={graphFullscreenZoom}
          surface={graphScrollSurface} viewport={graphCanvasViewport} scroll={graphScrollPosition}
          onNavigate={navigateFullscreenGraph} />}
        <div className="studio-graph-content" style={graphFullscreen && webglUnavailable ? {
          width: graphScrollSurface.width,
          height: graphScrollSurface.height,
          minWidth: '100%',
          minHeight: '100%',
        } : undefined}>
        <div className="studio-graph-tools" aria-label="图谱工具">
        {graphFullscreen && (
          <div className="studio-graph-fullscreen-search" role="search">
            <input value={searchDraft} aria-label="搜索全屏图谱" placeholder="搜索名称、Key 或说明"
              onChange={(event) => update((current) => stageStudioSearch(current, event.target.value), '')}
              onKeyDown={(event) => {
                event.stopPropagation();
                if (!shouldSubmitStudioSearch(event)) return;
                event.preventDefault();
                submitSearch();
              }} />
            <button type="button" title="搜索" aria-label="搜索" onClick={submitSearch}><Search size={13} /></button>
            {(searchDraft || session.query) && (
              <button type="button" title="清除搜索" aria-label="清除搜索"
                onClick={clearSearch}><X size={13} /></button>
            )}
          </div>
        )}
        <div className="seg" aria-label="关系高亮">
          <button type="button" className={session.graphHighlight === 'all' ? 'on' : ''}
            onClick={() => update({ graphHighlight: 'all', selectedElementId: '' })}>全部</button>
          <button type="button" className={session.graphHighlight === 'rule' ? 'on' : ''}
            onClick={() => update({ graphHighlight: 'rule', selectedElementId: '' })}><Scale size={13} />规则链</button>
          <button type="button" className={session.graphHighlight === 'metric' ? 'on' : ''}
            onClick={() => update({ graphHighlight: 'metric', selectedElementId: '' })}><Gauge size={13} />指标链</button>
        </div>
        {(session.expandedClusterIds || []).length > 0 && (
          <button type="button" className="studio-icon-button" title="折叠关系簇" aria-label="折叠关系簇"
            onClick={() => update({ expandedClusterIds: [], selectedElementId: '' })}><Minimize2 size={15} /></button>
        )}
        {!webglUnavailable && <>
          <span className={`studio-force-status is-${forceStatus}`}><i />{studioForceStatusLabel(forceStatus)}</span>
          <button type="button" className="studio-icon-button"
            title={forcePaused ? '继续力模拟' : '暂停力模拟'} aria-label={forcePaused ? '继续力模拟' : '暂停力模拟'}
            onClick={() => forcePaused ? instanceRef.current?.resumeForce() : instanceRef.current?.pauseForce()}>
            {forcePaused ? <Play size={15} /> : <Pause size={15} />}
          </button>
          <button type="button" className="studio-icon-button" title="重新加热力模拟" aria-label="重新加热力模拟"
            onClick={() => instanceRef.current?.reheatForce()}><RotateCcw size={15} /></button>
        </>}
        <button type="button" className="studio-icon-button" title="全屏查看" aria-label="全屏查看"
          onClick={() => void toggleFullscreen()}>{graphFullscreen ? <X size={15} /> : <Maximize2 size={15} />}</button>
        </div>
        {!filtered.nodes.length && (
          <div className="studio-graph-empty-overlay" role="status">
            <b>没有匹配的知识元素</b><span>调整搜索或筛选条件后重试。</span>
          </div>
        )}
        {webglUnavailable ? <ReactFlow
        key={graphFullscreen ? 'studio-graph-scroll' : 'studio-graph-interactive'}
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        nodesDraggable={!graphFullscreen}
        nodesConnectable={false}
        nodesFocusable={false}
        elementsSelectable
        nodeClickDistance={STUDIO_NODE_CLICK_DISTANCE}
        nodeDragThreshold={STUDIO_NODE_DRAG_THRESHOLD}
        onlyRenderVisibleElements
        minZoom={graphFullscreen ? 0.5 : 0.2}
        maxZoom={graphFullscreen ? 2 : 2.2}
        defaultViewport={graphFullscreen ? undefined : session.graphViewport || { x: 0, y: 0, zoom: 0.8 }}
        viewport={graphFullscreen ? graphScrollSurface.viewport : undefined}
        panOnDrag={!graphFullscreen}
        zoomOnScroll={!graphFullscreen}
        zoomOnPinch={!graphFullscreen}
        preventScrolling={!graphFullscreen}
        onInit={(instance) => { instanceRef.current = instance; }}
        onMoveStart={() => setIsViewportMoving(true)}
        onMoveEnd={(_event, viewport) => {
          setIsViewportMoving(false);
          setZoomTier(studioZoomTier(viewport.zoom));
          if (!graphFullscreen) update({ graphViewport: viewport });
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
        onNodeClick={(_event, node) => selectGraphElement(node.id)}
        onNodeDoubleClick={(_event, node) => filtered.clusters[node.id]
          ? selectGraphElement(node.id)
          : onOpen(node.id, 'graph')}
        onPaneClick={() => update({ selectedElementId: '' })}
        >
        <Background color="#dfe4ec" gap={24} size={1} />
        {graphFullscreen ? <Controls showInteractive={false} position="bottom-right"
          onZoomIn={() => changeGraphFullscreenZoom(graphFullscreenZoom + 0.25)}
          onZoomOut={() => changeGraphFullscreenZoom(graphFullscreenZoom - 0.25)}
          onFitView={resetFullscreenGraph} />
          : <Controls showInteractive={false} position="top-right" />}
        {!graphFullscreen && nodes.length <= 80 && <MiniMap pannable zoomable nodeColor={(node) => studioGraphKindMeta(node.data.kind).color} />}
        </ReactFlow> : <StudioWebGLGraph
          ref={instanceRef}
          nodes={webglNodes}
          edges={filtered.edges}
          routes={{}}
          layoutMode="force"
          selectedId={visibleSelectedElementId}
          activeNodeIds={active.nodes}
          activeEdgeIds={active.edges}
          hasHighlight={hasHighlight}
          motionEnabled
          reducedMotion={reducedMotion}
          initialViewport={initialViewportRef.current}
          onNodeClick={selectGraphElement}
          onNodeDoubleClick={(id) => filtered.clusters[id] ? selectGraphElement(id) : onOpen(id, 'graph')}
          onNodeDragEnd={(id, center) => {
            const node = positionedNodesById.get(id);
            if (!node) return;
            update((current) => ({
              ...current,
              nodePositions: {
                ...current.nodePositions,
                [id]: { x: center.x - node.width / 2, y: center.y - node.height / 2 },
              },
            }));
          }}
          onBackgroundClick={() => update({ selectedElementId: '' })}
          onEdgeHover={(id) => setHoveredEdgeId(id || '')}
          onForceStatusChange={setForceStatus}
          onForceError={() => setForceStatus('error')}
          onUnsupported={() => setWebglUnavailable(true)}
          onZoomTierChange={setZoomTier}
          onViewportChange={(viewport) => {
            if (!graphFullscreen) update({ graphViewport: viewport });
          }}
        />}
        <div className="studio-graph-legend" aria-label="图例">
        {Object.entries(KIND_META).map(([kind, meta]) => {
          // onto-web 适配：本体只有对象/逻辑两类，无实例的 kind 不占图例位
          if (!session.graph?.nodes?.some((node) => node.kind === kind)) return null;
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
        {selectedNode && studioSummaryVisible(visibleSelectedElementId, dismissedSummaryElementId, {
          activeKind: session.kind,
          selectedKind: selectedNode.kind,
        }) && (
          <aside className={`studio-graph-summary${selectedNode.kind === 'object' ? ' has-attributes' : ''}`}
            aria-label={selectedNode.kind === 'object' ? '对象属性' : '节点摘要'}>
          <button type="button" className="studio-summary-close" title="关闭摘要" aria-label="关闭摘要"
            onClick={() => setDismissedSummaryElementId(visibleSelectedElementId)}><X size={14} /></button>
          <span>{KIND_META[selectedNode.kind]?.label || selectedNode.kind}</span>
          <strong>{selectedNode.label}</strong>
          <code>{selectedNode.id}</code>
          {selectedElement?.description && <p>{selectedElement.description}</p>}
          <small>{degreeById.get(selectedNode.id) || 0} 条关联关系</small>
          {selectedNode.kind === 'object' && (
            <section className="studio-object-attributes" aria-label="完整属性列表">
              <header><b>属性</b><small>{selectedObjectAttributes.length} 项</small></header>
              {(() => { if (session.detailLoading && !session.details?.[selectedNode.id]) return (
                <div className="studio-object-attributes-state">正在读取属性…</div>
              ); return selectedObjectAttributes.length ? (
                <div className="studio-object-attribute-list">
                  {selectedObjectAttributes.map((field, index) => {
                    const key = field.key || field.name || `field_${index + 1}`;
                    const label = field.cnName || field.label || `未命名属性 ${index + 1}`;
                    return <div className="studio-object-attribute" key={`${key}:${index}`} title={label}>
                      <b>{label}</b>
                    </div>;
                  })}
                </div>
              ) : <div className="studio-object-attributes-state">暂无属性</div>; })()}
            </section>
          )}
          <button type="button" className="btn btn-p" onClick={() => onOpen(selectedNode.id, 'graph')}>
            <PanelRightOpen size={14} />打开条目
          </button>
          </aside>
        )}
        </div>
      </div>
    </div>
  );
}

const SCROLL_MINIMAP_WIDTH = 190;
const SCROLL_MINIMAP_HEIGHT = 126;
const SCROLL_MINIMAP_PADDING = 8;

function StudioGraphScrollMiniMap({ nodes, routes, zoom, surface, viewport, scroll, onNavigate }) {
  const projection = useMemo(() => {
    const width = Math.max(1, surface.width || 1);
    const height = Math.max(1, surface.height || 1);
    const scale = Math.min(
      (SCROLL_MINIMAP_WIDTH - SCROLL_MINIMAP_PADDING * 2) / width,
      (SCROLL_MINIMAP_HEIGHT - SCROLL_MINIMAP_PADDING * 2) / height,
    );
    const offsetX = (SCROLL_MINIMAP_WIDTH - width * scale) / 2;
    const offsetY = (SCROLL_MINIMAP_HEIGHT - height * scale) / 2;
    return { width, height, scale, offsetX, offsetY };
  }, [surface.height, surface.width]);

  const navigate = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const localX = (event.clientX - rect.left) * (SCROLL_MINIMAP_WIDTH / rect.width);
    const localY = (event.clientY - rect.top) * (SCROLL_MINIMAP_HEIGHT / rect.height);
    onNavigate(
      (localX - projection.offsetX) / projection.scale,
      (localY - projection.offsetY) / projection.scale,
    );
  };

  return (
    <div className="studio-graph-scroll-minimap">
      <svg viewBox={`0 0 ${SCROLL_MINIMAP_WIDTH} ${SCROLL_MINIMAP_HEIGHT}`}
        role="img" aria-label="主图谱小地图导航"
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          navigate(event);
        }}
        onPointerMove={(event) => {
          if (event.buttons === 1) navigate(event);
        }}>
        <rect x="0" y="0" width={SCROLL_MINIMAP_WIDTH} height={SCROLL_MINIMAP_HEIGHT}
          className="studio-graph-scroll-minimap-bg" />
        {Object.entries(routes).map(([edgeId, points]) => points.length > 1 && (
          <polyline key={edgeId} points={points.map((point) => {
            const x = (point.x * zoom + surface.viewport.x) * projection.scale + projection.offsetX;
            const y = (point.y * zoom + surface.viewport.y) * projection.scale + projection.offsetY;
            return `${x},${y}`;
          }).join(' ')} className="studio-graph-scroll-minimap-edge" />
        ))}
        {nodes.map((node) => {
          const nodeWidth = Number(node.measured?.width ?? node.width ?? node.style?.width) || 160;
          const nodeHeight = Number(node.measured?.height ?? node.height ?? node.style?.height) || 72;
          const x = ((Number(node.position?.x) || 0) * zoom + surface.viewport.x) * projection.scale + projection.offsetX;
          const y = ((Number(node.position?.y) || 0) * zoom + surface.viewport.y) * projection.scale + projection.offsetY;
          return <rect key={node.id} x={x} y={y}
            width={Math.max(1.5, nodeWidth * zoom * projection.scale)}
            height={Math.max(1.5, nodeHeight * zoom * projection.scale)} rx="1"
            fill={KIND_META[node.data?.kind]?.color || '#98a2b1'} />;
        })}
        <rect
          x={scroll.left * projection.scale + projection.offsetX}
          y={scroll.top * projection.scale + projection.offsetY}
          width={Math.min(projection.width, viewport.width || 0) * projection.scale}
          height={Math.min(projection.height, viewport.height || 0) * projection.scale}
          className="studio-graph-scroll-minimap-viewport" />
      </svg>
    </div>
  );
}

const StudioNode = memo(({ data }) => {
  const Icon = KIND_ICON[data.kind] || Boxes;
  const meta = studioGraphKindMeta(data.kind);
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
      <strong title={data.label}>{data.label}</strong>
      <Handle id="target-right" type="target" position={Position.Right} isConnectable={false} />
      <Handle id="source-right" type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
});

const StudioEdge = memo(({ id, sourceX, sourceY, targetX, targetY, markerEnd, data }) => {
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
        opacity: (() => { if (dimmed) return 0.1; return emphasized ? 1 : 0.72; })(),
      }} />
    {Array.from({ length: data.particleCount || 0 }, (_, index) => {
      const begin = -(particleDuration / data.particleCount) * index;
      return <circle key={`${id}:particle:${index}`} className="studio-edge-particle"
        r={index ? 2.5 : 3} fill={data.color} aria-hidden="true">
        <animateMotion path={path} dur={`${particleDuration}s`} begin={`${begin}s`} repeatCount="indefinite" />
        <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.12;0.84;1"
          dur={`${particleDuration}s`} begin={`${begin}s`} repeatCount="indefinite" />
      </circle>;
    })}
    {data.label && (
      <EdgeLabelRenderer>
        <span className={`studio-edge-label${data.active ? ' active' : ''}${data.hovered ? ' hovered' : ''}`}
          style={{ color: data.labelColor, opacity: (() => { if (dimmed) return 0.58; return emphasized ? 1 : 0.94; })(), transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}>
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

function studioForceStatusLabel(status) {
  if (status === 'initializing') return '正在初始化';
  if (status === 'running') return '布局计算中';
  if (status === 'paused') return '已暂停';
  if (status === 'settled') return '布局已稳定 · 环境动态';
  if (status === 'error') return '布局不可用';
  return '等待启动';
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
