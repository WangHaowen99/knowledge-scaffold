import { memo, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  GitBranch,
  ListOrdered,
  Maximize2,
  Minimize2,
  PanelRightOpen,
  RefreshCw,
  RotateCcw,
  Workflow,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
} from '@xyflow/react';

import { getStudioOperationFlow, studioError } from '../studio-api.js';
import {
  operationFlowMode,
  operationFlowScrollCanvas,
  operationFlowWheelZoom,
  operationFlowZoom,
  operationStepsDiagram,
  projectOperationFlowDiagram,
} from './operationFlowModel.js';

export default function OperationFlowPanel({
  projectId,
  operation,
  workingRevision,
  onClose,
  onOpen,
}) {
  const requestRef = useRef(0);
  const [state, setState] = useState({ phase: 'loading', data: null, error: null });
  const [mode, setMode] = useState('empty');
  const [activeDiagramId, setActiveDiagramId] = useState('');
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setState({ phase: 'loading', data: null, error: null });
    void getStudioOperationFlow(projectId, operation.id, workingRevision, { signal: controller.signal })
      .then((data) => {
        if (requestRef.current !== requestId
          || data.operationId !== operation.id
          || data.workingRevision !== workingRevision) return;
        const nextMode = operationFlowMode(data);
        setState({ phase: 'ready', data, error: null });
        setMode(nextMode);
        setActiveDiagramId(data.diagrams?.[0]?.id || '');
      })
      .catch((error) => {
        if (controller.signal.aborted || error?.cause?.name === 'AbortError') return;
        if (requestRef.current === requestId) {
          setState({ phase: 'error', data: null, error: studioError(error) });
        }
      });
    return () => {
      controller.abort();
      requestRef.current += 1;
    };
  }, [operation.id, projectId, reloadToken, workingRevision]);

  const data = state.data;
  const diagrams = data?.diagrams || [];
  const activeDiagram = diagrams.find((item) => item.id === activeDiagramId) || diagrams[0];
  const stepsDiagram = useMemo(() => operationStepsDiagram(
    operation.id,
    operation.title,
    data?.steps || [],
  ), [data?.steps, operation.id, operation.title]);
  const diagram = mode === 'business-flow' ? activeDiagram?.document : stepsDiagram;
  const focusedNodeIds = mode === 'business-flow' ? activeDiagram?.focusedNodeIds || [] : [];
  const hasSteps = Boolean(data?.steps?.length);
  const hasBusinessFlow = diagrams.length > 0;

  return (
    <aside className="studio-operation-flow-panel" aria-label="操作流程">
      <header className="studio-operation-flow-head">
        <div className="studio-operation-flow-title">
          <i><Workflow size={16} /></i>
          <div><span>操作流程</span><strong>{operation.title}</strong><code>{operation.id}</code></div>
        </div>
        <button type="button" className="studio-operation-flow-close" title="关闭流程" aria-label="关闭操作流程"
          onClick={onClose}><X size={15} /></button>
      </header>

      {(() => { if (state.phase === 'loading') return (
        <PanelState icon={<RefreshCw className="spin" size={20} />} title="正在读取操作流程" text="正在同步当前工作修订。" />
      ); return state.phase === 'error' ? (
        <PanelState icon={<AlertCircle size={21} />} title="操作流程暂时无法读取" text={state.error?.message}
          action={<button type="button" className="btn btn-sm" onClick={() => setReloadToken((value) => value + 1)}><RefreshCw size={13} />重试</button>} />
      ) : (
        <>
          {(hasSteps || hasBusinessFlow) && (
            <div className="seg studio-operation-flow-tabs" role="tablist" aria-label="操作流程查看方式">
              {hasSteps && <button type="button" role="tab" aria-selected={mode === 'operation-steps'}
                className={(() => { if (mode === 'operation-steps') return 'on'; return ''; })()} onClick={() => setMode('operation-steps')}>
                <ListOrdered size={13} />操作步骤
              </button>}
              {hasBusinessFlow && <button type="button" role="tab" aria-selected={mode === 'business-flow'}
                className={(() => { if (mode === 'business-flow') return 'on'; return ''; })()} onClick={() => setMode('business-flow')}>
                <GitBranch size={13} />所在业务流程
              </button>}
            </div>
          )}

          {mode === 'business-flow' && diagrams.length > 1 && (
            <div className="studio-operation-flow-switcher" role="tablist" aria-label="关联业务流程">
              {diagrams.map((item) => <button type="button" role="tab" key={item.id}
                aria-selected={item.id === activeDiagram?.id} className={(() => { if (item.id === activeDiagram?.id) return 'on'; return ''; })()}
                onClick={() => setActiveDiagramId(item.id)}>{item.title}</button>)}
            </div>
          )}

          {data?.issues?.length > 0 && (
            <details className="studio-operation-flow-issues">
              <summary><AlertCircle size={13} />{data.issues.length} 项流程引用问题</summary>
              <ul>{data.issues.map((issue, index) => <li key={`${issue.code}:${issue.reference || index}`}>{issue.message}</li>)}</ul>
            </details>
          )}

          {(() => { if (diagram?.nodes?.length) return (
            <>
              <div className="studio-operation-flow-caption">
                <strong>{diagram.title || '操作流程'}</strong>
                <span>{(() => { if (mode === 'business-flow') return '展示操作所在的完整业务流程，引用节点已高亮。'; return '按操作条目中的原始顺序展示内部步骤。'; })()}</span>
              </div>
              <OperationFlowCanvas key={`${mode}:${diagram.id}`}
                diagram={diagram} focusedNodeIds={focusedNodeIds} />
            </>
          ); return (
            <PanelState icon={<Workflow size={22} />} title={(() => { if (mode === 'business-flow') return '业务流程尚未添加节点'; return '尚未补充流程'; })()}
              text={(() => { if (mode === 'business-flow') return '可以切换其他流程或查看操作步骤。'; return '可在操作条目中维护操作步骤。'; })()} />
          ); })()}
        </>
      ); })()}

      <footer className="studio-operation-flow-footer">
        <button type="button" className="btn btn-p" onClick={() => onOpen(operation.id, 'graph')}>
          <PanelRightOpen size={14} />打开操作条目
        </button>
      </footer>
    </aside>
  );
}

function OperationFlowCanvas({ diagram, focusedNodeIds }) {
  const canvasRef = useRef(null);
  const flowInstanceRef = useRef(null);
  const fallbackFullscreenRef = useRef(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(1);
  const graph = useMemo(
    () => projectOperationFlowDiagram(diagram, focusedNodeIds),
    [diagram, focusedNodeIds],
  );
  const logicalViewport = fullscreen ? {
    width: viewport.width / zoom,
    height: viewport.height / zoom,
  } : viewport;
  const displayedGraph = useMemo(
    () => fullscreen ? operationFlowScrollCanvas(graph, 56, logicalViewport) : graph,
    [fullscreen, graph, logicalViewport.height, logicalViewport.width],
  );

  useEffect(() => {
    const onFullscreenChange = () => {
      if (document.fullscreenElement === canvasRef.current) setFullscreen(true);
      else if (!fallbackFullscreenRef.current) {
        setFullscreen(false);
        setZoom(1);
      }
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange);
  }, []);

  useEffect(() => {
    if (!fullscreen || !canvasRef.current) return undefined;
    const canvas = canvasRef.current;
    const measure = () => setViewport({ width: canvas.clientWidth, height: canvas.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [fullscreen]);

  useEffect(() => {
    if (!fullscreen || !canvasRef.current) return undefined;
    const canvas = canvasRef.current;
    const handleFullscreenWheel = (event) => {
      event.preventDefault();
      setZoom((current) => operationFlowWheelZoom(current, event.deltaY));
    };
    canvas.addEventListener('wheel', handleFullscreenWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', handleFullscreenWheel);
  }, [fullscreen]);

  useEffect(() => {
    if (!fullscreen || !canvasRef.current || !displayedGraph.width || !displayedGraph.height) return undefined;
    let innerFrame = 0;
    const frame = window.requestAnimationFrame(() => {
      innerFrame = window.requestAnimationFrame(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        canvas.scrollLeft = Math.max(0, (canvas.scrollWidth - canvas.clientWidth) / 2);
        canvas.scrollTop = Math.max(0, (canvas.scrollHeight - canvas.clientHeight) / 2);
      });
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (innerFrame) window.cancelAnimationFrame(innerFrame);
    };
  }, [displayedGraph.height, displayedGraph.width, fullscreen]);

  useEffect(() => {
    if (fullscreen) return undefined;
    let innerFrame = 0;
    let settleTimer = 0;
    const resetAndFit = () => {
      const canvas = canvasRef.current;
      if (canvas) {
        canvas.scrollLeft = 0;
        canvas.scrollTop = 0;
      }
      flowInstanceRef.current?.fitView({ padding: 0.18, minZoom: 0.28, maxZoom: 1.08 });
    };
    resetAndFit();
    const frame = window.requestAnimationFrame(() => {
      innerFrame = window.requestAnimationFrame(() => {
        resetAndFit();
        settleTimer = window.setTimeout(resetAndFit, 160);
      });
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (innerFrame) window.cancelAnimationFrame(innerFrame);
      if (settleTimer) window.clearTimeout(settleTimer);
    };
  }, [diagram.id, fullscreen]);

  const toggleFullscreen = async () => {
    if (fullscreen) {
      fallbackFullscreenRef.current = false;
      if (document.fullscreenElement === canvasRef.current) await document.exitFullscreen();
      setFullscreen(false);
      setZoom(1);
      return;
    }
    setZoom(1);
    try {
      await canvasRef.current?.requestFullscreen();
      fallbackFullscreenRef.current = false;
      setFullscreen(true);
    } catch {
      fallbackFullscreenRef.current = true;
      setFullscreen(true);
    }
  };

  const changeZoom = (value) => setZoom(operationFlowZoom(value));
  const edges = useMemo(() => displayedGraph.edges.map((edge) => {
    const exception = edge.data.kind === 'exception';
    const dataFlow = edge.data.kind === 'data';
    const color = (() => { if (exception) return '#b35252'; return dataFlow ? '#397888' : '#708094'; })();
    return {
      ...edge,
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 15, height: 15 },
      style: { stroke: color, strokeWidth: 1.6, strokeDasharray: exception || dataFlow ? '5 4' : undefined },
      labelStyle: { fill: exception ? '#944141' : '#526274', fontSize: 10 },
      labelBgStyle: { fill: '#fff', fillOpacity: 0.95 },
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 3,
    };
  }), [displayedGraph.edges]);
  const surfaceStyle = fullscreen ? {
    width: displayedGraph.width,
    height: displayedGraph.height,
    transform: `scale(${zoom})`,
    transformOrigin: 'top left',
  } : undefined;
  const stageStyle = fullscreen ? {
    width: displayedGraph.width * zoom,
    height: displayedGraph.height * zoom,
    minWidth: '100%',
    minHeight: '100%',
  } : undefined;
  return (
    <div ref={canvasRef} className={`studio-operation-flow-canvas${fullscreen ? ' is-scrollable is-fullscreen' : ''}`}
      aria-label={`${diagram.title || '操作'}流程图`}>
      <button type="button" className="studio-operation-flow-fullscreen"
        title={fullscreen ? '退出全屏' : '全屏查看流程图'}
        aria-label={fullscreen ? '退出流程图全屏' : '全屏查看流程图'}
        onClick={() => void toggleFullscreen()}>
        {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
      </button>
      {fullscreen && <div className="studio-operation-flow-zoom" aria-label="流程图缩放">
        <button type="button" title="缩小" aria-label="缩小流程图" disabled={zoom <= 0.5}
          onClick={() => changeZoom(zoom - 0.25)}><ZoomOut size={16} /></button>
        <span>{Math.round(zoom * 100)}%</span>
        <button type="button" title="恢复 100%" aria-label="恢复流程图原始大小"
          onClick={() => changeZoom(1)}><RotateCcw size={15} /></button>
        <button type="button" title="放大" aria-label="放大流程图" disabled={zoom >= 2}
          onClick={() => changeZoom(zoom + 0.25)}><ZoomIn size={16} /></button>
      </div>}
      <div className="studio-operation-flow-stage" style={stageStyle}>
        <div className="studio-operation-flow-surface" style={surfaceStyle}>
          <ReactFlow key={fullscreen ? 'fullscreen-scroll' : 'panel-fit'}
            nodes={displayedGraph.nodes} edges={edges} nodeTypes={FLOW_NODE_TYPES}
            nodesDraggable={false} nodesConnectable={false} nodesFocusable={false}
            edgesFocusable={false} elementsSelectable={false} fitView={!fullscreen}
            defaultViewport={fullscreen ? { x: 0, y: 0, zoom: 1 } : undefined}
            fitViewOptions={{ padding: 0.18, minZoom: 0.28, maxZoom: 1.08 }}
            minZoom={fullscreen ? 1 : 0.2} maxZoom={fullscreen ? 1 : 1.8}
            panOnDrag={!fullscreen} zoomOnScroll={!fullscreen} zoomOnPinch={!fullscreen}
            preventScrolling={!fullscreen} zoomOnDoubleClick={false}
            onInit={(instance) => {
              flowInstanceRef.current = instance;
              if (!fullscreen) {
                window.requestAnimationFrame(() => instance.fitView({
                  padding: 0.18,
                  minZoom: 0.28,
                  maxZoom: 1.08,
                }));
              }
            }}>
            <Background gap={18} size={1} color="#e1e6ec" />
            {!fullscreen && <Controls position="bottom-right" showInteractive={false} />}
          </ReactFlow>
        </div>
      </div>
    </div>
  );
}

const OperationFlowNode = memo(({ data }) => {
  const { source, target } = handlePositions(data.direction);
  return (
    <div className={`studio-operation-flow-node is-${data.kind}${data.focused ? ' is-focused' : ''}`}
      style={{ minHeight: data.estimatedHeight }} title={data.detail || data.label}>
      <Handle type="target" position={target} isConnectable={false} />
      <span>{data.label}</span>
      <Handle type="source" position={source} isConnectable={false} />
    </div>
  );
});

const FLOW_NODE_TYPES = { operationFlow: OperationFlowNode };

function handlePositions(direction) {
  if (direction === 'BT') return { source: Position.Top, target: Position.Bottom };
  if (direction === 'RL') return { source: Position.Left, target: Position.Right };
  if (direction === 'LR') return { source: Position.Right, target: Position.Left };
  return { source: Position.Bottom, target: Position.Top };
}

function PanelState({ icon, title, text, action = null }) {
  return <div className="studio-operation-flow-state">{icon}<b>{title}</b><span>{text}</span>{action}</div>;
}
