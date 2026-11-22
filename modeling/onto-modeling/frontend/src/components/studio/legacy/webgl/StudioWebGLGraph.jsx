import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import { LocateFixed, Minus, Pin, PinOff, Plus, RotateCw } from 'lucide-react';
import { ThreeGraphRuntime as WebGLGraphRuntime } from './StudioThreeGraphRuntime.js';
import './studio-three-ux.css';

const GUIDE_STORAGE_KEY = 'studio.graph.3d.guide.v1';
function needsGraphGuide() {
  try { return localStorage.getItem(GUIDE_STORAGE_KEY) !== 'dismissed'; } catch { return true; }
}

const StudioWebGLGraph = forwardRef(({
  nodes,
  edges,
  routes,
  layoutMode,
  onLayoutModeChange,
  onExpandAll,
  clustered,
  allLabels = false,
  onAllLabelsChange,
  selectedId,
  activeNodeIds,
  activeEdgeIds,
  hasHighlight,
  previewKind = '',
  motionEnabled = true,
  reducedMotion = false,
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
}, ref) => {
  const hostRef = useRef(null);
  const minimapRef = useRef(null);
  const runtimeRef = useRef(null);
  const selectedIdRef = useRef(selectedId);
  const initialViewportAppliedRef = useRef(false);
  const [ready, setReady] = useState(false);
  const [presented, setPresented] = useState(false);
  const [rendererName, setRendererName] = useState('WebGL');
  const [pinned, setPinned] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [minimapExpanded, setMinimapExpanded] = useState(false);
  const [minimapHover, setMinimapHover] = useState('');
  const [showGuide, setShowGuide] = useState(needsGraphGuide);
  const [presentation, setPresentation] = useState({ bloom: true, particles: false });
  selectedIdRef.current = selectedId;

  useEffect(() => {
    let active = true;
    const runtime = new WebGLGraphRuntime({
      onReady: (name) => {
        setRendererName(name);
        setReady(true);
      },
      onScenePresented: () => {
        if (active) setPresented(true);
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
      onMinimapHover: (name) => { if (active) setMinimapHover(name); },
      onAutoRotateChange: (value) => { if (active) setRotating(value); },
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
      previewKind,
      motionEnabled,
      reducedMotion,
    });
    setPinned(runtimeRef.current?.isPinned(selectedId) || false);
  }, [activeEdgeIds, activeNodeIds, hasHighlight, previewKind, motionEnabled, ready, reducedMotion, selectedId]);

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
    if (ready) runtimeRef.current?.setPresentation({ ...presentation, labels: allLabels });
  }, [allLabels, presentation, ready]);

  useEffect(() => {
    if (!ready || !nodes.length || initialViewportAppliedRef.current) return;
    initialViewportAppliedRef.current = true;
    if (initialViewport) runtimeRef.current?.setViewport(initialViewport);
  }, [initialViewport, nodes.length, ready]);

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
  const togglePresentation = (key) => setPresentation((current) => ({ ...current, [key]: !current[key] }));
  const toggleOrbit = () => setRotating(runtimeRef.current?.setAutoRotate(!rotating) || false);
  const dismissGuide = () => {
    setShowGuide(false);
    try { localStorage.setItem(GUIDE_STORAGE_KEY, 'dismissed'); } catch { /* The guide still closes when storage is unavailable. */ }
  };

  return (
    <div className="studio-webgl-graph" data-renderer={rendererName} data-layout-mode={layoutMode}>
      <div ref={hostRef} className="studio-webgl-stage" />
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
      <div className={`studio-three-minimap-panel${minimapExpanded ? ' is-expanded' : ''}`}>
        <canvas ref={minimapRef} className="studio-webgl-minimap" aria-label="图谱小地图，点击可导航；选中节点显示强调色圆环" />
        <button type="button" className="studio-three-minimap-size" aria-label={minimapExpanded ? '缩小小地图' : '放大小地图至两倍'} aria-pressed={minimapExpanded}
          onClick={() => setMinimapExpanded((value) => !value)}>{minimapExpanded ? '1×' : '2×'}</button>
        {minimapHover && <span className="studio-three-minimap-tooltip" role="status">{minimapHover}</span>}
      </div>
      {showGuide && presented && <aside className="studio-three-guide" aria-label="图谱操作引导">
        <strong>探索 3D 图谱</strong>
        <ol><li>拖动空白旋转，右键拖动平移。</li><li>滚轮缩放；找不到图时点“适配视图”。</li><li>单击选中看关联，双击节点进入编辑。</li></ol>
        <button type="button" onClick={dismissGuide}>知道了</button>
      </aside>}
      {!presented && <div className="studio-webgl-loading">正在初始化 WebGL 渲染器</div>}
    </div>
  );
});

export default StudioWebGLGraph;
