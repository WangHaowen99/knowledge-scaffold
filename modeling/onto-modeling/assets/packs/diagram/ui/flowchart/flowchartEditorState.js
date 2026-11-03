// 流程图编辑会话：一张图（FlowchartModel）的撤销 / 重做、选中、脏标记与保存状态。
// 连续编辑同一个字段（带同一个 coalesce 键）只记一步撤销，避免逐字撤销。
import { normalizeModel } from './flowchartModel.js';

const clone = (v) => structuredClone(v);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function nextId(prefix, ids) {
  let i = 1;
  while (ids.has(`${prefix}${i}`)) i += 1;
  return `${prefix}${i}`;
}

function statusOf(session, dirty) {
  if (session.saveInFlight) return 'saving';
  return dirty ? 'dirty' : 'saved';
}

function commit(session, model, selection = session.selection, coalesce = null) {
  const dirty = !same(model, session.savedModel);
  const merge = coalesce && session.coalesceKey === coalesce;
  return {
    ...session,
    past: merge ? session.past : [...session.past, session.model],
    model,
    future: [],
    selection,
    coalesceKey: coalesce,
    editGeneration: session.editGeneration + 1,
    dirty,
    saveStatus: statusOf(session, dirty),
  };
}

const validSelection = (model, sel) => {
  if (!sel) return null;
  const list = sel.type === 'node' ? model.nodes : model.edges;
  return list.some((x) => x.id === sel.id) ? sel : null;
};

export function createFlowchartEditorSession(model, { savedRevision = null, selection = null } = {}) {
  const m = normalizeModel(model);
  return {
    model: m,
    savedModel: clone(m),
    savedRevision,
    past: [],
    future: [],
    selection: validSelection(m, selection),
    coalesceKey: null,
    editGeneration: 0,
    saveGeneration: null,
    savingModel: null,
    saveInFlight: false,
    dirty: false,
    saveStatus: 'saved',
    saveError: '',
  };
}

export function reduceFlowchartEditorSession(session, action) {
  switch (action.type) {
    case 'replace-model': {
      // 有未保存修改时不覆盖，除非明确要求（载入最新版本）
      if (session.dirty && !action.force) return session;
      const next = createFlowchartEditorSession(action.model, {
        savedRevision: action.savedRevision ?? session.savedRevision,
        selection: action.selection === undefined ? session.selection : action.selection,
      });
      if (same(next.model, session.model) && next.savedRevision === session.savedRevision && !session.dirty) return session;
      return next;
    }

    case 'mark-saving':
      if (session.saveInFlight) return session;
      return {
        ...session,
        saveGeneration: session.editGeneration,
        savingModel: clone(session.model),
        saveInFlight: true,
        saveStatus: 'saving',
        saveError: '',
        coalesceKey: null,
      };

    case 'mark-saved': {
      const saved = action.model ? normalizeModel(action.model) : clone(session.savingModel ?? session.model);
      const editedAfter = session.saveGeneration !== null && session.editGeneration !== session.saveGeneration;
      const model = editedAfter ? session.model : saved;
      const dirty = !same(model, saved);
      return {
        ...session,
        model,
        savedModel: clone(saved),
        savedRevision: action.savedRevision ?? session.savedRevision,
        saveGeneration: null,
        savingModel: null,
        saveInFlight: false,
        dirty,
        saveStatus: dirty ? 'dirty' : 'saved',
        saveError: '',
        selection: validSelection(model, session.selection),
      };
    }

    case 'mark-save-failed':
      return {
        ...session,
        saveGeneration: null,
        savingModel: null,
        saveInFlight: false,
        saveStatus: 'error',
        saveError: action.message || '保存失败',
      };

    case 'undo': {
      const model = session.past.at(-1);
      if (!model) return session;
      const dirty = !same(model, session.savedModel);
      return {
        ...session,
        model,
        past: session.past.slice(0, -1),
        future: [session.model, ...session.future],
        selection: validSelection(model, session.selection),
        coalesceKey: null,
        editGeneration: session.editGeneration + 1,
        dirty,
        saveStatus: statusOf(session, dirty),
      };
    }

    case 'redo': {
      const [model, ...future] = session.future;
      if (!model) return session;
      const dirty = !same(model, session.savedModel);
      return {
        ...session,
        model,
        past: [...session.past, session.model],
        future,
        selection: validSelection(model, session.selection),
        coalesceKey: null,
        editGeneration: session.editGeneration + 1,
        dirty,
        saveStatus: statusOf(session, dirty),
      };
    }

    case 'select':
      return { ...session, selection: validSelection(session.model, action.selection), coalesceKey: null };

    case 'add-node': {
      const model = clone(session.model);
      const id = action.node?.id || nextId('n', new Set(model.nodes.map((n) => n.id)));
      model.nodes.push({
        id,
        type: action.node?.type || 'step',
        label: action.node?.label ?? '新建步骤',
        description: action.node?.description || '',
        x: Math.round(action.position?.x ?? 150),
        y: Math.round(action.position?.y ?? 60),
        refs: [],
        pending: false,
        pending_note: null,
      });
      return commit(session, model, { type: 'node', id });
    }

    case 'connect': {
      const model = clone(session.model);
      const ids = new Set(model.nodes.map((n) => n.id));
      if (!ids.has(action.from) || !ids.has(action.to) || action.from === action.to) return session;
      if (model.edges.some((e) => e.from === action.from && e.to === action.to)) return session;
      const id = nextId('e', new Set(model.edges.map((e) => e.id)));
      model.edges.push({ id, type: action.edge?.type || 'seq', from: action.from, to: action.to, label: action.edge?.label || '', condition: action.edge?.condition || '' });
      return commit(session, model, { type: 'edge', id });
    }

    // 拖线到空白处：在松手的位置新建节点并连上
    case 'spawn-connected': {
      const model = clone(session.model);
      if (!model.nodes.some((n) => n.id === action.from)) return session;
      const nodeId = nextId('n', new Set(model.nodes.map((n) => n.id)));
      model.nodes.push({
        id: nodeId, type: action.node?.type || 'step', label: action.node?.label ?? '新建步骤', description: '',
        x: Math.round(action.position.x), y: Math.round(action.position.y), refs: [], pending: false, pending_note: null,
      });
      const edgeId = nextId('e', new Set(model.edges.map((e) => e.id)));
      model.edges.push({ id: edgeId, type: action.edge?.type || 'seq', from: action.from, to: nodeId, label: '', condition: '' });
      return commit(session, model, { type: 'node', id: nodeId });
    }

    case 'update-node': {
      const model = clone(session.model);
      const node = model.nodes.find((n) => n.id === action.id);
      if (!node) return session;
      ['type', 'label', 'description'].forEach((k) => { if (action.patch[k] !== undefined) node[k] = action.patch[k]; });
      if (same(model, session.model)) return session;
      return commit(session, model, { type: 'node', id: node.id }, action.coalesce ? `node:${node.id}:${action.coalesce}` : null);
    }

    case 'update-edge': {
      const model = clone(session.model);
      const edge = model.edges.find((e) => e.id === action.id);
      if (!edge) return session;
      ['type', 'label', 'condition'].forEach((k) => { if (action.patch[k] !== undefined) edge[k] = action.patch[k]; });
      if (same(model, session.model)) return session;
      return commit(session, model, { type: 'edge', id: edge.id }, action.coalesce ? `edge:${edge.id}:${action.coalesce}` : null);
    }

    case 'update-process': {
      const model = clone(session.model);
      model.process = { ...model.process, ...clone(action.patch) };
      if (same(model, session.model)) return session;
      return commit(session, model, session.selection, action.coalesce ? `process:${action.coalesce}` : null);
    }

    case 'delete-selection': {
      const sel = session.selection;
      if (!sel) return session;
      return reduceFlowchartEditorSession(session, { type: sel.type === 'node' ? 'delete-node' : 'delete-edge', id: sel.id });
    }

    case 'delete-node': {
      if (!session.model.nodes.some((n) => n.id === action.id)) return session;
      const model = clone(session.model);
      model.nodes = model.nodes.filter((n) => n.id !== action.id);
      model.edges = model.edges.filter((e) => e.from !== action.id && e.to !== action.id);
      return commit(session, model, null);
    }

    case 'delete-edge': {
      if (!session.model.edges.some((e) => e.id === action.id)) return session;
      const model = clone(session.model);
      model.edges = model.edges.filter((e) => e.id !== action.id);
      return commit(session, model, null);
    }

    case 'move-node': {
      const model = clone(session.model);
      const node = model.nodes.find((n) => n.id === action.id);
      if (!node) return session;
      const x = Math.round(action.position.x);
      const y = Math.round(action.position.y);
      if (node.x === x && node.y === y) return session;
      node.x = x;
      node.y = y;
      return commit(session, model);
    }

    // 自动布局：positions 是 {id: {x, y}}（节点中心）
    case 'set-positions': {
      const model = clone(session.model);
      model.nodes.forEach((n) => {
        const p = action.positions?.[n.id];
        if (p) { n.x = Math.round(p.x); n.y = Math.round(p.y); }
      });
      if (same(model, session.model)) return session;
      return commit(session, model);
    }

    default:
      return session;
  }
}
