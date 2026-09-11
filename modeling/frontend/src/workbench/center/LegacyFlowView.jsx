import dagre from '@dagrejs/dagre';
import {
  Background, BaseEdge, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider,
  addEdge, useNodesInitialized, useNodesState, useReactFlow,
} from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import './legacy-flow.css';

const NODE_KIND_LABELS = { start: '开始', end: '结束', terminal: '起止', action: '业务步骤', decision: '判断', data: '数据' };

/* 业务流程图的标签常写成「主步骤 副说明」或带括号的补充，拆成两行读起来清楚得多：
   主标题加粗，副标题小一号灰字，和参考版式一致。拆不出来就整句当主标题。 */
function splitLabel(raw) {
  const text = String(raw ?? '').trim();
  const bracket = text.match(/^(.+?)[（(]([^（()）]+)[)）]\s*$/);
  if (bracket) return { title: bracket[1].trim(), note: bracket[2].trim() };
  const broken = text.match(/^(.+?)[\n：:]\s*(.+)$/s);
  if (broken) return { title: broken[1].trim(), note: broken[2].trim() };
  return { title: text, note: '' };
}

function LegacyNode({ data, selected }) {
  const kind = NODE_KIND_LABELS[data.kind] || '业务步骤';
  const { title, note } = splitLabel(data.label);
  const ports = data.handles || [
    { type: 'target', id: 'in', position: Position.Top },
    { type: 'source', id: 'out', position: Position.Bottom },
  ];
  const shell = `fc-node is-${data.kind || 'action'}${selected || data.focused ? ' is-selected' : ''}`;
  return <div className={shell} data-kind={data.kind || 'action'}>
    {ports.map((handle) => <Handle key={handle.id} {...handle} isConnectable={!!data.editable}
      className={`fc-handle${data.editable ? ' connectable' : ''}`} />)}
    {/* 判断节点用菱形外框，文字保持水平，否则旋转后中文可读性很差 */}
    <span className="fc-node__shape" aria-hidden="true" />
    <span className="fc-node__body">
      <span className="fc-node__kind">{kind}</span>
      <strong className="fc-node__label" title={data.label}>{title}</strong>
      {note && <span className="fc-node__note">{note}</span>}
    </span>
  </div>;
}

const nodeTypes = { flow: LegacyNode };
const PORTS = {
  TB: [Position.Top, Position.Bottom], BT: [Position.Bottom, Position.Top],
  LR: [Position.Left, Position.Right], RL: [Position.Right, Position.Left],
};

function OutsideEdge({ sourceX, sourceY, targetX, targetY, data, ...props }) {
  const { horizontal, lane } = data;
  const points = horizontal
    ? [[sourceX, sourceY], [sourceX, lane], [targetX, lane], [targetX, targetY]]
    : [[sourceX, sourceY], [lane, sourceY], [lane, targetY], [targetX, targetY]];
  let path = `M ${points[0].join(' ')}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [x, y] = points[i];
    const before = points[i - 1], after = points[i + 1];
    const radius = Math.min(8, Math.hypot(x - before[0], y - before[1]) / 2,
      Math.hypot(x - after[0], y - after[1]) / 2);
    path += ` L ${x + Math.sign(before[0] - x) * radius} ${y + Math.sign(before[1] - y) * radius}`;
    path += ` Q ${x} ${y} ${x + Math.sign(after[0] - x) * radius} ${y + Math.sign(after[1] - y) * radius}`;
  }
  path += ` L ${points.at(-1).join(' ')}`;
  return <BaseEdge {...props} path={path}
    labelX={horizontal ? (sourceX + targetX) / 2 : lane}
    labelY={horizontal ? lane : (sourceY + targetY) / 2} />;
}

const edgeTypes = { outside: OutsideEdge };

function outsideRoutes(nodes, edges, direction) {
  const horizontal = ['LR', 'RL'].includes(direction);
  const axis = horizontal ? 'x' : 'y';
  const size = horizontal ? 'width' : 'height';
  const forward = ['TB', 'LR'].includes(direction) ? 1 : -1;
  const lane = Math.max(...nodes.map((node) => horizontal
    ? node.position.y + node.measured.height : node.position.x + node.measured.width));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let offset = 0;
  return Object.fromEntries(edges.flatMap((edge) => {
    const from = byId.get(edge.from), to = byId.get(edge.to);
    const start = from.position[axis] + from.measured[size] / 2;
    const end = to.position[axis] + to.measured[size] / 2;
    const skipsNode = nodes.some((node) => node !== from && node !== to
      && node.position[axis] > Math.min(start, end)
      && node.position[axis] + node.measured[size] < Math.max(start, end));
    if ((end - start) * forward > 0 && !skipsNode) return [];
    offset += 28;
    return [[edge.id, { horizontal, lane: lane + offset }]];
  }));
}

export function layoutLegacyNodes(nodes, edges, direction) {
  // Browser-measured dimensions include every wrapped line, including long Chinese
  // labels. Multigraph preserves parallel branches in the layout input.
  const layout = new dagre.graphlib.Graph({ multigraph: true });
  layout.setGraph({ rankdir: direction, ranksep: 64, nodesep: 48, marginx: 32, marginy: 32 });
  layout.setDefaultEdgeLabel(() => ({}));
  for (const node of nodes) layout.setNode(node.id, {
    width: node.measured.width, height: node.measured.height,
  });
  for (const edge of edges) layout.setEdge(edge.from, edge.to, {
    minlen: edge.minLength,
    width: edge.label ? Math.min(280, Array.from(edge.label).length * 12 + 16) : 0,
    height: edge.label ? 26 : 0,
  }, edge.id);
  dagre.layout(layout);
  return nodes.map((node) => {
    const { x, y, width, height } = layout.node(node.id);
    return { ...node, position: { x: x - width / 2, y: y - height / 2 } };
  });
}

function Canvas({ graph, focusNode, controller, onZoom, editable = false, onDraftChange }) {
  const [selection, setSelection] = useState(null);
  const [ready, setReady] = useState(false);
  const [routes, setRoutes] = useState({});
  const box = useRef(null);
  const instance = useReactFlow();
  const initialized = useNodesInitialized();
  const [nodes, setNodes, onNodesChange] = useNodesState(graph.nodes.map((node, index) => ({
    id: node.id, type: 'flow', position: { x: 32, y: index * 120 },
    data: { ...node, editable, handles: [
      { type: 'target', id: 'in', position: PORTS[graph.direction][0] },
      { type: 'source', id: 'out', position: PORTS[graph.direction][1] },
      { type: 'source', id: 'outside-out', position: ['LR', 'RL'].includes(graph.direction) ? Position.Bottom : Position.Right },
      { type: 'target', id: 'outside-in', position: ['LR', 'RL'].includes(graph.direction) ? Position.Bottom : Position.Right },
    ] },
  })));
  /* 编辑态下用户新连的边只存在前端草稿里，点「保存为候选」才走 PUT /package/diagram
     落盘（服务端带乐观锁和草稿冲突拦截）。不直接改正式包，和其它变更一样要过确认。 */
  const [draftEdges, setDraftEdges] = useState([]);
  const handleConnect = useCallback((connection) => {
    setDraftEdges((current) => addEdge({
      ...connection,
      id: `draft-${current.length + 1}-${connection.source}-${connection.target}`,
      label: '',
    }, current));
  }, []);

  const edges = useMemo(() => {
    const base = graph.edges.map((edge) => {
      const route = routes[edge.id];
      const marker = { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#8795a9' };
      return {
        id: edge.id, source: edge.from, target: edge.to,
        sourceHandle: route ? 'outside-out' : 'out', targetHandle: route ? 'outside-in' : 'in',
        label: edge.label, type: route ? 'outside' : 'smoothstep', data: route,
        pathOptions: { offset: 24, borderRadius: 8 },
        markerEnd: edge.arrow === 'arrow_open' ? undefined : marker,
        markerStart: edge.arrow === 'double_arrow_point' ? marker : undefined,
        style: { stroke: '#8795a9', strokeWidth: edge.stroke === 'thick' ? 3 : 1.5,
          strokeDasharray: edge.stroke === 'dotted' ? '5 4' : undefined },
        labelStyle: { fill: '#4f5d70', fontSize: 12 },
        labelBgStyle: { fill: '#fbfcfe', fillOpacity: 0.96 },
      };
    });
    return base.concat(draftEdges.map((edge) => ({
      ...edge,
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#f0a020' },
      style: { stroke: '#f0a020', strokeWidth: 2, strokeDasharray: '6 4' },
    })));
  }, [graph, routes, draftEdges]);

  // Reset layout only when measured sizes change, never when the user zooms or selects.
  const sizes = nodes.map((node) => `${node.id}:${node.measured?.width}:${node.measured?.height}`).join('|');
  useEffect(() => {
    if (!initialized) return;
    const laidOut = layoutLegacyNodes(instance.getNodes(), graph.edges, graph.direction);
    setNodes(laidOut);
    setRoutes(outsideRoutes(laidOut, graph.edges, graph.direction));
    setReady(true);
  }, [initialized, sizes, graph, setNodes, instance]);

  /* 只按宽度贴合会让纵向长图溢出画布：十几个节点竖排时，宽度铺满的缩放比必然让底部几节
     跑到可视区外，首屏看不到流程的结尾。高度也参与取小，两个方向都装得下再居中。 */
  const fitWidth = useCallback(() => {
    const bounds = instance.getNodesBounds(instance.getNodes());
    if (!bounds.width || !box.current) return;
    const width = box.current.clientWidth;
    const height = box.current.clientHeight;
    const byWidth = (width - 64) / bounds.width;
    const byHeight = bounds.height > 0 ? (height - 64) / bounds.height : byWidth;
    const zoom = Math.max(0.1, Math.min(1, Math.min(byWidth, byHeight)));
    void instance.setViewport({
      x: (width - bounds.width * zoom) / 2 - bounds.x * zoom,
      y: Math.max(32, (height - bounds.height * zoom) / 2) - bounds.y * zoom,
      zoom,
    });
  }, [instance]);

  useEffect(() => {
    if (!ready) return;
    const frame = requestAnimationFrame(fitWidth);
    return () => cancelAnimationFrame(frame);
  }, [ready, fitWidth]);

  useEffect(() => {
    if (onDraftChange) onDraftChange({ edges: draftEdges, dirty: draftEdges.length > 0 });
  }, [draftEdges, onDraftChange]);

  useEffect(() => {
    controller.current = {
      zoomIn: () => instance.zoomIn(), zoomOut: () => instance.zoomOut(),
      fit: fitWidth, overview: () => instance.fitView({ padding: 0.15, minZoom: 0.05, maxZoom: 1 }),
      reset: () => instance.zoomTo(1),
    };
    return () => { controller.current = null; };
  }, [controller, instance, fitWidth]);

  useEffect(() => {
    setNodes((current) => current.map((node) => ({ ...node,
      selected: selection === node.id,
      data: { ...node.data, focused: focusNode === node.id },
    })));
  }, [focusNode, selection, setNodes]);

  useEffect(() => {
    if (!ready || !focusNode) return;
    const frame = requestAnimationFrame(() => {
      const node = instance.getNode(focusNode);
      if (node) void instance.setCenter(node.position.x + node.measured.width / 2,
        node.position.y + node.measured.height / 2, { zoom: instance.getZoom(), duration: 180 });
    });
    return () => cancelAnimationFrame(frame);
  }, [focusNode, ready, instance]);

  const selected = graph.nodes.find((node) => node.id === selection);
  return <div className="wb-legacy-flow" ref={box} aria-label="业务流程图" data-layout-ready={ready}>
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodesChange={onNodesChange}
      onMove={(_, viewport) => onZoom(viewport.zoom)}
      onNodeClick={(_, node) => setSelection(node.id)} onPaneClick={() => setSelection(null)}
      onConnect={editable ? handleConnect : undefined}
      nodesDraggable={editable} nodesConnectable={editable} edgesReconnectable={false}
      deleteKeyCode={null} minZoom={0.05} maxZoom={1.8} proOptions={{ hideAttribution: true }}>
      <Background color="#dfe4eb" gap={20} size={1} />
    </ReactFlow>
    <div className="wb-legacy-legend" aria-label="流程图图例">
      <span className="is-terminal"><i />起止</span>
      <span><i />业务步骤</span>
      <span className="is-decision"><i />判断</span>
      <span className="is-data"><i />数据</span>
    </div>
    {selected && <aside className="wb-legacy-inspector" aria-label="节点属性">
      <button type="button" aria-label="关闭节点属性" onClick={() => setSelection(null)}>×</button>
      <small>{NODE_KIND_LABELS[selected.kind] || '起止'}</small>
      <strong>{selected.label}</strong>
    </aside>}
  </div>;
}

export default function LegacyFlowView(props) {
  return <ReactFlowProvider><Canvas {...props} /></ReactFlowProvider>;
}
