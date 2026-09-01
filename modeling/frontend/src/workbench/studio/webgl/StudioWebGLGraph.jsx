import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import { LocateFixed, Minus, Pin, PinOff, Plus } from 'lucide-react';
import { WebGLGraphRuntime } from './StudioWebGLRuntime.js';

const StudioWebGLGraph = forwardRef(function StudioWebGLGraph({
  nodes,
  edges,
  routes,
  layoutMode,
  selectedId,
  activeNodeIds,
  activeEdgeIds,
  hasHighlight,
  motionEnabled,
  reducedMotion,
  initialViewport,
  onNodeClick,
  onNodeDoubleClick,
  onNodeDragEnd,
  onBackgroundClick,
  onEdgeHover,
  onForceStatusChange,
  onForceError,
  onUnsupported,
  onZoomTierChange,
  onViewportChange,
}, ref) {
  const hostRef = useRef(null);
  const minimapRef = useRef(null);
  const runtimeRef = useRef(null);
  const selectedIdRef = useRef(selectedId);
  const initialViewportAppliedRef = useRef(false);
  const [ready, setReady] = useState(false);
  const [rendererName, setRendererName] = useState('WebGL');
  const [pinned, setPinned] = useState(false);
  selectedIdRef.current = selectedId;

  useEffect(() => {
    let active = true;
    const runtime = new WebGLGraphRuntime({
      onReady: (name) => {
        setRendererName(name);
        setReady(true);
      },
      onNodeClick,
      onNodeDoubleClick,
      onNodeDragEnd,
      onBackgroundClick,
      onEdgeHover,
      onForceStatusChange,
      onForceError,
      onPinChange: (nodeId, value) => {
        if (selectedIdRef.current === nodeId) setPinned(value);
      },
      onContextLost: onUnsupported,
      onZoomTierChange,
    });
    runtimeRef.current = runtime;
    runtime.mount(hostRef.current, minimapRef.current).catch(() => {
      if (active) onUnsupported?.();
    });
    return () => {
      active = false;
      runtime.destroy();
      runtimeRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!runtimeRef.current) return;
    Object.assign(runtimeRef.current.callbacks, {
      onNodeClick,
      onNodeDoubleClick,
      onNodeDragEnd,
      onBackgroundClick,
      onEdgeHover,
      onForceStatusChange,
      onForceError,
      onContextLost: onUnsupported,
      onZoomTierChange,
      onViewportChange,
    });
  }, [onBackgroundClick, onEdgeHover, onForceError, onForceStatusChange, onNodeClick,
    onNodeDoubleClick, onNodeDragEnd, onUnsupported, onViewportChange, onZoomTierChange]);

  useEffect(() => {
    if (!ready) return;
    runtimeRef.current?.setScene({ nodes, edges, routes });
  }, [edges, nodes, ready, routes]);

  useEffect(() => {
    if (!ready) return;
    runtimeRef.current?.setInteractionState({
      selectedId,
      activeNodeIds,
      activeEdgeIds,
      hasHighlight,
      motionEnabled,
      reducedMotion,
    });
    setPinned(runtimeRef.current?.isPinned(selectedId) || false);
  }, [activeEdgeIds, activeNodeIds, hasHighlight, motionEnabled, ready, reducedMotion, selectedId]);

  useEffect(() => {
    if (!ready) return;
    if (selectedId) runtimeRef.current?.focusNode(selectedId);
    else runtimeRef.current?.restoreSelectionView();
  }, [ready, selectedId]);

  useEffect(() => {
    if (!ready) return;
    runtimeRef.current?.setLayoutMode(layoutMode);
  }, [layoutMode, ready]);

  useEffect(() => {
    if (!ready || initialViewportAppliedRef.current) return;
    initialViewportAppliedRef.current = true;
    if (initialViewport) runtimeRef.current?.setViewport(initialViewport);
  }, [initialViewport, ready]);

  useImperativeHandle(ref, () => ({
    fitView: () => runtimeRef.current?.fitView(true),
    getViewport: () => runtimeRef.current?.getViewport(),
    setCenter: (x, y, options) => runtimeRef.current?.setCenter(x, y, options),
    focusNode: (id) => runtimeRef.current?.focusNode(id),
    restoreSelectionView: () => runtimeRef.current?.restoreSelectionView(),
    pauseForce: () => runtimeRef.current?.pauseForce(),
    resumeForce: () => runtimeRef.current?.resumeForce(),
    reheatForce: () => runtimeRef.current?.reheatForce(),
  }), []);

  const togglePin = () => {
    if (!selectedId) return;
    setPinned(runtimeRef.current?.togglePin(selectedId) || false);
  };

  return (
    <div className="studio-webgl-graph" data-renderer={rendererName} data-layout-mode={layoutMode}>
      <div ref={hostRef} className="studio-webgl-stage" />
      <span className="studio-webgl-renderer-badge"><i />{rendererName}</span>
      <div className="studio-webgl-navigation" aria-label="WebGL 图谱导航">
        {layoutMode === 'force' && selectedId && (
          <button type="button" title={pinned ? '取消固定选中节点' : '固定选中节点'} aria-label={pinned ? '取消固定选中节点' : '固定选中节点'} aria-pressed={pinned} onClick={togglePin}>
            {pinned ? <PinOff size={15} /> : <Pin size={15} />}
          </button>
        )}
        <button type="button" title="放大" aria-label="放大" onClick={() => runtimeRef.current?.zoomBy(1.2)}><Plus size={15} /></button>
        <button type="button" title="缩小" aria-label="缩小" onClick={() => runtimeRef.current?.zoomBy(1 / 1.2)}><Minus size={15} /></button>
        <button type="button" title="适配全部" aria-label="适配全部" onClick={() => runtimeRef.current?.fitView(true)}><LocateFixed size={15} /></button>
      </div>
      <canvas ref={minimapRef} className="studio-webgl-minimap" aria-label="图谱小地图，点击可导航" />
      {!ready && <div className="studio-webgl-loading">正在初始化 WebGL 渲染器</div>}
    </div>
  );
});

export default StudioWebGLGraph;
