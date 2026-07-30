import assert from 'node:assert/strict';
import test from 'node:test';

import {
  appendBlankFlowchartDiagram,
  normalizeFlowchartDocument,
  removeFlowchartNode,
  validateFlowchartDocument,
} from '../src/flowchart/document.js';

test('规范流程图文档为缺失的连线补充稳定 ID', () => {
  const input = {
    diagrams: [{
      id: 'approval',
      title: '授信审批',
      processMeta: {
        trigger: '收到授信申请',
        actors: ['客户经理'],
      },
      nodes: [
        { id: 'start', label: '申请', kind: 'start' },
        { id: 'review', label: '审核资料', kind: 'action' },
      ],
      edges: [{ from: 'start', to: 'review', label: '提交' }],
    }],
  };

  const first = normalizeFlowchartDocument(input);
  const second = normalizeFlowchartDocument(input);

  assert.equal(first.diagrams[0].processMeta?.trigger, '申请');
  assert.deepEqual(first.diagrams[0].processMeta?.actors, ['客户经理']);
  assert.match(first.diagrams[0].edges[0].id, /^edge_[0-9a-f]{16}$/);
  assert.equal(first.diagrams[0].edges[0].kind, 'sequence');
  assert.equal(first.diagrams[0].edges[0].id, second.diagrams[0].edges[0].id);
});

test('只接受 processMeta 作为流程元数据结构', () => {
  const document = normalizeFlowchartDocument({
    diagrams: [{
      id: 'single-model', title: '唯一模型', actors: ['旧结构角色'],
      nodes: [
        { id: 'start', label: '开始', kind: 'start' },
        { id: 'end', label: '完成', kind: 'end' },
      ],
      edges: [{ id: 'edge', from: 'start', to: 'end', kind: 'sequence' }],
    }],
  });

  assert.equal(document.diagrams[0].processMeta?.actors, undefined);
  assert.equal(document.diagrams[0].processMeta?.trigger, '开始');
  assert.deepEqual(document.diagrams[0].processMeta?.outputs, ['完成']);
});

test('可以在已有文档末尾追加空白流程图', () => {
  const original = normalizeFlowchartDocument({ diagrams: [] });

  const changed = appendBlankFlowchartDiagram(original, { id: 'new-process', title: '新增流程' });

  assert.equal(changed.diagrams.length, 1);
  assert.deepEqual(changed.diagrams[0], {
    id: 'new-process', title: '新增流程', nodes: [], edges: [],
    layout: { direction: 'TB', positions: {} },
  });
  assert.equal(original.diagrams.length, 0);
});

test('单份流程图文档最多保存 30 张流程图', () => {
  const diagrams = Array.from({ length: 30 }, (_, index) => ({
    id: `process-${index + 1}`, title: `流程 ${index + 1}`, nodes: [], edges: [],
  }));
  const document = normalizeFlowchartDocument({ diagrams });
  assert.equal(document.diagrams.length, 30);

  assert.throws(
    () => appendBlankFlowchartDiagram(document, { id: 'process-31', title: '流程 31' }),
    /最多只能包含 30 张流程图/,
  );
  assert.throws(
    () => normalizeFlowchartDocument({
      diagrams: [...diagrams, { id: 'process-31', title: '流程 31', nodes: [], edges: [] }],
    }),
    /最多只能包含 30 张流程图/,
  );
});

test('业务不完整和循环只产生警告，不阻止流程图草稿', () => {
  const document = normalizeFlowchartDocument({
    diagrams: [{
      id: 'exception-loop',
      title: '异常重试',
      nodes: [
        { id: 'judge', label: '是否通过', kind: 'decision' },
        { id: 'retry', label: '重新处理', kind: 'action' },
      ],
      edges: [
        { id: 'edge-to-retry', from: 'judge', to: 'retry', kind: 'condition' },
        { id: 'edge-to-judge', from: 'retry', to: 'judge', kind: 'sequence' },
      ],
      layout: {
        direction: 'LR',
        positions: { judge: { x: 20, y: 40 }, retry: { x: 260, y: 40 } },
      },
    }],
  });

  const issues = validateFlowchartDocument(document);

  assert.equal(issues.some((issue) => issue.severity === 'error'), false);
  assert.deepEqual(
    new Set(issues.map((issue) => issue.code)),
    new Set(['missing_start', 'missing_end', 'unlabeled_decision_branch', 'cycle']),
  );
  assert.deepEqual(document.diagrams[0].layout?.positions.retry, { x: 260, y: 40 });
});

test('重复 ID 和悬空连线被识别为结构错误', () => {
  const document = normalizeFlowchartDocument({
    diagrams: [{
      id: 'broken',
      title: '待修流程',
      nodes: [
        { id: 'same', label: '步骤一', kind: 'action' },
        { id: 'same', label: '步骤二', kind: 'action' },
      ],
      edges: [
        { id: 'edge-a', from: 'same', to: 'missing', kind: 'sequence' },
        { id: 'edge-a', from: 'same', to: 'same', kind: 'sequence' },
      ],
    }],
  });

  const errors = validateFlowchartDocument(document)
    .filter((issue) => issue.severity === 'error')
    .map((issue) => issue.code);

  assert.deepEqual(errors, ['duplicate_node_id', 'missing_edge_endpoint', 'duplicate_edge_id']);
});

test('删除节点级联删除连线和布局且不修改原文档', () => {
  const original = normalizeFlowchartDocument({
    diagrams: [{
      id: 'edit',
      title: '编辑流程',
      nodes: [
        { id: 'a', label: '开始', kind: 'start' },
        { id: 'b', label: '处理', kind: 'action' },
        { id: 'c', label: '结束', kind: 'end' },
      ],
      edges: [
        { id: 'ab', from: 'a', to: 'b', kind: 'sequence' },
        { id: 'bc', from: 'b', to: 'c', kind: 'sequence' },
      ],
      layout: { positions: { a: { x: 0, y: 0 }, b: { x: 0, y: 100 }, c: { x: 0, y: 200 } } },
    }],
  });

  const changed = removeFlowchartNode(original, 'edit', 'b');

  assert.deepEqual(changed.diagrams[0].nodes.map((node) => node.id), ['a', 'c']);
  assert.deepEqual(changed.diagrams[0].edges, []);
  assert.deepEqual(Object.keys(changed.diagrams[0].layout?.positions ?? {}), ['a', 'c']);
  assert.equal(original.diagrams[0].nodes.length, 3);
  assert.equal(original.diagrams[0].edges.length, 2);
});

test('拒绝缺少流程图列表的文档', () => {
  assert.throws(
    () => normalizeFlowchartDocument({}),
    /流程图列表格式不正确/,
  );
});

test('保存时以开始和结束节点同步触发条件与流程输出', () => {
  const document = normalizeFlowchartDocument({
    diagrams: [{
      id: 'edited-process',
      title: '已编辑流程',
      processMeta: {
        trigger: '旧触发条件', actors: ['经办人'], inputs: ['申请'],
        outputs: ['旧输出'], exceptions: [], handoffs: [],
      },
      nodes: [
        { id: 'start', label: '每日定时触发', kind: 'start' },
        { id: 'work', label: '处理任务', kind: 'action' },
        { id: 'done', label: '形成日报', kind: 'end' },
      ],
      edges: [],
    }],
  });

  assert.equal(document.diagrams[0].processMeta?.trigger, '每日定时触发');
  assert.deepEqual(document.diagrams[0].processMeta?.outputs, ['形成日报']);
  assert.deepEqual(document.diagrams[0].processMeta?.actors, ['经办人']);
});

test('删除开始和结束节点后不沿用旧触发条件与输出', () => {
  const document = normalizeFlowchartDocument({
    diagrams: [{
      id: 'incomplete-process',
      title: '待补流程',
      processMeta: { trigger: '旧触发条件', actors: ['经办人'], outputs: ['旧输出'] },
      nodes: [{ id: 'work', label: '处理任务', kind: 'action' }],
      edges: [],
    }],
  });

  assert.equal(document.diagrams[0].processMeta?.trigger, undefined);
  assert.deepEqual(document.diagrams[0].processMeta?.outputs, []);
  assert.deepEqual(document.diagrams[0].processMeta?.actors, ['经办人']);
});
