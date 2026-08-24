export const STUDIO_TARGET = '知识包 Studio';

export const KIND_META = {
  object: { label: '对象', className: 'object' },
  operation: { label: '操作', className: 'operation' },
  rule: { label: '规则', className: 'rule' },
  metric: { label: '指标', className: 'metric' },
  concept: { label: '融合概念', className: 'concept' },
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

export function filterStudioGraph(graph, kind, query, elements = []) {
  const normalized = String(query || '').trim().toLocaleLowerCase('zh-CN');
  const searchable = new Map(elements.map((element) => [
    element.id,
    `${element.id} ${element.title} ${element.description}`.toLocaleLowerCase('zh-CN'),
  ]));
  const nodes = (graph?.nodes || []).filter((node) => (
    (!kind || node.kind === kind)
    && (!normalized || (searchable.get(node.id) || `${node.id} ${node.label}`
      .toLocaleLowerCase('zh-CN')).includes(normalized))
  ));
  const ids = new Set(nodes.map((node) => node.id));
  const edges = (graph?.edges || []).filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  return { nodes, edges };
}

const sessions = new Map();

export function readStudioSession(projectId) {
  if (!sessions.has(projectId)) sessions.set(projectId, createStudioSession());
  return sessions.get(projectId);
}

export function writeStudioSession(projectId, session) {
  sessions.set(projectId, session);
  return session;
}

export function resetStudioSession(projectId) {
  sessions.delete(projectId);
}
