function clone(value) {
  return structuredClone(value);
}

function nextId(prefix, ids) {
  let index = 1;
  while (ids.has(`${prefix}-${index}`)) index += 1;
  return `${prefix}-${index}`;
}

function normalizeDiagram(diagram, fallbackId) {
  const normalized = clone(diagram);
  normalized.id = normalized.id || fallbackId;
  normalized.title = normalized.title || '未命名流程';
  normalized.nodes = Array.isArray(normalized.nodes) ? normalized.nodes : [];
  normalized.edges = Array.isArray(normalized.edges) ? normalized.edges : [];

  const nodeIds = new Set(normalized.nodes.map((node) => node.id).filter(Boolean));
  normalized.nodes = normalized.nodes.map((node) => {
    const id = node.id || nextId('node', nodeIds);
    nodeIds.add(id);
    return {
      ...node,
      id,
      kind: node.kind || 'action',
      label: node.label || '未命名节点',
    };
  });

  const edgeIds = new Set(normalized.edges.map((edge) => edge.id).filter(Boolean));
  normalized.edges = normalized.edges.map((edge) => {
    const id = edge.id || nextId('edge', edgeIds);
    edgeIds.add(id);
    return {
      ...edge,
      id,
      kind: edge.kind || 'sequence',
      label: edge.label || '',
      conditionText: edge.conditionText || '',
    };
  });
  normalized.layout = normalized.layout || { direction: 'TB', positions: {} };
  normalized.layout.direction = normalized.layout.direction || 'TB';
  normalized.layout.positions = normalized.layout.positions || {};
  return normalized;
}

function normalizeDocument(document) {
  const diagrams = Array.isArray(document?.diagrams) ? document.diagrams : [];
  return {
    diagrams: diagrams.map((diagram, index) => normalizeDiagram(diagram, `diagram-${index + 1}`)),
  };
}

function documentsEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function diagramById(document, diagramId) {
  return document.diagrams.find((diagram) => diagram.id === diagramId);
}

function commit(session, document, selection = session.selection) {
  const dirty = !documentsEqual(document, session.savedDocument);
  return {
    ...session,
    past: [...session.past, session.document],
    document,
    future: [],
    selection,
    editGeneration: session.editGeneration + 1,
    dirty,
    saveStatus: session.saveInFlight ? 'saving' : dirty ? 'dirty' : 'saved',
  };
}

export function createFlowchartEditorSession(document, { savedRevision = null, activeDiagramId = '' } = {}) {
  const normalized = normalizeDocument(document);
  // 从 .mmd 打开时要直接停在被点击的那张图上；传入的 id 不存在则退回第一张
  const requested = normalized.diagrams.some((diagram) => diagram.id === activeDiagramId)
    ? activeDiagramId
    : '';
  return {
    document: normalized,
    savedDocument: clone(normalized),
    savedRevision,
    activeDiagramId: requested || normalized.diagrams[0]?.id || '',
    past: [],
    future: [],
    selection: null,
    editGeneration: 0,
    saveGeneration: null,
    savingDocument: null,
    saveInFlight: false,
    dirty: false,
    saveStatus: 'saved',
    saveError: '',
  };
}

export function reduceFlowchartEditorSession(session, action) {
  if (action.type === 'replace-document') {
    if (session.dirty && !action.force) return session;
    const replacement = createFlowchartEditorSession(action.document, {
      savedRevision: action.savedRevision ?? session.savedRevision,
      activeDiagramId: action.activeDiagramId || session.activeDiagramId,
    });
    if (documentsEqual(replacement.document, session.document)
      && replacement.savedRevision === session.savedRevision) return session;
    return replacement;
  }

  if (action.type === 'mark-saving') {
    if (!session.dirty || session.saveInFlight) return session;
    return {
      ...session,
      saveGeneration: session.editGeneration,
      savingDocument: clone(session.document),
      saveInFlight: true,
      saveStatus: 'saving',
      saveError: '',
    };
  }

  if (action.type === 'mark-saved') {
    const savedDocument = action.document
      ? normalizeDocument(action.document)
      : clone(session.savingDocument ?? session.document);
    const editedAfterRequest = session.saveGeneration !== null
      && session.editGeneration !== session.saveGeneration;
    const document = editedAfterRequest ? session.document : savedDocument;
    const dirty = !documentsEqual(document, savedDocument);
    return {
      ...session,
      document,
      savedDocument: clone(savedDocument),
      savedRevision: action.savedRevision ?? session.savedRevision,
      saveGeneration: null,
      savingDocument: null,
      saveInFlight: false,
      dirty,
      saveStatus: dirty ? 'dirty' : 'saved',
      saveError: '',
    };
  }

  if (action.type === 'mark-save-failed') {
    return {
      ...session,
      saveGeneration: null,
      savingDocument: null,
      saveInFlight: false,
      saveStatus: 'error',
      saveError: action.message || '保存失败',
    };
  }

  if (action.type === 'undo') {
    const document = session.past.at(-1);
    if (!document) return session;
    const dirty = !documentsEqual(document, session.savedDocument);
    return {
      ...session,
      document,
      past: session.past.slice(0, -1),
      future: [session.document, ...session.future],
      selection: null,
      editGeneration: session.editGeneration + 1,
      dirty,
      saveStatus: session.saveInFlight ? 'saving' : dirty ? 'dirty' : 'saved',
    };
  }

  if (action.type === 'redo') {
    const [document, ...future] = session.future;
    if (!document) return session;
    const dirty = !documentsEqual(document, session.savedDocument);
    return {
      ...session,
      document,
      past: [...session.past, session.document],
      future,
      selection: null,
      editGeneration: session.editGeneration + 1,
      dirty,
      saveStatus: session.saveInFlight ? 'saving' : dirty ? 'dirty' : 'saved',
    };
  }

  if (action.type === 'add-diagram') {
    const document = clone(session.document);
    const id = action.id || nextId('diagram', new Set(document.diagrams.map((diagram) => diagram.id)));
    document.diagrams.push({
      id,
      title: action.title || '未命名流程',
      description: action.description || '',
      nodes: [],
      edges: [],
      layout: { direction: 'TB', positions: {} },
    });
    return { ...commit(session, document, null), activeDiagramId: id };
  }

  if (action.type === 'delete-diagram') {
    const document = clone(session.document);
    const index = document.diagrams.findIndex((diagram) => diagram.id === action.diagramId);
    if (index < 0) return session;
    const deletingActive = session.activeDiagramId === action.diagramId;
    document.diagrams.splice(index, 1);
    const activeDiagramId = deletingActive
      ? document.diagrams[Math.min(index, document.diagrams.length - 1)]?.id || ''
      : session.activeDiagramId;
    return { ...commit(session, document, null), activeDiagramId };
  }

  if (action.type === 'select-diagram') {
    if (!session.document.diagrams.some((diagram) => diagram.id === action.diagramId)) return session;
    return { ...session, activeDiagramId: action.diagramId, selection: null };
  }

  if (action.type === 'select-element') {
    return { ...session, selection: action.selection || null };
  }

  if (action.type === 'add-node') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram) return session;

    const ids = new Set(diagram.nodes.map((node) => node.id));
    const id = action.node.id || nextId('node', ids);
    diagram.nodes.push({
      id,
      kind: action.node.kind || 'action',
      label: action.node.label || '新建步骤',
      detail: action.node.detail || '',
    });
    diagram.layout = diagram.layout || { direction: 'TB', positions: {} };
    diagram.layout.positions = diagram.layout.positions || {};
    diagram.layout.positions[id] = action.position || { x: 120, y: 120 };

    return commit(session, document, { type: 'node', diagramId: diagram.id, id });
  }

  if (action.type === 'connect-nodes') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram) return session;
    const nodeIds = new Set(diagram.nodes.map((node) => node.id));
    if (!nodeIds.has(action.from) || !nodeIds.has(action.to)) return session;

    const id = action.edge?.id || nextId('edge', new Set(diagram.edges.map((edge) => edge.id)));
    diagram.edges.push({
      id,
      from: action.from,
      to: action.to,
      kind: action.edge?.kind || 'sequence',
      label: action.edge?.label || '下一步',
      conditionText: action.edge?.conditionText || '',
    });
    return commit(session, document, { type: 'edge', diagramId: diagram.id, id });
  }

  if (action.type === 'spawn-connected-node') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram) return session;
    const nodeIds = new Set(diagram.nodes.map((node) => node.id));
    if (!nodeIds.has(action.from)) return session;

    const nodeId = action.node?.id || nextId('node', nodeIds);
    diagram.nodes.push({
      id: nodeId,
      kind: action.node?.kind || 'action',
      label: action.node?.label || '新建步骤',
      detail: action.node?.detail || '',
    });
    diagram.layout = diagram.layout || { direction: 'TB', positions: {} };
    diagram.layout.positions = diagram.layout.positions || {};
    diagram.layout.positions[nodeId] = action.position || { x: 120, y: 120 };

    const edgeId = action.edge?.id || nextId('edge', new Set(diagram.edges.map((edge) => edge.id)));
    diagram.edges.push({
      id: edgeId,
      from: action.from,
      to: nodeId,
      kind: action.edge?.kind || 'sequence',
      label: action.edge?.label || '下一步',
      conditionText: action.edge?.conditionText || '',
    });
    return commit(session, document, { type: 'node', diagramId: diagram.id, id: nodeId });
  }

  if (action.type === 'reconnect-edge') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    const edge = diagram?.edges.find((candidate) => candidate.id === action.id);
    if (!edge) return session;
    const nodeIds = new Set(diagram.nodes.map((node) => node.id));
    if (!nodeIds.has(action.from) || !nodeIds.has(action.to)) return session;
    edge.from = action.from;
    edge.to = action.to;
    return commit(session, document, { type: 'edge', diagramId: diagram.id, id: edge.id });
  }

  if (action.type === 'update-node') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    const node = diagram?.nodes.find((candidate) => candidate.id === action.id);
    if (!node) return session;
    if (action.patch.kind !== undefined) node.kind = action.patch.kind;
    if (action.patch.label !== undefined) node.label = action.patch.label;
    if (action.patch.detail !== undefined) node.detail = action.patch.detail;
    return commit(session, document, { type: 'node', diagramId: diagram.id, id: node.id });
  }

  if (action.type === 'update-diagram') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram) return session;
    if (action.patch.title !== undefined) diagram.title = action.patch.title;
    if (action.patch.description !== undefined) diagram.description = action.patch.description;
    if (action.patch.processMeta !== undefined) {
      diagram.processMeta = {
        ...(diagram.processMeta || {}),
        ...clone(action.patch.processMeta),
      };
    }
    return commit(session, document, null);
  }

  if (action.type === 'update-edge') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    const edge = diagram?.edges.find((candidate) => candidate.id === action.id);
    if (!edge) return session;
    if (action.patch.kind !== undefined) edge.kind = action.patch.kind;
    if (action.patch.label !== undefined) edge.label = action.patch.label;
    if (action.patch.conditionText !== undefined) edge.conditionText = action.patch.conditionText;
    return commit(session, document, { type: 'edge', diagramId: diagram.id, id: edge.id });
  }

  if (action.type === 'delete-node') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram?.nodes.some((node) => node.id === action.id)) return session;
    diagram.nodes = diagram.nodes.filter((node) => node.id !== action.id);
    diagram.edges = diagram.edges.filter((edge) => edge.from !== action.id && edge.to !== action.id);
    if (diagram.layout?.positions) delete diagram.layout.positions[action.id];
    return commit(session, document, null);
  }

  if (action.type === 'delete-edge') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram?.edges.some((edge) => edge.id === action.id)) return session;
    diagram.edges = diagram.edges.filter((edge) => edge.id !== action.id);
    return commit(session, document, null);
  }

  if (action.type === 'move-node') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram?.nodes.some((node) => node.id === action.id)) return session;
    diagram.layout = diagram.layout || { direction: 'TB', positions: {} };
    diagram.layout.positions = diagram.layout.positions || {};
    diagram.layout.positions[action.id] = clone(action.position);
    return commit(session, document);
  }

  if (action.type === 'set-layout') {
    const document = clone(session.document);
    const diagram = diagramById(document, action.diagramId);
    if (!diagram) return session;
    const nodeIds = new Set(diagram.nodes.map((node) => node.id));
    diagram.layout = {
      direction: action.direction || diagram.layout?.direction || 'TB',
      positions: Object.fromEntries(
        Object.entries(action.positions || {}).filter(([nodeId]) => nodeIds.has(nodeId)),
      ),
    };
    return commit(session, document);
  }

  return session;
}
