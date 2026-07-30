import dagre from '@dagrejs/dagre';
import {
  Background,
  ConnectionMode,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
} from '@xyflow/react';
import {
  Diamond,
  LayoutDashboard,
  Plus,
  Redo2,
  Trash2,
  Undo2,
} from 'lucide-react';
import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import AddFlowchartCard from './AddFlowchartCard.jsx';
import {
  createFlowchartEditorSession,
  reduceFlowchartEditorSession,
} from './flowchartEditorState.js';

const NODE_WIDTH = 190;
const NODE_HEIGHT = 72;

const NODE_KIND_LABELS = {
  start: '开始',
  end: '结束',
  action: '业务步骤',
  decision: '判断',
  object: '数据对象',
  data: '数据',
};

const EDGE_KIND_LABELS = {
  sequence: '顺序流转',
  condition: '条件分支',
  exception: '异常流转',
  data: '数据流转',
};

function fallbackPositions(diagram) {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: diagram.layout?.direction || 'TB',
    ranksep: 60,
    nodesep: 44,
    marginx: 36,
    marginy: 36,
  });
  diagram.nodes.forEach((node) => graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  const nodeIds = new Set(diagram.nodes.map((node) => node.id));
  diagram.edges.forEach((edge) => {
    if (nodeIds.has(edge.from) && nodeIds.has(edge.to)) graph.setEdge(edge.from, edge.to);
  });
  dagre.layout(graph);
  return Object.fromEntries(diagram.nodes.map((node) => {
    const position = graph.node(node.id);
    return [node.id, {
      x: position.x - NODE_WIDTH / 2,
      y: position.y - NODE_HEIGHT / 2,
    }];
  }));
}

function displayEdgeLabel(edge) {
  if (edge.label && edge.conditionText) return `${edge.label} · ${edge.conditionText}`;
  return edge.label || edge.conditionText || '';
}

function metadataText(values) {
  return Array.isArray(values) ? values.join('\n') : '';
}

function parseMetadataText(value) {
  return value.split(/(?:\r?\n|[；;,，])/u).map((item) => item.trim()).filter(Boolean);
}

function isEditableTarget(target) {
  return Boolean(target?.closest?.('input, textarea, select, [contenteditable="true"]'));
}

// 自定义节点：四向连线手柄 + 双击就地改名。拖动任意手柄到目标节点或空白处即可连线。
function FlowNode({ id, data, selected }) {
  const [draft, setDraft] = useState(null);
  const inputRef = useRef(null);
  const editing = draft !== null;

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const commit = (value) => {
    const label = value.trim();
    if (label && label !== data.label) data.onRename?.(id, label);
    setDraft(null);
  };

  return (
    <div className={`fc-node is-${data.kind || 'action'}${selected ? ' is-selected' : ''}`}
      onDoubleClick={(event) => {
        event.stopPropagation();
        if (!data.disabled) setDraft(data.label);
      }}>
      <Handle type="target" position={Position.Top} id="t" className="fc-handle" isConnectable={!data.disabled} />
      <Handle type="source" position={Position.Right} id="r" className="fc-handle" isConnectable={!data.disabled} />
      <Handle type="source" position={Position.Bottom} id="b" className="fc-handle" isConnectable={!data.disabled} />
      <Handle type="target" position={Position.Left} id="l" className="fc-handle" isConnectable={!data.disabled} />
      <span className="fc-node__kind">{NODE_KIND_LABELS[data.kind] || NODE_KIND_LABELS.action}</span>
      {editing ? (
        <input
          ref={inputRef}
          className="fc-node__rename nodrag"
          aria-label="节点名称"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={(event) => commit(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') { event.preventDefault(); commit(event.currentTarget.value); }
            if (event.key === 'Escape') { event.preventDefault(); setDraft(null); }
          }}
        />
      ) : (
        <strong className="fc-node__label" title="双击修改名称">{data.label}</strong>
      )}
      {data.detail && !editing && <small className="fc-node__detail">{data.detail}</small>}
    </div>
  );
}

const nodeTypes = { flow: FlowNode };

function graphElements(diagram, selection, handlers) {
  if (!diagram) return { nodes: [], edges: [] };
  const fallback = fallbackPositions(diagram);
  const nodeIds = new Set(diagram.nodes.map((node) => node.id));
  return {
    nodes: diagram.nodes.map((node) => ({
      id: node.id,
      type: 'flow',
      position: diagram.layout?.positions?.[node.id] || fallback[node.id],
      selected: selection?.type === 'node' && selection.id === node.id,
      data: {
        kind: node.kind || 'action',
        label: node.label,
        detail: node.detail || '',
        disabled: handlers.disabled,
        onRename: handlers.onRename,
      },
    })),
    edges: diagram.edges
      .filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to))
      .map((edge) => ({
        id: edge.id,
        source: edge.from,
        target: edge.to,
        label: displayEdgeLabel(edge),
        type: 'smoothstep',
        interactionWidth: 20,
        reconnectable: !handlers.disabled,
        selected: selection?.type === 'edge' && selection.id === edge.id,
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
        className: `flowchart-editor-edge is-${edge.kind || 'sequence'}`,
      })),
  };
}

function nextNodePosition(diagram) {
  const positions = Object.values(diagram.layout?.positions ?? {});
  if (!positions.length) {
    return { x: 160 + diagram.nodes.length * 24, y: 100 + diagram.nodes.length * 100 };
  }
  const direction = diagram.layout?.direction || 'TB';
  if (direction === 'LR') {
    return {
      x: Math.max(...positions.map((position) => position.x)) + NODE_WIDTH + 80,
      y: positions.reduce((sum, position) => sum + position.y, 0) / positions.length,
    };
  }
  return {
    x: positions.reduce((sum, position) => sum + position.x, 0) / positions.length,
    y: Math.max(...positions.map((position) => position.y)) + NODE_HEIGHT + 80,
  };
}

function stateSummary(session) {
  return {
    document: session.document,
    dirty: session.dirty,
    saveStatus: session.saveStatus,
    saveError: session.saveError,
    savedRevision: session.savedRevision,
    activeDiagramId: session.activeDiagramId,
    canUndo: session.past.length > 0,
    canRedo: session.future.length > 0,
  };
}

function ElementInspector({ selection, diagram, dispatch, disabled = false }) {
  if (!diagram) {
    return (
      <aside className="flowchart-editor-inspector" aria-label="流程图属性">
        <div className="flowchart-editor-inspector__head"><strong>属性</strong></div>
        <div className="flowchart-editor-inspector__empty">
          请先添加一张流程图。
        </div>
      </aside>
    );
  }
  if (!selection || selection.diagramId !== diagram.id) {
    const update = (patch) => dispatch({ type: 'update-diagram', diagramId: diagram.id, patch });
    const processMeta = diagram.processMeta || {};
    const updateMeta = (key, value) => update({
      processMeta: { [key]: parseMetadataText(value) },
    });
    return (
      <aside className="flowchart-editor-inspector" aria-label="流程图属性">
        <div className="flowchart-editor-inspector__head"><strong>流程属性</strong><span>当前流程</span></div>
        <label>
          <span>流程名称</span>
          <input value={diagram.title} disabled={disabled}
            onChange={(event) => update({ title: event.target.value })} />
        </label>
        <label>
          <span>流程说明</span>
          <textarea rows={4} value={diagram.description || ''} disabled={disabled}
            onChange={(event) => update({ description: event.target.value })} />
          <small>选择节点或连线后，可继续编辑对应业务属性。</small>
        </label>
        <label>
          <span>参与角色</span>
          <textarea rows={3} value={metadataText(processMeta.actors)} disabled={disabled}
            placeholder="每行一个角色"
            onChange={(event) => updateMeta('actors', event.target.value)} />
        </label>
        <label>
          <span>流程输入</span>
          <textarea rows={3} value={metadataText(processMeta.inputs)} disabled={disabled}
            placeholder="每行一项输入"
            onChange={(event) => updateMeta('inputs', event.target.value)} />
        </label>
        <label>
          <span>异常处理</span>
          <textarea rows={3} value={metadataText(processMeta.exceptions)} disabled={disabled}
            placeholder="每行一种异常或处理方式"
            onChange={(event) => updateMeta('exceptions', event.target.value)} />
        </label>
        <label>
          <span>流程交接</span>
          <textarea rows={3} value={metadataText(processMeta.handoffs)} disabled={disabled}
            placeholder="每行一项交接说明"
            onChange={(event) => updateMeta('handoffs', event.target.value)} />
          <small>触发条件请编辑开始节点，流程输出请编辑结束节点。</small>
        </label>
      </aside>
    );
  }

  if (selection.type === 'node') {
    const node = diagram.nodes.find((candidate) => candidate.id === selection.id);
    if (!node) return null;
    const update = (patch) => dispatch({
      type: 'update-node', diagramId: diagram.id, id: node.id, patch,
    });
    return (
      <aside className="flowchart-editor-inspector" aria-label="节点属性">
        <div className="flowchart-editor-inspector__head">
          <strong>节点属性</strong><span>{NODE_KIND_LABELS[node.kind] || '节点'}</span>
        </div>
        <label>
          <span>节点名称</span>
          <input value={node.label} disabled={disabled}
            onChange={(event) => update({ label: event.target.value })} />
        </label>
        <label>
          <span>节点类型</span>
          <select value={node.kind} disabled={disabled}
            onChange={(event) => update({ kind: event.target.value })}>
            {Object.entries(NODE_KIND_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          <span>业务说明</span>
          <textarea rows={5} value={node.detail || ''} disabled={disabled}
            onChange={(event) => update({ detail: event.target.value })} />
          <small>帮助模型理解节点职责，不直接显示在图上。</small>
        </label>
      </aside>
    );
  }

  const edge = diagram.edges.find((candidate) => candidate.id === selection.id);
  if (!edge) return null;
  const update = (patch) => dispatch({
    type: 'update-edge', diagramId: diagram.id, id: edge.id, patch,
  });
  return (
    <aside className="flowchart-editor-inspector" aria-label="连线属性">
      <div className="flowchart-editor-inspector__head">
        <strong>流转条件</strong><span>{EDGE_KIND_LABELS[edge.kind] || '连线'}</span>
      </div>
      <label>
        <span>图上文字</span>
        <input value={edge.label || ''} placeholder="如：审批通过" disabled={disabled}
          onChange={(event) => update({ label: event.target.value })} />
      </label>
      <label>
        <span>流转类型</span>
        <select value={edge.kind} disabled={disabled}
          onChange={(event) => update({ kind: event.target.value })}>
          {Object.entries(EDGE_KIND_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>
      <label>
        <span>业务逻辑或条件</span>
        <textarea rows={6} value={edge.conditionText || ''} disabled={disabled}
          placeholder="如：风险等级不是高风险，并且资料核验无异常"
          onChange={(event) => update({ conditionText: event.target.value })} />
        <small>完整条件写在这里，画布上仍显示简短文字。</small>
      </label>
    </aside>
  );
}

export default function FlowchartEditor({
  document,
  savedRevision = null,
  initialDiagramId = '',
  onSave,
  onStateChange,
  onRecommendDiagram,
  onCreateBlankDiagram,
  disabled = false,
}) {
  const [session, dispatch] = useReducer(
    reduceFlowchartEditorSession,
    null,
    () => createFlowchartEditorSession(document, { savedRevision, activeDiagramId: initialDiagramId }),
  );
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const flowInstanceRef = useRef(null);
  const activeDiagram = session.document.diagrams.find(
    (diagram) => diagram.id === session.activeDiagramId,
  ) || session.document.diagrams[0];

  const renameNode = useCallback((nodeId, label) => {
    if (!activeDiagram) return;
    dispatch({
      type: 'update-node',
      diagramId: activeDiagram.id,
      id: nodeId,
      patch: { label },
    });
  }, [activeDiagram?.id]);

  const graph = useMemo(
    () => graphElements(activeDiagram, session.selection, { disabled, onRename: renameNode }),
    [activeDiagram, session.selection, disabled, renameNode],
  );

  useEffect(() => {
    dispatch({ type: 'replace-document', document, savedRevision });
  }, [document, savedRevision]);

  useEffect(() => {
    onStateChangeRef.current?.(stateSummary(session));
  }, [session]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      flowInstanceRef.current?.fitView({ padding: 0.22, duration: 180 });
    });
    return () => cancelAnimationFrame(frame);
  }, [activeDiagram?.id]);

  // Ctrl/Cmd+Z 撤销、Ctrl/Cmd+Shift+Z 或 Ctrl+Y 重做；输入框内不劫持。
  useEffect(() => {
    const onKeyDown = (event) => {
      if (disabled || isEditableTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && !event.altKey) {
        if (key === 'z' && !event.shiftKey) { event.preventDefault(); dispatch({ type: 'undo' }); }
        if ((key === 'z' && event.shiftKey) || key === 'y') { event.preventDefault(); dispatch({ type: 'redo' }); }
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [disabled]);

  const addNode = (kind) => {
    if (!activeDiagram) return;
    dispatch({
      type: 'add-node',
      diagramId: activeDiagram.id,
      node: { kind, label: kind === 'decision' ? '新建判断' : '新建步骤' },
      position: nextNodePosition(activeDiagram),
    });
  };

  const deleteSelection = () => {
    const selection = session.selection;
    if (!selection) return;
    dispatch({
      type: selection.type === 'node' ? 'delete-node' : 'delete-edge',
      diagramId: selection.diagramId,
      id: selection.id,
    });
  };

  const deleteActiveDiagram = () => {
    if (!activeDiagram || disabled) return;
    const emptyHint = session.document.diagrams.length === 1
      ? '删除后流程列表将为空，可再新建流程。'
      : '删除后将自动切换到其他流程。';
    if (!window.confirm(`确定删除业务流程“${activeDiagram.title}”吗？${emptyHint}\n保存草稿后删除才会生效。`)) return;
    dispatch({ type: 'delete-diagram', diagramId: activeDiagram.id });
  };

  const save = async () => {
    if (!session.dirty || session.saveStatus === 'saving' || !onSave) return;
    dispatch({ type: 'mark-saving' });
    try {
      const result = await onSave(session.document, { expectedRevision: session.savedRevision });
      dispatch({
        type: 'mark-saved',
        document: result?.document,
        savedRevision: result?.savedRevision ?? result?.draftRevision,
      });
    } catch (error) {
      dispatch({ type: 'mark-save-failed', message: error?.message || '保存失败' });
    }
  };

  const createBlank = () => {
    if (onCreateBlankDiagram) {
      onCreateBlankDiagram();
      return;
    }
    dispatch({ type: 'add-diagram', title: '未命名流程' });
  };

  const applyAutoLayout = () => {
    if (!activeDiagram) return;
    dispatch({
      type: 'set-layout',
      diagramId: activeDiagram.id,
      direction: activeDiagram.layout?.direction || 'TB',
      positions: fallbackPositions(activeDiagram),
    });
  };

  // React Flow 的删除键、点选都会走 change 通道，这里统一映射回编辑器状态机。
  const onNodesChange = useCallback((changes) => {
    if (!activeDiagram) return;
    let selectedNodeId = null;
    let deselected = false;
    changes.forEach((change) => {
      if (change.type === 'remove' && !disabled) {
        dispatch({ type: 'delete-node', diagramId: activeDiagram.id, id: change.id });
      } else if (change.type === 'select') {
        if (change.selected) selectedNodeId = change.id;
        else deselected = true;
      }
    });
    if (selectedNodeId) {
      dispatch({
        type: 'select-element',
        selection: { type: 'node', diagramId: activeDiagram.id, id: selectedNodeId },
      });
    } else if (deselected) {
      dispatch({ type: 'select-element', selection: null });
    }
  }, [activeDiagram?.id, disabled]);

  const onEdgesChange = useCallback((changes) => {
    if (!activeDiagram) return;
    let selectedEdgeId = null;
    let deselected = false;
    changes.forEach((change) => {
      if (change.type === 'remove' && !disabled) {
        dispatch({ type: 'delete-edge', diagramId: activeDiagram.id, id: change.id });
      } else if (change.type === 'select') {
        if (change.selected) selectedEdgeId = change.id;
        else deselected = true;
      }
    });
    if (selectedEdgeId) {
      dispatch({
        type: 'select-element',
        selection: { type: 'edge', diagramId: activeDiagram.id, id: selectedEdgeId },
      });
    } else if (deselected) {
      dispatch({ type: 'select-element', selection: null });
    }
  }, [activeDiagram?.id, disabled]);

  // 禁止自连和重复边；ConnectionMode.Loose 下任意手柄都可以作为起点或终点。
  const isValidConnection = useCallback((connection) => {
    if (!activeDiagram) return false;
    const { source, target } = connection;
    if (!source || !target || source === target) return false;
    return !activeDiagram.edges.some((edge) => edge.from === source && edge.to === target);
  }, [activeDiagram]);

  const onConnect = useCallback((connection) => {
    if (!activeDiagram || disabled) return;
    dispatch({
      type: 'connect-nodes',
      diagramId: activeDiagram.id,
      from: connection.source,
      to: connection.target,
      edge: { label: '下一步', kind: 'sequence', conditionText: '' },
    });
  }, [activeDiagram?.id, disabled]);

  // 拖线到画布空白处松手：在松手位置新建节点并自动连上，一步完成。
  const onConnectEnd = useCallback((event, connectionState) => {
    if (!activeDiagram || disabled) return;
    if (connectionState.isValid || !connectionState.fromNode) return;
    const target = event.target;
    if (!target?.classList?.contains('react-flow__pane')) return;
    const { clientX, clientY } = 'changedTouches' in event ? event.changedTouches[0] : event;
    const position = flowInstanceRef.current?.screenToFlowPosition({ x: clientX, y: clientY });
    if (!position) return;
    dispatch({
      type: 'spawn-connected-node',
      diagramId: activeDiagram.id,
      from: connectionState.fromNode.id,
      node: { kind: 'action', label: '新建步骤' },
      position: { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 },
    });
  }, [activeDiagram?.id, disabled]);

  const onReconnect = useCallback((oldEdge, connection) => {
    if (!activeDiagram || disabled) return;
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    dispatch({
      type: 'reconnect-edge',
      diagramId: activeDiagram.id,
      id: oldEdge.id,
      from: connection.source,
      to: connection.target,
    });
  }, [activeDiagram?.id, disabled]);

  const statusText = session.saveStatus === 'saving'
    ? '保存中…'
    : session.dirty
      ? '有未保存修改'
      : session.savedRevision == null ? '草稿已保存' : `草稿修订 ${session.savedRevision}`;

  return (
    <section className="flowchart-editor" data-dirty={session.dirty ? 'true' : 'false'}
      data-editor-locked={disabled ? 'true' : 'false'}>
      <nav className="flowchart-editor-rail" aria-label="流程图列表">
        <div className="flowchart-editor-rail__head">
          <strong>业务流程</strong><span>{session.document.diagrams.length}</span>
          <button type="button" className="flowchart-editor-rail__delete"
            title="删除当前业务流程" aria-label="删除当前业务流程"
            disabled={disabled || !activeDiagram || session.saveStatus === 'saving'}
            onClick={deleteActiveDiagram}><Trash2 size={14} /></button>
        </div>
        <div className="flowchart-editor-rail__items">
          {session.document.diagrams.map((diagram, index) => (
            <button key={diagram.id} type="button"
              className={`flowchart-editor-rail__item${diagram.id === activeDiagram?.id ? ' is-active' : ''}`}
              disabled={disabled}
              onClick={() => dispatch({ type: 'select-diagram', diagramId: diagram.id })}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <span><strong>{diagram.title}</strong><small>{diagram.nodes.length} 个节点 · {diagram.edges.length} 条流转</small></span>
            </button>
          ))}
          <AddFlowchartCard disabled={disabled || session.dirty || session.saveStatus === 'saving'}
            disabledReason={session.dirty
              ? '请先保存当前流程图修改，再继续添加'
              : session.saveStatus === 'saving'
                ? '草稿保存完成后可继续添加'
                : disabled ? '当前流程图正在处理，请稍后再试' : ''}
            onRecommend={onRecommendDiagram} onCreateBlank={createBlank} />
        </div>
      </nav>

      <div className="flowchart-editor-workspace">
        <header className="flowchart-editor-toolbar" aria-label="流程图编辑工具">
          <button type="button" title="新增业务步骤" aria-label="新增业务步骤"
            disabled={disabled || !activeDiagram} onClick={() => addNode('action')}><Plus size={16} /></button>
          <button type="button" title="新增判断节点" aria-label="新增判断节点"
            disabled={disabled || !activeDiagram} onClick={() => addNode('decision')}><Diamond size={16} /></button>
          <button type="button" title="删除选中节点或边（Delete）" aria-label="删除选中节点或边"
            disabled={disabled || !session.selection} onClick={deleteSelection}><Trash2 size={16} /></button>
          <span className="flowchart-editor-toolbar__divider" />
          <button type="button" title="撤销（Ctrl+Z）" aria-label="撤销"
            disabled={disabled || !session.past.length} onClick={() => dispatch({ type: 'undo' })}><Undo2 size={16} /></button>
          <button type="button" title="重做（Ctrl+Shift+Z）" aria-label="重做"
            disabled={disabled || !session.future.length} onClick={() => dispatch({ type: 'redo' })}><Redo2 size={16} /></button>
          <button type="button" title="自动布局" aria-label="自动布局"
            disabled={disabled || !activeDiagram?.nodes.length} onClick={applyAutoLayout}><LayoutDashboard size={16} /></button>
          <div className="flowchart-editor-toolbar__save">
            <span className={`is-${session.saveStatus}`}>{statusText}</span>
            <button type="button" className="flowchart-editor-save" disabled={disabled || !session.dirty || session.saveStatus === 'saving'}
              onClick={save}>保存草稿</button>
          </div>
        </header>
        {session.saveError && <div className="flowchart-editor-error" role="alert">{session.saveError}</div>}
        {!disabled && activeDiagram && (
          <div className="flowchart-editor-hint">拖动节点圆点即可连线；拖到空白处松手可新建相连节点；双击节点改名，Delete 删除选中项。</div>
        )}
        <div className="flowchart-editor-body">
          <div className="flowchart-editor-canvas" aria-label="流程图编辑画布">
            {activeDiagram ? (
              <ReactFlow nodes={graph.nodes} edges={graph.edges} fitView
                nodeTypes={nodeTypes}
                onInit={(instance) => { flowInstanceRef.current = instance; }}
                nodesDraggable={!disabled} nodesConnectable={!disabled} elementsSelectable
                connectionMode={ConnectionMode.Loose} connectionRadius={28}
                isValidConnection={isValidConnection}
                onConnect={onConnect} onConnectEnd={onConnectEnd} onReconnect={onReconnect}
                onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
                onNodeDragStop={(_, node) => dispatch({
                  type: 'move-node', diagramId: activeDiagram.id, id: node.id, position: node.position,
                })}
                onPaneClick={() => dispatch({ type: 'select-element', selection: null })}
                deleteKeyCode={disabled ? null : ['Backspace', 'Delete']}
                minZoom={0.3} maxZoom={1.8} proOptions={{ hideAttribution: true }}>
                <Background color="#dfe4eb" gap={20} size={1} />
                <Controls showInteractive={false} position="bottom-right" />
              </ReactFlow>
            ) : (
              <div className="flowchart-editor-canvas__empty">请先添加一张流程图</div>
            )}
          </div>
          <ElementInspector selection={session.selection} diagram={activeDiagram} dispatch={dispatch}
            disabled={disabled} />
        </div>
      </div>
    </section>
  );
}
