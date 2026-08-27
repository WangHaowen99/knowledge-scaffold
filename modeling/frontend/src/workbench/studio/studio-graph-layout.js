import ELK from 'elkjs/lib/elk-api.js';
import elkWorkerUrl from 'elkjs/lib/elk-worker.min.js?url';

import { separateStudioReciprocalRoutes } from './studio-graph-geometry.js';

const NODE_SIZES = {
  object: { width: 194, height: 82 },
  operation: { width: 184, height: 72 },
  rule: { width: 174, height: 70 },
  metric: { width: 164, height: 62 },
  concept: { width: 170, height: 68 },
};
const DEFAULT_NODE_SIZE = { width: 178, height: 72 };
const elk = typeof Worker === 'function' ? new ELK({ workerUrl: elkWorkerUrl }) : null;

export async function layoutStudioGraph(nodes, edges) {
  const sizedNodes = nodes.map(withNodeSize);
  if (!elk) return { nodes: fallbackStudioLayout(sizedNodes), routes: {} };
  const graph = await elk.layout({
    id: 'studio-root',
    layoutOptions: studioLayoutOptions(sizedNodes.length),
    children: sizedNodes.map((node) => ({ id: node.id, width: node.width, height: node.height })),
    edges: edges.map((edge) => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  });
  const result = projectResult(sizedNodes, graph);
  return { ...result, routes: separateStudioReciprocalRoutes(result.routes, edges) };
}

export function fallbackStudioLayout(nodes) {
  const sizedNodes = nodes.map(withNodeSize);
  const hasBackendPositions = sizedNodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y));
  if (hasBackendPositions) {
    return sizedNodes.map((node) => ({ ...node, position: { x: node.x, y: node.y } }));
  }
  const columns = Math.max(1, Math.ceil(Math.sqrt(sizedNodes.length)));
  const columnWidths = Array.from({ length: columns }, () => 0);
  const rowHeights = [];
  sizedNodes.forEach((node, index) => {
    const column = index % columns;
    const row = Math.floor(index / columns);
    columnWidths[column] = Math.max(columnWidths[column], node.width);
    rowHeights[row] = Math.max(rowHeights[row] || 0, node.height);
  });
  const columnOffsets = offsets(columnWidths, 52);
  const rowOffsets = offsets(rowHeights, 48);
  return sizedNodes.map((node, index) => ({
    ...node,
    position: {
      x: columnOffsets[index % columns],
      y: rowOffsets[Math.floor(index / columns)],
    },
  }));
}

export function studioNodeSize(node) {
  const base = NODE_SIZES[node.kind] || DEFAULT_NODE_SIZE;
  const titleUnits = Array.from(node.label || '').length;
  const keyUnits = Math.ceil(String(node.id || '').length * 0.45);
  return {
    width: base.width + Math.min(30, Math.max(0, Math.max(titleUnits, keyUnits) - 8) * 2),
    height: base.height,
  };
}

export function studioLayoutOptions(nodeCount) {
  const layerBound = studioLayerBound(nodeCount);
  return {
    'elk.algorithm': 'layered',
    'elk.direction': 'RIGHT',
    'elk.edgeRouting': 'ORTHOGONAL',
    'elk.aspectRatio': '1.8',
    'elk.padding': '[top=36,left=36,bottom=36,right=36]',
    'elk.separateConnectedComponents': 'true',
    'elk.spacing.componentComponent': '80',
    'elk.spacing.nodeNode': '68',
    'elk.spacing.edgeNode': '38',
    'elk.spacing.edgeEdge': '18',
    'elk.layered.spacing.nodeNodeBetweenLayers': '140',
    'elk.layered.spacing.edgeNodeBetweenLayers': '32',
    'elk.layered.spacing.edgeEdgeBetweenLayers': '18',
    'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
    'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
    'elk.layered.nodePlacement.favorStraightEdges': 'false',
    'elk.layered.nodePlacement.bk.edgeStraightening': 'NONE',
    'elk.layered.considerModelOrder.longEdgeStrategy': 'DUMMY_NODE_UNDER',
    'elk.layered.unnecessaryBendpoints': 'true',
    'elk.layered.mergeEdges': 'false',
    'elk.layered.thoroughness': '8',
    ...(layerBound ? {
      'elk.layered.layering.strategy': 'COFFMAN_GRAHAM',
      'elk.layered.layering.coffmanGraham.layerBound': String(layerBound),
    } : {}),
  };
}

export function studioLayerBound(nodeCount) {
  if (!Number.isFinite(nodeCount) || nodeCount <= 10) return null;
  return Math.max(4, Math.min(8, Math.ceil(Math.sqrt(nodeCount) / 2)));
}

function withNodeSize(node) {
  return { ...node, ...studioNodeSize(node) };
}

function offsets(sizes, gap) {
  let cursor = 0;
  return sizes.map((size) => {
    const value = cursor;
    cursor += size + gap;
    return value;
  });
}

function projectResult(sourceNodes, graph) {
  const sourceById = new Map(sourceNodes.map((node) => [node.id, node]));
  return {
    nodes: (graph.children || []).map((node) => ({
      ...sourceById.get(node.id),
      width: Math.round(node.width || sourceById.get(node.id)?.width || 0),
      height: Math.round(node.height || sourceById.get(node.id)?.height || 0),
      position: { x: Math.round(node.x || 0), y: Math.round(node.y || 0) },
    })),
    routes: Object.fromEntries((graph.edges || []).map((edge) => [edge.id, edgeRoutePoints(edge)])),
  };
}

function edgeRoutePoints(edge) {
  const section = edge.sections?.[0];
  if (!section) return [];
  return [section.startPoint, ...(section.bendPoints || []), section.endPoint]
    .map((point) => ({ x: Math.round(point.x), y: Math.round(point.y) }));
}
