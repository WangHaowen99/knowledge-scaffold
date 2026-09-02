import dagre from '@dagrejs/dagre';

const DEFAULT_NODE_WIDTH = 172;
const DEFAULT_NODE_HEIGHT = 56;
const NODE_HORIZONTAL_PADDING = 24;
const NODE_LINE_HEIGHT = 17;
const NODE_VERTICAL_PADDING = 20;

export function operationFlowMode(flow) {
  if (flow?.defaultView === 'business-flow' && flow.diagrams?.length) return 'business-flow';
  if (flow?.steps?.length) return 'operation-steps';
  if (flow?.diagrams?.length) return 'business-flow';
  return 'empty';
}

export function operationStepsDiagram(operationId, operationTitle, steps) {
  if (!Array.isArray(steps) || !steps.length) return null;
  const nodes = [
    { id: 'start', label: '开始', kind: 'start' },
    ...steps.map((label, index) => ({ id: `step-${index + 1}`, label, kind: 'action' })),
    { id: 'end', label: '完成', kind: 'end' },
  ];
  return {
    id: `steps-${operationId}`,
    title: `${operationTitle} · 操作步骤`,
    nodes,
    edges: nodes.slice(0, -1).map((node, index) => ({
      id: `${node.id}:${nodes[index + 1].id}`,
      from: node.id,
      to: nodes[index + 1].id,
      kind: 'sequence',
    })),
    layout: { direction: 'TB', positions: {} },
  };
}

export function projectOperationFlowDiagram(diagram, focusedNodeIds = []) {
  if (!diagram) return { direction: 'TB', nodes: [], edges: [] };
  const direction = diagram.layout?.direction || 'TB';
  const positions = diagramPositions(diagram, direction);
  const focused = new Set(focusedNodeIds);
  return {
    direction,
    nodes: (diagram.nodes || []).map((node) => {
      const dimensions = nodeDimensions(node);
      return {
        id: node.id,
        type: 'operationFlow',
        position: positions[node.id] || { x: 0, y: 0 },
        data: {
          label: node.label || '未命名节点',
          detail: node.detail || '',
          kind: node.kind || 'action',
          direction,
          focused: focused.has(node.id),
          estimatedHeight: dimensions.height,
        },
        style: { width: dimensions.width },
      };
    }),
    edges: (diagram.edges || []).map((edge) => ({
      id: edge.id,
      source: edge.from,
      target: edge.to,
      type: 'smoothstep',
      label: displayEdgeLabel(edge) || undefined,
      data: { kind: edge.kind || 'sequence' },
    })),
  };
}

export function operationFlowScrollCanvas(graph, padding = 56, viewport = {}) {
  if (!graph?.nodes?.length) return { ...graph, width: 0, height: 0 };
  const boxes = graph.nodes.map((node) => ({
    node,
    left: node.position?.x || 0,
    top: node.position?.y || 0,
    right: (node.position?.x || 0) + (Number(node.style?.width) || DEFAULT_NODE_WIDTH),
    bottom: (node.position?.y || 0) + (Number(node.data?.estimatedHeight) || DEFAULT_NODE_HEIGHT),
  }));
  const minX = Math.min(...boxes.map((box) => box.left));
  const minY = Math.min(...boxes.map((box) => box.top));
  const maxX = Math.max(...boxes.map((box) => box.right));
  const maxY = Math.max(...boxes.map((box) => box.bottom));
  const contentWidth = maxX - minX;
  const contentHeight = maxY - minY;
  const width = Math.max(contentWidth + padding * 2, Number(viewport.width) || 0);
  const height = Math.max(contentHeight + padding * 2, Number(viewport.height) || 0);
  const offsetX = (width - contentWidth) / 2;
  const offsetY = (height - contentHeight) / 2;
  return {
    ...graph,
    nodes: boxes.map(({ node }) => ({
      ...node,
      position: {
        x: (node.position?.x || 0) - minX + offsetX,
        y: (node.position?.y || 0) - minY + offsetY,
      },
    })),
    width: Math.ceil(width),
    height: Math.ceil(height),
  };
}

function diagramPositions(diagram, direction) {
  const saved = diagram.layout?.positions || {};
  if (savedPositionsAreUsable(diagram.nodes || [], saved)) return saved;
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: direction, ranksep: 54, nodesep: 34, marginx: 30, marginy: 30 });
  for (const node of diagram.nodes || []) {
    const dimensions = nodeDimensions(node);
    graph.setNode(node.id, { width: dimensions.width, height: dimensions.height });
  }
  const nodeIds = new Set((diagram.nodes || []).map((node) => node.id));
  for (const edge of diagram.edges || []) {
    if (nodeIds.has(edge.from) && nodeIds.has(edge.to)) graph.setEdge(edge.from, edge.to);
  }
  dagre.layout(graph);
  return Object.fromEntries((diagram.nodes || []).map((node) => {
    const point = graph.node(node.id) || { x: 0, y: 0 };
    const dimensions = nodeDimensions(node);
    return [node.id, { x: point.x - dimensions.width / 2, y: point.y - dimensions.height / 2 }];
  }));
}

function nodeDimensions(node) {
  const label = String(node.label || '未命名节点');
  const width = labelUnits(label) > 30 ? 224 : labelUnits(label) > 18 ? 196
    : node.kind === 'decision' ? 176 : DEFAULT_NODE_WIDTH;
  const unitsPerLine = Math.max(8, Math.floor((width - NODE_HORIZONTAL_PADDING) / 12));
  const lines = label.split(/\r?\n/u).reduce((total, line) => (
    total + Math.max(1, Math.ceil(labelUnits(line) / unitsPerLine))
  ), 0);
  return {
    width,
    height: Math.max(
      node.kind === 'decision' ? 72 : DEFAULT_NODE_HEIGHT,
      NODE_VERTICAL_PADDING + lines * NODE_LINE_HEIGHT,
    ),
  };
}

function savedPositionsAreUsable(nodes, positions) {
  if (!nodes.length || !nodes.every((node) => validPosition(positions[node.id]))) return false;
  const boxes = nodes.map((node) => {
    const position = positions[node.id];
    const dimensions = nodeDimensions(node);
    return {
      left: position.x,
      top: position.y,
      right: position.x + dimensions.width,
      bottom: position.y + dimensions.height,
    };
  });
  return boxes.every((box, index) => boxes.slice(index + 1).every((other) => (
    box.right + 16 <= other.left
    || other.right + 16 <= box.left
    || box.bottom + 16 <= other.top
    || other.bottom + 16 <= box.top
  )));
}

function labelUnits(value) {
  return [...String(value || '')].reduce((total, character) => (
    total + (/\s/u.test(character) ? 0.35 : /[\u0000-\u00ff]/u.test(character) ? 0.58 : 1)
  ), 0);
}

function validPosition(value) {
  return Number.isFinite(value?.x) && Number.isFinite(value?.y);
}

function displayEdgeLabel(edge) {
  return [...new Set([edge.label, edge.conditionText].map((value) => String(value || '').trim()).filter(Boolean))]
    .join(' · ');
}
