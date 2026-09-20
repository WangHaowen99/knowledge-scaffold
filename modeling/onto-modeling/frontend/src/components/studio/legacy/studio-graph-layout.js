import ELK from 'elkjs/lib/elk-api.js';
import elkWorkerUrl from 'elkjs/lib/elk-worker.min.js?url';

import { separateStudioReciprocalRoutes } from './studio-graph-geometry.js';

const NODE_SIZES = {
  object: { width: 148, height: 148 },
  operation: { width: 120, height: 72 },
  rule: { width: 114, height: 68 },
  metric: { width: 118, height: 62 },
  script: { width: 106, height: 68 },
  cluster: { width: 170, height: 170 },
};
const NODE_MAX_WIDTHS = {
  object: 180,
  operation: 180,
  rule: 170,
  metric: 180,
  script: 160,
  cluster: 240,
};
const DEFAULT_NODE_SIZE = { width: 106, height: 66 };
const DEFAULT_NODE_MAX_WIDTH = 160;
const NODE_TITLE_FONT_SIZE = 15;
const NODE_TITLE_WIDTH_RATIO = 0.72;
const NODE_TITLE_LINE_HEIGHT = 18;
let elk = null;

function studioElk() {
  if (typeof Worker !== 'function') return null;
  if (!elk) elk = new ELK({ workerUrl: elkWorkerUrl });
  return elk;
}

export async function layoutStudioGraph(nodes, edges, sizingEdges = edges) {
  const degreeById = studioDegreeById(nodes, sizingEdges);
  const sizedNodes = nodes.map((node) => withNodeSize(node, degreeById.get(node.id) || 0));
  const engine = studioElk();
  if (!engine) return { nodes: fallbackStudioLayout(sizedNodes), routes: {} };
  const graph = await engine.layout({
    id: 'studio-root',
    layoutOptions: studioLayoutOptions(sizedNodes.length),
    children: sizedNodes.map((node) => ({ id: node.id, width: node.width, height: node.height })),
    edges: edges.map((edge) => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  });
  const result = projectResult(sizedNodes, graph);
  return { ...result, routes: separateStudioReciprocalRoutes(result.routes, edges) };
}

export function fallbackStudioLayout(nodes, sizingEdges = []) {
  const degreeById = studioDegreeById(nodes, sizingEdges);
  const sizedNodes = nodes.map((node) => withNodeSize(node, degreeById.get(node.id) || 0));
  const hasBackendPositions = studioBackendPositionsUsable(sizedNodes);
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

export function studioBackendPositionsUsable(nodes) {
  if (!nodes.every((node) => Number.isFinite(node.x) && Number.isFinite(node.y))) return false;
  if (nodes.length <= 1) return true;
  const unique = new Set(nodes.map((node) => `${node.x}:${node.y}`));
  if (unique.size !== nodes.length) return false;
  if (nodes.length < 4) return true;
  const xs = nodes.map((node) => node.x);
  const ys = nodes.map((node) => node.y);
  const width = Math.max(...xs) - Math.min(...xs);
  const height = Math.max(...ys) - Math.min(...ys);
  if (width < 1 || height < 1) return false;
  const aspect = width / height;
  return aspect >= 0.125 && aspect <= 8;
}

export function studioNodeSize(node, degree = 0) {
  const base = NODE_SIZES[node.kind] || DEFAULT_NODE_SIZE;
  const maxWidth = NODE_MAX_WIDTHS[node.kind] || DEFAULT_NODE_MAX_WIDTH;
  const degreeBoost = (() => { if (degree >= 5) return 18; return degree >= 3 ? 10 : (() => { if (degree >= 1) return 4; return 0; })(); })();
  const titleWidth = estimateStudioTextWidth(node.label, NODE_TITLE_FONT_SIZE);
  const contentWidth = Math.ceil(titleWidth / NODE_TITLE_WIDTH_RATIO);
  const width = Math.round(Math.min(maxWidth, Math.max(base.width + degreeBoost, contentWidth)));
  let lineCount = Math.max(1, Math.ceil(titleWidth / Math.max(1, width * NODE_TITLE_WIDTH_RATIO)));
  let height = Math.round(base.height + Math.min(degreeBoost, 10) + (lineCount - 1) * NODE_TITLE_LINE_HEIGHT);
  if (node.kind === 'object' || node.kind === 'cluster') {
    const memberBoost = node.kind === 'cluster'
      ? Math.min(60, Math.round(Math.sqrt(Math.max(1, node.memberCount || 1)) * 5))
      : 0;
    let diameter = Math.max(width, height);
    diameter = Math.min(maxWidth, diameter + memberBoost);
    lineCount = Math.max(1, Math.ceil(titleWidth / Math.max(1, diameter * NODE_TITLE_WIDTH_RATIO)));
    height = Math.round(base.height + Math.min(degreeBoost, 10) + (lineCount - 1) * NODE_TITLE_LINE_HEIGHT);
    diameter = Math.max(diameter, width, height);
    return { width: diameter, height: diameter };
  }
  return { width, height };
}

export function estimateStudioTextWidth(text, fontSize = NODE_TITLE_FONT_SIZE) {
  return Array.from(String(text || '')).reduce((width, character) => {
    if (/\s/u.test(character)) return width + fontSize * 0.3;
    if (/[\u2e80-\u9fff\uf900-\ufaff]/u.test(character)) return width + fontSize;
    if (/[A-Z]/u.test(character)) return width + fontSize * 0.66;
    if (/[a-z0-9]/u.test(character)) return width + fontSize * 0.54;
    return width + fontSize * 0.64;
  }, 0);
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

function withNodeSize(node, degree = 0) {
  return { ...node, ...studioNodeSize(node, degree), degree };
}

function studioDegreeById(nodes, edges) {
  const result = new Map(nodes.map((node) => [node.id, 0]));
  edges.forEach((edge) => {
    if (result.has(edge.source)) result.set(edge.source, result.get(edge.source) + 1);
    if (result.has(edge.target)) result.set(edge.target, result.get(edge.target) + 1);
  });
  return result;
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
