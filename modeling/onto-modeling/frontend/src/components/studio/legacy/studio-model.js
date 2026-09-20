export const STUDIO_TARGET = '知识包 Studio';

export const KIND_META = {
  object: { label: '对象', className: 'object', color: '#07386f', soft: '#9fc8ff', glow: 'rgba(7, 56, 111, .52)' },
  operation: { label: '操作', className: 'operation', color: '#07513a', soft: '#a8dfc2', glow: 'rgba(7, 81, 58, .52)' },
  rule: { label: '规则', className: 'rule', color: '#5d3500', soft: '#f8ce5d', glow: 'rgba(93, 53, 0, .52)' },
  metric: { label: '指标', className: 'metric', color: '#35166f', soft: '#cdb5ff', glow: 'rgba(53, 22, 111, .52)' },
  script: { label: '脚本', className: 'script', color: '#791538', soft: '#f3a8c0', glow: 'rgba(121, 21, 56, .52)' },
};

export const STUDIO_CLUSTER_NODE_THRESHOLD = 500;
export const STUDIO_CLUSTER_COMPLEXITY_THRESHOLD = 280;
export const STUDIO_CLUSTER_MIN_COMPLEX_NODE_COUNT = 200;

export function createStudioSession() {
  return {
    phase: 'idle',
    error: null,
    schema: null,
    elements: [],
    kindTotals: {},
    total: 0,
    graph: null,
    details: {},
    nextCursor: null,
    workingRevision: '',
    selectedElementId: '',
    view: 'graph',
    returnView: 'graph',
    query: '',
    queryDraft: '',
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
  restored.queryDraft = restored.query;
  restored.kind = String(restored.kind || '');
  if (restored.kind && !Object.prototype.hasOwnProperty.call(KIND_META, restored.kind)) restored.kind = '';
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

export function studioSearchDraft(session) {
  return String(session?.queryDraft ?? session?.query ?? '');
}

export function stageStudioSearch(session, value) {
  return { ...session, queryDraft: String(value ?? '') };
}

export function submitStudioSearch(session) {
  const query = studioSearchDraft(session).trim();
  if (query === String(session?.query || '') && query === studioSearchDraft(session)) return session;
  return { ...session, query, queryDraft: query };
}

export function clearStudioSearch(session) {
  if (!session?.query && !studioSearchDraft(session)) return session;
  return { ...session, query: '', queryDraft: '', selectedElementId: '' };
}

export function shouldSubmitStudioSearch(event) {
  return event?.key === 'Enter'
    && !event?.isComposing
    && !event?.nativeEvent?.isComposing;
}

export function studioTextMatchesQuery(text, query) {
  const normalizedText = String(text ?? '').normalize('NFKC').toLocaleLowerCase('zh-CN');
  const normalizedQuery = String(query ?? '').normalize('NFKC').trim().toLocaleLowerCase('zh-CN');
  if (!normalizedQuery) return true;
  if (normalizedText.includes(normalizedQuery)) return true;
  const queryCharacters = Array.from(normalizedQuery);
  if (queryCharacters.length < 3 || !/^\p{Script=Han}+$/u.test(normalizedQuery)) return false;
  return (normalizedText.match(/\p{Script=Han}+/gu) || []).some((token) => (
    fuzzyChineseTokenContains(token, queryCharacters)
  ));
}

function fuzzyChineseTokenContains(token, queryCharacters) {
  const tokenCharacters = Array.from(token);
  const minimumLength = Math.max(1, queryCharacters.length - 1);
  const maximumLength = queryCharacters.length + 1;
  for (let start = 0; start < tokenCharacters.length; start += 1) {
    for (let length = minimumLength; length <= maximumLength; length += 1) {
      if (start + length > tokenCharacters.length) continue;
      if (withinSingleEdit(tokenCharacters.slice(start, start + length), queryCharacters)) return true;
    }
  }
  return false;
}

function withinSingleEdit(left, right) {
  if (Math.abs(left.length - right.length) > 1) return false;
  if (left.length === right.length) {
    let differences = 0;
    for (let index = 0; index < left.length; index += 1) {
      if (left[index] !== right[index] && differences++ >= 1) return false;
    }
    return true;
  }
  const shorter = left.length < right.length ? left : right;
  const longer = left.length < right.length ? right : left;
  let shortIndex = 0;
  let longIndex = 0;
  let skipped = false;
  while (shortIndex < shorter.length && longIndex < longer.length) {
    if (shorter[shortIndex] === longer[longIndex]) {
      shortIndex += 1;
      longIndex += 1;
    } else {
      if (skipped) return false;
      skipped = true;
      longIndex += 1;
    }
  }
  return true;
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
    kind: Object.prototype.hasOwnProperty.call(KIND_META, params.get('studioKind') || '')
      ? params.get('studioKind') || ''
      : '',
    query: params.get('studioQuery') || '',
  };
}

function setOptional(params, key, value) {
  if (value) params.set(key, value);
  else params.delete(key);
}

export function applyStudioFocus(session, focus) {
  if (focus?.kind !== 'studio') return session;
  const requestedView = (() => { if (['graph', 'table', 'entry'].includes(focus.view)) return focus.view; return focus.elementId ? 'entry' : session.view; })();
  const selectedElementId = focus.elementId || session.selectedElementId;
  return {
    ...session,
    view: requestedView,
    selectedElementId,
    returnView: (() => { if (requestedView === 'entry') return ((() => { if (session.view === 'table') return 'table'; return session.returnView || 'graph'; })()); return requestedView; })(),
  };
}

export function filterStudioGraph(graph, kind, query, elements = [], hiddenKinds = []) {
  const normalized = String(query || '').trim();
  const hidden = new Set(hiddenKinds);
  const searchable = new Map(elements.map((element) => [
    element.id,
    `${element.id} ${element.title} ${element.description}`,
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
    studioTextMatchesQuery(searchable.get(node.id) || `${node.id} ${node.label}`, normalized)
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

export function projectStudioLargeGraph(graph, {
  expandedClusterIds = [],
  query = '',
  selectedElementId = '',
  threshold = STUDIO_CLUSTER_NODE_THRESHOLD,
  maxClusterSize = null,
} = {}) {
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  if (String(query || '').trim() || !studioGraphNeedsClustering(nodes, edges, threshold)) {
    return { nodes, edges, clusters: {}, clustered: false };
  }

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const targetClusterCount = Math.min(30, Math.max(10, Math.round(Math.sqrt(nodes.length / 8))));
  const resolvedClusterSize = Number.isInteger(maxClusterSize) && maxClusterSize > 0
    ? maxClusterSize
    : Math.min(180, Math.max(1, Math.ceil(nodes.length / targetClusterCount)));
  const adjacency = new Map(nodes.map((node) => [node.id, new Set()]));
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  edges.forEach((edge) => {
    if (!adjacency.has(edge.source) || !adjacency.has(edge.target)) return;
    adjacency.get(edge.source).add(edge.target);
    adjacency.get(edge.target).add(edge.source);
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  });

  const unassigned = new Set(nodes.map((node) => node.id));
  const groups = [];
  const compareIds = (left, right) => (
    (degree.get(right) || 0) - (degree.get(left) || 0) || left.localeCompare(right)
  );
  while (unassigned.size) {
    const seed = [...unassigned].sort(compareIds)[0];
    const queue = [seed];
    const queued = new Set(queue);
    const ids = [];
    while (queue.length && ids.length < resolvedClusterSize) {
      const id = queue.shift();
      if (!unassigned.delete(id)) continue;
      ids.push(id);
      [...(adjacency.get(id) || [])].filter((next) => unassigned.has(next) && !queued.has(next))
        .sort(compareIds)
        .forEach((next) => { queued.add(next); queue.push(next); });
    }
    if (ids.length < resolvedClusterSize) {
      [...unassigned].sort(compareIds).slice(0, resolvedClusterSize - ids.length)
        .forEach((id) => { unassigned.delete(id); ids.push(id); });
    }
    groups.push(ids.sort());
  }

  const expanded = new Set(expandedClusterIds || []);
  const displayIdByNode = new Map();
  const clusters = {};
  const displayNodes = [];
  const clusterColumns = Math.max(1, Math.ceil(Math.sqrt(groups.length * 1.6)));
  const clusterRows = Math.ceil(groups.length / clusterColumns);
  groups.forEach((ids, index) => {
    const clusterId = `cluster:${index + 1}:${ids[0]}`;
    const members = ids.map((id) => nodeById.get(id)).filter(Boolean);
    const hub = [...members].sort((left, right) => compareIds(left.id, right.id))[0];
    const isExpanded = expanded.has(clusterId) || ids.includes(selectedElementId);
    const clusterCenter = {
      x: (index % clusterColumns - (clusterColumns - 1) / 2) * 360,
      y: (Math.floor(index / clusterColumns) - (clusterRows - 1) / 2) * 300,
    };
    clusters[clusterId] = { id: clusterId, memberIds: ids, expanded: isExpanded };
    if (isExpanded) {
      const memberColumns = Math.max(1, Math.ceil(Math.sqrt(members.length * 1.4)));
      const memberRows = Math.ceil(members.length / memberColumns);
      members.forEach((node, memberIndex) => {
        displayIdByNode.set(node.id, node.id);
        displayNodes.push({
          ...node,
          x: clusterCenter.x + (memberIndex % memberColumns - (memberColumns - 1) / 2) * 150,
          y: clusterCenter.y + (Math.floor(memberIndex / memberColumns) - (memberRows - 1) / 2) * 125,
        });
      });
      return;
    }
    ids.forEach((id) => displayIdByNode.set(id, clusterId));
    displayNodes.push({
      id: clusterId,
      kind: 'cluster',
      label: `${hub?.label || '关系节点'} 等 ${ids.length} 项`,
      memberCount: ids.length,
      memberIds: ids,
      ...clusterCenter,
    });
  });

  const aggregatedEdges = new Map();
  edges.forEach((edge) => {
    const source = displayIdByNode.get(edge.source);
    const target = displayIdByNode.get(edge.target);
    if (!source || !target || source === target) return;
    const collapsedEdge = source.startsWith('cluster:') && target.startsWith('cluster:');
    const key = collapsedEdge ? `${source}|${target}` : `${source}|${target}|${edge.kind}`;
    const current = aggregatedEdges.get(key);
    if (current) {
      current.count += 1;
      current.label = `${current.count} 条关系`;
      return;
    }
    aggregatedEdges.set(key, {
      ...edge,
      id: `cluster-edge:${key}`,
      source,
      target,
      ...(collapsedEdge ? { kind: 'cluster_relation', label: '1 条关系' } : {}),
      count: 1,
    });
  });
  return { nodes: displayNodes, edges: [...aggregatedEdges.values()], clusters, clustered: true };
}

export function studioGraphNeedsClustering(
  nodes = [],
  edges = [],
  threshold = STUDIO_CLUSTER_NODE_THRESHOLD,
) {
  const nodeCount = nodes.length;
  if (nodeCount >= threshold) return true;
  if (threshold !== STUDIO_CLUSTER_NODE_THRESHOLD
    || nodeCount < STUDIO_CLUSTER_MIN_COMPLEX_NODE_COUNT
    || !edges.length) return false;
  const nodeIds = new Set(nodes.map((node) => node.id));
  const validEdgeCount = edges.reduce((count, edge) => (
    nodeIds.has(edge.source) && nodeIds.has(edge.target) ? count + 1 : count
  ), 0);
  if (validEdgeCount < nodeCount * 0.8) return false;
  const edgeDensity = validEdgeCount / Math.max(1, nodeCount);
  return edgeDensity >= 2
    || nodeCount + validEdgeCount * 0.35 >= STUDIO_CLUSTER_COMPLEXITY_THRESHOLD;
}

export function studioVisibleElementId(selectedElementId, nodes = []) {
  const selected = String(selectedElementId || '');
  return selected && nodes.some((node) => node.id === selected) ? selected : '';
}

export function studioKindCounts(elements = []) {
  return elements.reduce((result, element) => {
    if (element?.kind) result[element.kind] = (result[element.kind] || 0) + 1;
    return result;
  }, {});
}

export function studioGraphScrollSurface(nodes = [], zoom = 1, viewport = {}, padding = 80, decorations = []) {
  if (!nodes.length && !decorations.length) {
    return { width: 0, height: 0, viewport: { x: 0, y: 0, zoom: 1 } };
  }
  const scale = Math.min(2, Math.max(0.5, Number(zoom) || 1));
  const nodeBoxes = nodes.map((node) => {
    const width = Number(node.measured?.width ?? node.width ?? node.style?.width) || 160;
    const height = Number(node.measured?.height ?? node.height ?? node.style?.height) || 72;
    const left = Number(node.position?.x) || 0;
    const top = Number(node.position?.y) || 0;
    return { left, top, right: left + width, bottom: top + height };
  });
  const decorationBoxes = decorations.flatMap((item) => {
    const x = Number(item?.x);
    const y = Number(item?.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return [];
    const width = Math.max(0, Number(item?.width) || 0);
    const height = Math.max(0, Number(item?.height) || 0);
    return [{ left: x, top: y, right: x + width, bottom: y + height }];
  });
  const boxes = [...nodeBoxes, ...decorationBoxes];
  const minX = Math.min(...boxes.map((box) => box.left));
  const minY = Math.min(...boxes.map((box) => box.top));
  const maxX = Math.max(...boxes.map((box) => box.right));
  const maxY = Math.max(...boxes.map((box) => box.bottom));
  const contentWidth = (maxX - minX) * scale;
  const contentHeight = (maxY - minY) * scale;
  const width = Math.max(contentWidth + padding * 2, Number(viewport.width) || 0);
  const height = Math.max(contentHeight + padding * 2, Number(viewport.height) || 0);
  return {
    width: Math.ceil(width),
    height: Math.ceil(height),
    viewport: {
      x: (width - contentWidth) / 2 - minX * scale,
      y: (height - contentHeight) / 2 - minY * scale,
      zoom: scale,
    },
  };
}

export function studioSummaryVisible(
  selectedElementId,
  dismissedElementId,
  { activeKind = '', selectedKind = '' } = {},
) {
  const selected = String(selectedElementId || '');
  return Boolean(selected)
    && selected !== String(dismissedElementId || '')
    && !(activeKind === 'operation' && selectedKind === 'operation');
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
    return { color: '#8a5400', labelColor: '#5e3900', dash: '6 4' };
  }
  if (edge.kind === 'uses_metric') {
    return { color: '#5935a4', labelColor: '#402479', dash: '3 3' };
  }
  if (edge.kind === 'uses_object' && nodesById.get(edge.source)?.kind === 'metric') {
    return { color: '#5935a4', labelColor: '#402479' };
  }
  if (edge.kind === 'uses_object') return { color: '#126347', labelColor: '#0a4531' };
  if (['uses_script', 'script_bound_operation', 'script_uses_object'].includes(edge.kind)) {
    return { color: '#8f1740', labelColor: '#65102d', dash: '2 4' };
  }
  if (edge.kind === 'object_relation') return { color: '#0b4c91', labelColor: '#06366a' };
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
