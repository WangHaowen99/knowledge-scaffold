export const STUDIO_TARGET = '知识包 Studio';

export const KIND_META = {
  object: { label: '对象', className: 'object', color: '#4d8ccf', soft: '#edf5fd', glow: 'rgba(77, 140, 207, .42)' },
  operation: { label: '操作', className: 'operation', color: '#398164', soft: '#edf8f2', glow: 'rgba(57, 129, 100, .42)' },
  rule: { label: '规则', className: 'rule', color: '#a97917', soft: '#fff7dc', glow: 'rgba(169, 121, 23, .42)' },
  metric: { label: '指标', className: 'metric', color: '#7162a8', soft: '#f4f1fb', glow: 'rgba(113, 98, 168, .42)' },
  concept: { label: '融合概念', className: 'concept', color: '#b24f76', soft: '#fbedf3', glow: 'rgba(178, 79, 118, .42)' },
};

export function createStudioSession() {
  return {
    phase: 'idle',
    error: null,
    schema: null,
    elements: [],
    total: 0,
    graph: null,
    details: {},
    nextCursor: null,
    workingRevision: '',
    selectedElementId: '',
    view: 'graph',
    returnView: 'graph',
    query: '',
    kind: '',
    hiddenKinds: [],
    graphHighlight: 'all',
    graphMotion: null,
    graphViewport: null,
    nodePositions: {},
    expandedClusterIds: [],
    tableScrollTop: 0,
    selectedIds: [],
    entryDrafts: {},
    studioPendingReview: false,
    studioWarnings: [],
    studioWarningCount: 0,
  };
}

const HISTORY_FIELDS = [
  'view',
  'returnView',
  'selectedElementId',
  'query',
  'kind',
  'hiddenKinds',
  'graphHighlight',
  'graphMotion',
  'graphViewport',
  'nodePositions',
  'expandedClusterIds',
  'tableScrollTop',
  'selectedIds',
];

export function studioHistorySnapshot(session, projectId = '') {
  return {
    ...(projectId ? { projectId } : {}),
    ...Object.fromEntries(HISTORY_FIELDS.map((key) => [key, session[key]])),
  };
}

export function restoreStudioHistory(session, snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return session;
  const restored = { ...session };
  for (const key of HISTORY_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(snapshot, key)) restored[key] = snapshot[key];
  }
  if (!['graph', 'table', 'entry'].includes(restored.view)) restored.view = 'graph';
  if (!['graph', 'table'].includes(restored.returnView)) restored.returnView = 'graph';
  restored.selectedElementId = String(restored.selectedElementId || '');
  restored.query = String(restored.query || '');
  restored.kind = String(restored.kind || '');
  restored.hiddenKinds = Array.isArray(restored.hiddenKinds)
    ? restored.hiddenKinds.filter((kind) => Object.prototype.hasOwnProperty.call(KIND_META, kind))
    : [];
  restored.graphHighlight = ['all', 'rule', 'metric'].includes(restored.graphHighlight)
    ? restored.graphHighlight
    : 'all';
  restored.graphMotion = typeof restored.graphMotion === 'boolean' ? restored.graphMotion : null;
  restored.selectedIds = Array.isArray(restored.selectedIds) ? restored.selectedIds : [];
  restored.expandedClusterIds = Array.isArray(restored.expandedClusterIds) ? restored.expandedClusterIds : [];
  restored.nodePositions = restored.nodePositions && typeof restored.nodePositions === 'object'
    ? restored.nodePositions
    : {};
  return restored;
}

export function studioHistoryUrl(location, session) {
  const url = new URL(location.href);
  url.searchParams.set('studioView', session.view || 'graph');
  setOptional(url.searchParams, 'studioElement', session.selectedElementId);
  setOptional(url.searchParams, 'studioKind', session.kind);
  setOptional(url.searchParams, 'studioQuery', session.query);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function studioHistoryFromLocation(location) {
  const params = new URL(location.href).searchParams;
  const view = params.get('studioView');
  if (!view) return null;
  return {
    view,
    selectedElementId: params.get('studioElement') || '',
    kind: params.get('studioKind') || '',
    query: params.get('studioQuery') || '',
  };
}

function setOptional(params, key, value) {
  if (value) params.set(key, value);
  else params.delete(key);
}

export function applyStudioFocus(session, focus) {
  if (focus?.kind !== 'studio') return session;
  const requestedView = ['graph', 'table', 'entry'].includes(focus.view)
    ? focus.view
    : focus.elementId ? 'entry' : session.view;
  const selectedElementId = focus.elementId || session.selectedElementId;
  return {
    ...session,
    view: requestedView,
    selectedElementId,
    returnView: requestedView === 'entry'
      ? (session.view === 'table' ? 'table' : session.returnView || 'graph')
      : requestedView,
  };
}

export function filterStudioGraph(graph, kind, query, elements = [], hiddenKinds = []) {
  const normalized = String(query || '').trim().toLocaleLowerCase('zh-CN');
  const hidden = new Set(hiddenKinds);
  const searchable = new Map(elements.map((element) => [
    element.id,
    `${element.id} ${element.title} ${element.description}`.toLocaleLowerCase('zh-CN'),
  ]));
  const eligibleNodes = (graph?.nodes || []).filter((node) => (
    (!kind || node.kind === kind)
    && !hidden.has(node.kind)
  ));
  const eligibleIds = new Set(eligibleNodes.map((node) => node.id));
  if (!normalized) {
    const edges = (graph?.edges || []).filter(
      (edge) => eligibleIds.has(edge.source) && eligibleIds.has(edge.target),
    );
    return { nodes: eligibleNodes, edges };
  }
  const matchedIds = new Set(eligibleNodes.filter((node) => (
    (searchable.get(node.id) || `${node.id} ${node.label}`.toLocaleLowerCase('zh-CN'))
      .includes(normalized)
  )).map((node) => node.id));
  const visibleIds = new Set(matchedIds);
  const edges = (graph?.edges || []).filter((edge) => {
    if (!eligibleIds.has(edge.source) || !eligibleIds.has(edge.target)) return false;
    if (!matchedIds.has(edge.source) && !matchedIds.has(edge.target)) return false;
    visibleIds.add(edge.source);
    visibleIds.add(edge.target);
    return true;
  });
  const nodes = eligibleNodes.filter((node) => visibleIds.has(node.id));
  return { nodes, edges };
}

export function studioVisibleElementId(selectedElementId, nodes = []) {
  const selected = String(selectedElementId || '');
  return selected && nodes.some((node) => node.id === selected) ? selected : '';
}

export function studioSummaryVisible(selectedElementId, dismissedElementId) {
  const selected = String(selectedElementId || '');
  return Boolean(selected) && selected !== String(dismissedElementId || '');
}

export function studioGraphNeighborhood(nodeId, edges = []) {
  if (!nodeId) return { nodes: new Set(), edges: new Set() };
  const nodes = new Set([nodeId]);
  const edgeIds = new Set();
  for (const edge of edges) {
    if (edge.source !== nodeId && edge.target !== nodeId) continue;
    nodes.add(edge.source);
    nodes.add(edge.target);
    edgeIds.add(edge.id);
  }
  return { nodes, edges: edgeIds };
}

export function studioGraphChain(kind, nodes = [], edges = []) {
  const kinds = new Map(nodes.map((node) => [node.id, node.kind]));
  const nodeIds = new Set();
  const edgeIds = new Set();
  for (const edge of edges) {
    if (kinds.get(edge.source) !== kind && kinds.get(edge.target) !== kind) continue;
    nodeIds.add(edge.source);
    nodeIds.add(edge.target);
    edgeIds.add(edge.id);
  }
  return { nodes: nodeIds, edges: edgeIds };
}

export function studioGraphEdgeStyle(edge, nodesById = new Map()) {
  if (['uses_rule', 'applies_to_object'].includes(edge.kind)) {
    return { color: '#b88924', labelColor: '#755a16', dash: '6 4' };
  }
  if (edge.kind === 'uses_metric') {
    return { color: '#8879bd', labelColor: '#5c4d91', dash: '3 3' };
  }
  if (edge.kind === 'uses_object' && nodesById.get(edge.source)?.kind === 'metric') {
    return { color: '#8879bd', labelColor: '#5c4d91' };
  }
  if (edge.kind === 'uses_object') return { color: '#4d9670', labelColor: '#377352' };
  if (edge.kind === 'concept_member') return { color: '#bd6687', labelColor: '#82415a', dash: '2 4' };
  if (edge.kind === 'object_relation') return { color: '#5f98ce', labelColor: '#3e6f9f' };
  return { color: '#98a2b1', labelColor: '#667080', dash: '4 4' };
}

const sessions = new Map();

export function readStudioSession(projectId) {
  if (!sessions.has(projectId)) {
    sessions.set(projectId, {
      ...createStudioSession(),
      entryDrafts: readPersistedStudioDrafts(projectId),
    });
  }
  return sessions.get(projectId);
}

export function writeStudioSession(projectId, session) {
  sessions.set(projectId, session);
  persistStudioDrafts(projectId, session?.entryDrafts);
  return session;
}

export function resetStudioSession(projectId) {
  sessions.delete(projectId);
  if (typeof globalThis.sessionStorage !== 'undefined') {
    globalThis.sessionStorage.removeItem(studioDraftStorageKey(projectId));
  }
}

function readPersistedStudioDrafts(projectId) {
  if (typeof globalThis.sessionStorage === 'undefined') return {};
  try {
    const parsed = JSON.parse(globalThis.sessionStorage.getItem(studioDraftStorageKey(projectId)) || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function persistStudioDrafts(projectId, drafts) {
  if (typeof globalThis.sessionStorage === 'undefined') return;
  const key = studioDraftStorageKey(projectId);
  if (!drafts || !Object.keys(drafts).length) {
    globalThis.sessionStorage.removeItem(key);
    return;
  }
  globalThis.sessionStorage.setItem(key, JSON.stringify(drafts));
}

function studioDraftStorageKey(projectId) {
  return `modeling:studio-entry-drafts:${String(projectId || '')}`;
}
