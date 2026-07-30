import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  createFlowchartEditorSession,
  reduceFlowchartEditorSession,
} from '../frontend/src/flowchart-editor/flowchartEditorState.js';

function sampleDocument() {
  return {
    diagrams: [{
      id: 'approval',
      title: '审批流程',
      nodes: [
        { id: 'start', kind: 'start', label: '收到申请' },
        { id: 'finish', kind: 'end', label: '完成审批' },
      ],
      edges: [{
        id: 'edge-start-finish',
        from: 'start',
        to: 'finish',
        kind: 'sequence',
        label: '提交',
        conditionText: '',
      }],
      layout: {
        direction: 'TB',
        positions: {
          start: { x: 80, y: 40 },
          finish: { x: 80, y: 220 },
        },
      },
    }],
  };
}

test('新增节点产生稳定 ID、初始位置并进入未保存状态', () => {
  const initial = createFlowchartEditorSession(sampleDocument(), { savedRevision: 4 });
  const next = reduceFlowchartEditorSession(initial, {
    type: 'add-node',
    diagramId: 'approval',
    node: { kind: 'action', label: '核验材料' },
    position: { x: 260, y: 130 },
  });

  const diagram = next.document.diagrams[0];
  assert.deepEqual(diagram.nodes.at(-1), {
    id: 'node-1',
    kind: 'action',
    label: '核验材料',
    detail: '',
  });
  assert.deepEqual(diagram.layout.positions['node-1'], { x: 260, y: 130 });
  assert.deepEqual(next.selection, { type: 'node', diagramId: 'approval', id: 'node-1' });
  assert.equal(next.dirty, true);
  assert.equal(next.saveStatus, 'dirty');
  assert.equal(next.savedRevision, 4);
});

test('连接节点生成可独立编辑的边并保存流转文字与条件', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'connect-nodes',
    diagramId: 'approval',
    from: 'finish',
    to: 'start',
    edge: {
      kind: 'exception',
      label: '退回补充',
      conditionText: '材料不完整或信息有冲突',
    },
  });

  assert.deepEqual(next.document.diagrams[0].edges.at(-1), {
    id: 'edge-1',
    from: 'finish',
    to: 'start',
    kind: 'exception',
    label: '退回补充',
    conditionText: '材料不完整或信息有冲突',
  });
  assert.deepEqual(next.selection, { type: 'edge', diagramId: 'approval', id: 'edge-1' });
});

test('拖线到空白处一次生成新节点、位置和相连边', () => {
  const initial = createFlowchartEditorSession(sampleDocument(), { savedRevision: 4 });
  const next = reduceFlowchartEditorSession(initial, {
    type: 'spawn-connected-node',
    diagramId: 'approval',
    from: 'start',
    node: { kind: 'action', label: '新建步骤' },
    position: { x: 320, y: 200 },
  });

  const diagram = next.document.diagrams[0];
  assert.deepEqual(diagram.nodes.at(-1), {
    id: 'node-1', kind: 'action', label: '新建步骤', detail: '',
  });
  assert.deepEqual(diagram.layout.positions['node-1'], { x: 320, y: 200 });
  assert.deepEqual(diagram.edges.at(-1), {
    id: 'edge-1', from: 'start', to: 'node-1',
    kind: 'sequence', label: '下一步', conditionText: '',
  });
  assert.deepEqual(next.selection, { type: 'node', diagramId: 'approval', id: 'node-1' });
  assert.equal(next.dirty, true);

  // 撤销一步即可同时移除新节点和相连边
  const undone = reduceFlowchartEditorSession(next, { type: 'undo' });
  assert.equal(undone.document.diagrams[0].nodes.length, 2);
  assert.equal(undone.document.diagrams[0].edges.length, 1);
});

test('拖线到空白处时起点节点不存在则不产生修改', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'spawn-connected-node',
    diagramId: 'approval',
    from: 'missing',
    node: { kind: 'action', label: '新建步骤' },
    position: { x: 0, y: 0 },
  });
  assert.equal(next, initial);
});

test('重连边只改起止节点且可撤销', () => {
  const withExtraNode = reduceFlowchartEditorSession(
    createFlowchartEditorSession(sampleDocument()),
    {
      type: 'add-node', diagramId: 'approval',
      node: { kind: 'action', label: '复核' }, position: { x: 400, y: 120 },
    },
  );
  const next = reduceFlowchartEditorSession(withExtraNode, {
    type: 'reconnect-edge',
    diagramId: 'approval',
    id: 'edge-start-finish',
    from: 'node-1',
    to: 'finish',
  });

  const edge = next.document.diagrams[0].edges.find((item) => item.id === 'edge-start-finish');
  assert.equal(edge.from, 'node-1');
  assert.equal(edge.to, 'finish');
  assert.deepEqual(next.selection, { type: 'edge', diagramId: 'approval', id: 'edge-start-finish' });

  const undone = reduceFlowchartEditorSession(next, { type: 'undo' });
  const restored = undone.document.diagrams[0].edges.find((item) => item.id === 'edge-start-finish');
  assert.equal(restored.from, 'start');
  assert.equal(restored.to, 'finish');
});

test('重连边指向不存在的节点时不产生修改', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'reconnect-edge',
    diagramId: 'approval',
    id: 'edge-start-finish',
    from: 'start',
    to: 'missing',
  });
  assert.equal(next, initial);
});

test('修改节点业务属性时保持稳定 ID 和布局位置', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'update-node',
    diagramId: 'approval',
    id: 'start',
    patch: { kind: 'action', label: '登记申请', detail: '记录申请人和材料清单' },
  });

  assert.deepEqual(next.document.diagrams[0].nodes[0], {
    id: 'start',
    kind: 'action',
    label: '登记申请',
    detail: '记录申请人和材料清单',
  });
  assert.deepEqual(next.document.diagrams[0].layout.positions.start, { x: 80, y: 40 });
});

test('空白流程可以修改流程名称和说明后保存', () => {
  const initial = createFlowchartEditorSession({
    diagrams: [{ id: 'blank', title: '未命名流程', nodes: [], edges: [] }],
  }, { savedRevision: 1 });
  const updated = reduceFlowchartEditorSession(initial, {
    type: 'update-diagram', diagramId: 'blank',
    patch: { title: '补充审批流程', description: '处理线下补充审批' },
  });
  assert.equal(updated.document.diagrams[0].title, '补充审批流程');
  assert.equal(updated.document.diagrams[0].description, '处理线下补充审批');
  assert.equal(updated.dirty, true);
});

test('流程属性可以结构化修改角色、输入、异常和交接', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const updated = reduceFlowchartEditorSession(initial, {
    type: 'update-diagram', diagramId: 'approval',
    patch: {
      processMeta: {
        actors: ['复核人', '审批人'],
        inputs: ['初审结果', '终审材料'],
        exceptions: ['材料冲突时退回'],
        handoffs: ['审批人通知经办人'],
      },
    },
  });

  assert.deepEqual(updated.document.diagrams[0].processMeta, {
    actors: ['复核人', '审批人'],
    inputs: ['初审结果', '终审材料'],
    exceptions: ['材料冲突时退回'],
    handoffs: ['审批人通知经办人'],
  });
  assert.equal(updated.dirty, true);
});

test('修改边时分别保存图上文字和完整业务条件', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'update-edge',
    diagramId: 'approval',
    id: 'edge-start-finish',
    patch: {
      kind: 'condition',
      label: '审批通过',
      conditionText: '风险等级不是高风险，并且资料核验无异常',
    },
  });

  assert.deepEqual(next.document.diagrams[0].edges[0], {
    id: 'edge-start-finish',
    from: 'start',
    to: 'finish',
    kind: 'condition',
    label: '审批通过',
    conditionText: '风险等级不是高风险，并且资料核验无异常',
  });
});

test('删除节点时级联删除所有关联边和布局位置', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'delete-node',
    diagramId: 'approval',
    id: 'start',
  });

  assert.deepEqual(next.document.diagrams[0].nodes.map((node) => node.id), ['finish']);
  assert.deepEqual(next.document.diagrams[0].edges, []);
  assert.equal(next.document.diagrams[0].layout.positions.start, undefined);
  assert.equal(next.selection, null);
});

test('删除边不影响其连接的节点', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'delete-edge',
    diagramId: 'approval',
    id: 'edge-start-finish',
  });

  assert.deepEqual(next.document.diagrams[0].edges, []);
  assert.deepEqual(next.document.diagrams[0].nodes.map((node) => node.id), ['start', 'finish']);
});

test('拖动节点只更新布局位置并可随文档保存', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'move-node',
    diagramId: 'approval',
    id: 'finish',
    position: { x: 420, y: 360 },
  });

  assert.deepEqual(next.document.diagrams[0].layout.positions.finish, { x: 420, y: 360 });
  assert.deepEqual(next.document.diagrams[0].nodes[1], {
    id: 'finish',
    kind: 'end',
    label: '完成审批',
  });
});

test('撤销和重做恢复完整文档与脏状态', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const changed = reduceFlowchartEditorSession(initial, {
    type: 'add-node',
    diagramId: 'approval',
    node: { kind: 'decision', label: '是否通过' },
    position: { x: 200, y: 140 },
  });
  const undone = reduceFlowchartEditorSession(changed, { type: 'undo' });
  const redone = reduceFlowchartEditorSession(undone, { type: 'redo' });

  assert.equal(undone.document.diagrams[0].nodes.length, 2);
  assert.equal(undone.dirty, false);
  assert.equal(undone.saveStatus, 'saved');
  assert.equal(redone.document.diagrams[0].nodes.at(-1).label, '是否通过');
  assert.equal(redone.dirty, true);
  assert.equal(redone.saveStatus, 'dirty');
});

test('保存成功后更新服务端修订并把当前文档设为新基线', () => {
  const initial = createFlowchartEditorSession(sampleDocument(), { savedRevision: 4 });
  const changed = reduceFlowchartEditorSession(initial, {
    type: 'update-node',
    diagramId: 'approval',
    id: 'start',
    patch: { label: '受理申请' },
  });
  const saving = reduceFlowchartEditorSession(changed, { type: 'mark-saving' });
  const saved = reduceFlowchartEditorSession(saving, { type: 'mark-saved', savedRevision: 5 });

  assert.equal(saving.saveStatus, 'saving');
  assert.equal(saved.savedRevision, 5);
  assert.equal(saved.dirty, false);
  assert.equal(saved.saveStatus, 'saved');
  assert.equal(reduceFlowchartEditorSession(saved, { type: 'undo' }).dirty, true);
});

test('保存请求期间继续编辑时保留后续修改并只更新已保存基线', () => {
  const initial = createFlowchartEditorSession(sampleDocument(), { savedRevision: 4 });
  const beforeSave = reduceFlowchartEditorSession(initial, {
    type: 'update-node', diagramId: 'approval', id: 'start', patch: { label: '受理申请' },
  });
  const saving = reduceFlowchartEditorSession(beforeSave, { type: 'mark-saving' });
  const editedWhileSaving = reduceFlowchartEditorSession(saving, {
    type: 'update-node', diagramId: 'approval', id: 'finish', patch: { label: '归档并通知' },
  });
  const savedResponse = structuredClone(beforeSave.document);
  const completed = reduceFlowchartEditorSession(editedWhileSaving, {
    type: 'mark-saved', document: savedResponse, savedRevision: 5,
  });

  assert.equal(completed.document.diagrams[0].nodes[0].label, '受理申请');
  assert.equal(completed.document.diagrams[0].nodes[1].label, '归档并通知');
  assert.equal(completed.savedDocument.diagrams[0].nodes[1].label, '完成审批');
  assert.equal(completed.savedRevision, 5);
  assert.equal(completed.dirty, true);
  assert.equal(completed.saveStatus, 'dirty');
});

test('规范流程图缺少节点或边 ID 时生成可重复的稳定 ID', () => {
  const input = {
    diagrams: [{
      id: 'approval',
      title: '审批流程',
      nodes: [
        { kind: 'start', label: '开始' },
        { id: 'done', kind: 'end', label: '结束' },
      ],
      edges: [{ from: 'node-1', to: 'done', label: '完成' }],
    }],
  };

  const first = createFlowchartEditorSession(input).document;
  const second = createFlowchartEditorSession(input).document;
  assert.deepEqual(first, second);
  assert.equal(first.diagrams[0].nodes[0].id, 'node-1');
  assert.equal(first.diagrams[0].edges[0].id, 'edge-1');
  assert.equal(first.diagrams[0].edges[0].conditionText, '');
});

test('新建空白流程图使用稳定 ID 并成为当前流程', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const next = reduceFlowchartEditorSession(initial, {
    type: 'add-diagram',
    title: '异常处理流程',
  });

  assert.equal(next.document.diagrams.at(-1).id, 'diagram-1');
  assert.equal(next.document.diagrams.at(-1).title, '异常处理流程');
  assert.deepEqual(next.document.diagrams.at(-1).nodes, []);
  assert.equal(next.activeDiagramId, 'diagram-1');
  assert.equal(next.dirty, true);
});

test('删除当前业务流程后选择相邻流程并支持删除最后一条流程', () => {
  const document = sampleDocument();
  document.diagrams.push({
    id: 'audit', title: '复核流程', nodes: [], edges: [],
    layout: { direction: 'TB', positions: {} },
  });
  const selected = reduceFlowchartEditorSession(
    createFlowchartEditorSession(document, { savedRevision: 2 }),
    { type: 'select-diagram', diagramId: 'audit' },
  );
  const deleted = reduceFlowchartEditorSession(selected, {
    type: 'delete-diagram', diagramId: 'audit',
  });

  assert.deepEqual(deleted.document.diagrams.map((diagram) => diagram.id), ['approval']);
  assert.equal(deleted.activeDiagramId, 'approval');
  assert.equal(deleted.selection, null);
  assert.equal(deleted.dirty, true);

  const empty = reduceFlowchartEditorSession(deleted, {
    type: 'delete-diagram', diagramId: 'approval',
  });
  assert.deepEqual(empty.document.diagrams, []);
  assert.equal(empty.activeDiagramId, '');
  assert.equal(empty.dirty, true);
});

test('自动布局可以一次性保存全部节点位置并参与撤销', () => {
  const initial = createFlowchartEditorSession(sampleDocument());
  const changed = reduceFlowchartEditorSession(initial, {
    type: 'set-layout',
    diagramId: 'approval',
    direction: 'LR',
    positions: {
      start: { x: 40, y: 120 },
      finish: { x: 320, y: 120 },
    },
  });

  assert.equal(changed.document.diagrams[0].layout.direction, 'LR');
  assert.deepEqual(changed.document.diagrams[0].layout.positions.finish, { x: 320, y: 120 });
  assert.deepEqual(
    reduceFlowchartEditorSession(changed, { type: 'undo' }).document.diagrams[0].layout.positions.finish,
    { x: 80, y: 220 },
  );
});

test('服务端返回新草稿时仅在本地无未保存修改时替换编辑会话', () => {
  const initial = createFlowchartEditorSession(sampleDocument(), { savedRevision: 3 });
  const replacement = {
    diagrams: [{ id: 'new-flow', title: '新流程', nodes: [], edges: [] }],
  };
  const replaced = reduceFlowchartEditorSession(initial, {
    type: 'replace-document', document: replacement, savedRevision: 4,
  });
  const dirty = reduceFlowchartEditorSession(initial, {
    type: 'update-node', diagramId: 'approval', id: 'start', patch: { label: '本地修改' },
  });
  const protectedLocal = reduceFlowchartEditorSession(dirty, {
    type: 'replace-document', document: replacement, savedRevision: 4,
  });

  assert.equal(replaced.activeDiagramId, 'new-flow');
  assert.equal(replaced.savedRevision, 4);
  assert.equal(replaced.dirty, false);
  assert.equal(protectedLocal.document.diagrams[0].nodes[0].label, '本地修改');
});

test('从 .mmd 打开时停在被点击的那张图，非法 id 退回第一张', () => {
  const document = {
    diagrams: [
      { id: 'intake', title: '受理流程', nodes: [], edges: [] },
      { id: 'approval', title: '审批流程', nodes: [], edges: [] },
    ],
  };

  assert.equal(createFlowchartEditorSession(document).activeDiagramId, 'intake');
  assert.equal(
    createFlowchartEditorSession(document, { activeDiagramId: 'approval' }).activeDiagramId,
    'approval',
  );
  // .mmd 与源文档不同步时不能停在空图上
  assert.equal(
    createFlowchartEditorSession(document, { activeDiagramId: 'ghost' }).activeDiagramId,
    'intake',
  );
});
