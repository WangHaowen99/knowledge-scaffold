// 流程图画布（标准编辑器）：React Flow 负责拖动、连线、平移缩放，节点与连线用设计稿的图形自绘。
// 节点圆点拖到另一个节点连线；拖到空白处松手，在松手处新建相连的业务步骤；双击节点改名。
import { ConnectionMode, Handle, Position, ReactFlow } from '@xyflow/react';
import '@xyflow/react/dist/base.css';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button, cx } from '../../ui/index.jsx';
import { EdgeMarkers, EdgeShape, FlowLegend, NodeShape } from './FlowShapes.jsx';
import { edgeGeometry, edgeTypeFor, fitViewport, nodeSize } from './flowchartModel.js';

const SIDES = [['t', Position.Top], ['r', Position.Right], ['b', Position.Bottom], ['l', Position.Left]];

function FcNode({ id, data, selected }) {
  const { node, w, h, disabled, onRename } = data;
  const [draft, setDraft] = useState(null);
  const inputRef = useRef(null);
  useEffect(() => {
    if (draft !== null) { inputRef.current?.focus(); inputRef.current?.select(); }
  }, [draft !== null]); // eslint-disable-line react-hooks/exhaustive-deps
  const finish = (value) => {
    const label = value.trim();
    if (label && label !== node.label) onRename(id, label);
    setDraft(null);
  };
  return (
    <div
      className={`fcn t-${node.type}${selected ? ' sel' : ''}`}
      style={{ width: w, height: h }}
      onDoubleClick={(e) => { e.stopPropagation(); if (!disabled) setDraft(node.label); }}
    >
      <svg className="fcn-svg" width={w} height={h}>
        <NodeShape node={draft !== null ? { ...node, label: '' } : node} cx={w / 2} cy={h / 2} selected={selected} />
      </svg>
      {draft !== null ? (
        <input
          ref={inputRef}
          className="fcn-rename nodrag"
          aria-label="节点名称"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={(e) => finish(e.target.value)}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === 'Enter') { e.preventDefault(); finish(e.currentTarget.value); }
            if (e.key === 'Escape') { e.preventDefault(); setDraft(null); }
          }}
        />
      ) : null}
      {SIDES.map(([k, pos]) => <Handle key={k} id={k} type="source" position={pos} className="fcn-h" isConnectable={!disabled} />)}
    </div>
  );
}

function FcEdge({ data, selected }) {
  if (!data?.geo) return null;
  const { geo } = data;
  return (
    <g className="fce">
      <line x1={geo.x1} y1={geo.y1} x2={geo.x2} y2={geo.y2} stroke="transparent" strokeWidth="14" />
      <EdgeShape edge={data.edge} geo={geo} prefix={data.prefix} selected={selected} />
    </g>
  );
}

const nodeTypes = { fc: FcNode };
const edgeTypes = { fc: FcEdge };

/**
 * @param {object} p
 * @param {object} p.model FlowchartModel（已规范化）
 * @param {{type: 'node'|'edge', id: string}|null} p.selection
 * @param {(action: object) => void} p.dispatch 编辑会话的 dispatch
 * @param {boolean} p.disabled 只读：不能拖、不能连、不能改名
 * @param {string} p.fitKey 变化时重新适配视野（换图、载入最新版本）
 */
export default function FlowchartCanvas({ model, selection, dispatch, disabled = false, fitKey = '' }) {
  const prefix = `fca${useId().replace(/[^a-z0-9]/gi, '')}`;
  const wrapRef = useRef(null);
  const rf = useRef(null);
  const autoFit = useRef(true);
  const [fitted, setFitted] = useState(false);
  const [live, setLive] = useState({});
  const [connecting, setConnecting] = useState(false);
  const modelRef = useRef(model);
  modelRef.current = model;

  const fit = useCallback((duration = 0) => {
    const el = wrapRef.current;
    if (!rf.current || !el) return;
    const r = el.getBoundingClientRect();
    const vp = fitViewport(modelRef.current.nodes, r.width, r.height);
    rf.current.setViewport(vp, duration ? { duration } : undefined);
    setFitted(true);
  }, []);

  // 画布尺寸变化（锁定条出现、窗口缩放）时，没手动缩放过就重新适配
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(() => { if (autoFit.current) fit(); });
    ro.observe(el);
    return () => ro.disconnect();
  }, [fit]);

  useEffect(() => { autoFit.current = true; fit(); }, [fitKey, fit]);

  const onRename = useCallback((id, label) => dispatch({ type: 'update-node', id, patch: { label } }), [dispatch]);

  const liveNodes = useMemo(() => model.nodes.map((n) => {
    const p = live[n.id];
    if (!p) return n;
    const { w, h } = nodeSize(n);
    return { ...n, x: p.x + w / 2, y: p.y + h / 2 };
  }), [model.nodes, live]);

  const rfNodes = useMemo(() => liveNodes.map((n) => {
    const { w, h } = nodeSize(n);
    return {
      id: n.id,
      type: 'fc',
      position: { x: n.x - w / 2, y: n.y - h / 2 },
      width: w,
      height: h,
      selected: selection?.type === 'node' && selection.id === n.id,
      data: { node: n, w, h, disabled, onRename },
    };
  }), [liveNodes, selection, disabled, onRename]);

  const rfEdges = useMemo(() => {
    const geo = edgeGeometry(liveNodes, model.edges);
    return model.edges.map((e) => ({
      id: e.id,
      source: e.from,
      target: e.to,
      type: 'fc',
      selected: selection?.type === 'edge' && selection.id === e.id,
      data: { edge: e, geo: geo.get(e.id), prefix },
    }));
  }, [liveNodes, model.edges, selection, prefix]);

  const onNodesChange = useCallback((changes) => {
    const moved = changes.filter((c) => c.type === 'position' && c.position);
    if (moved.length) setLive((prev) => ({ ...prev, ...Object.fromEntries(moved.map((c) => [c.id, c.position])) }));
  }, []);

  const select = (sel) => dispatch({ type: 'select', selection: sel });

  return (
    <div className="fc-cv" ref={wrapRef}>
      <svg className="fc-defs" aria-hidden="true"><EdgeMarkers prefix={prefix} /></svg>
      <ReactFlow
        className={cx(!fitted && 'fc-unfitted', connecting && 'fc-connecting')}
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onInit={(inst) => { rf.current = inst; fit(); }}
        onNodesChange={onNodesChange}
        onNodeClick={(_, n) => select({ type: 'node', id: n.id })}
        onEdgeClick={(_, e) => select({ type: 'edge', id: e.id })}
        onPaneClick={() => select(null)}
        onNodeDragStart={(_, n) => select({ type: 'node', id: n.id })}
        onNodeDragStop={(_, n) => {
          const node = modelRef.current.nodes.find((x) => x.id === n.id);
          if (node) {
            const { w, h } = nodeSize(node);
            dispatch({ type: 'move-node', id: n.id, position: { x: n.position.x + w / 2, y: n.position.y + h / 2 } });
          }
          setLive({});
        }}
        onMoveStart={(e) => { if (e) autoFit.current = false; }}
        isValidConnection={(c) => c.source !== c.target && !model.edges.some((e) => e.from === c.source && e.to === c.target)}
        onConnect={(c) => {
          const from = model.nodes.find((n) => n.id === c.source);
          const to = model.nodes.find((n) => n.id === c.target);
          dispatch({ type: 'connect', from: c.source, to: c.target, edge: { type: edgeTypeFor(from, to) } });
        }}
        onConnectStart={() => setConnecting(true)}
        onConnectEnd={(event, st) => {
          setConnecting(false);
          if (disabled || st?.isValid || !st?.fromNode) return;
          if (!event.target?.classList?.contains('react-flow__pane')) return;
          const pt = 'changedTouches' in event ? event.changedTouches[0] : event;
          const pos = rf.current?.screenToFlowPosition({ x: pt.clientX, y: pt.clientY });
          if (!pos) return;
          const from = modelRef.current.nodes.find((n) => n.id === st.fromNode.id);
          dispatch({ type: 'spawn-connected', from: st.fromNode.id, position: pos, edge: { type: edgeTypeFor(from, null) } });
        }}
        nodesDraggable={!disabled}
        nodesConnectable={!disabled}
        edgesReconnectable={false}
        elementsSelectable
        selectionKeyCode={null}
        multiSelectionKeyCode={null}
        deleteKeyCode={null}
        connectionMode={ConnectionMode.Loose}
        connectionRadius={28}
        minZoom={0.3}
        maxZoom={1.8}
        zoomOnDoubleClick={false}
        proOptions={{ hideAttribution: true }}
      />
      <FlowLegend />
      <div className="fc-zoom">
        <Button icon="zoomin" kind="ghost" size="sm" title="放大" onClick={() => { autoFit.current = false; rf.current?.zoomIn({ duration: 150 }); }} />
        <Button icon="zoomout" kind="ghost" size="sm" title="缩小" onClick={() => { autoFit.current = false; rf.current?.zoomOut({ duration: 150 }); }} />
        <Button icon="maximize" kind="ghost" size="sm" title="适应画布" onClick={() => { autoFit.current = true; fit(180); }} />
      </div>
    </div>
  );
}
