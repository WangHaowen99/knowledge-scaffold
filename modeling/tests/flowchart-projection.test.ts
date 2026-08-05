import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeFlowchartDocument } from '../src/flowchart/document.js';
import {
  documentToInterviewOutcome,
  renderDiagramFiles,
} from '../src/flowchart/projection.js';

test('规范文档投影为访谈结果并渲染带条件文字的知识包文件', () => {
  const document = normalizeFlowchartDocument({
    diagrams: [{
      id: 'credit-approval',
      title: '授信审批',
      processMeta: {
        trigger: '收到申请', actors: ['客户经理'], inputs: ['申请材料'],
        outputs: ['审批结果'], exceptions: ['资料缺失'], handoffs: ['交给放款岗'],
      },
      nodes: [
        { id: 'start', label: '收到申请', kind: 'start' },
        { id: 'review', label: '审核资料', kind: 'decision' },
        { id: 'approve', label: '审批通过', kind: 'action' },
        { id: 'end', label: '生成结果', kind: 'end' },
      ],
      edges: [
        { id: 'start-review', from: 'start', to: 'review', kind: 'sequence' },
        {
          id: 'review-approve', from: 'review', to: 'approve', kind: 'condition',
          label: '通过', conditionText: '金额小于 100 万',
        },
        { id: 'approve-end', from: 'approve', to: 'end', kind: 'sequence' },
      ],
    }],
  });

  const outcome = documentToInterviewOutcome(document);
  const files = renderDiagramFiles(document);

  assert.deepEqual(outcome.processes[0].steps, [
    { id: 'review', label: '审核资料', kind: 'decision' },
    { id: 'approve', label: '审批通过', kind: 'action' },
  ]);
  assert.deepEqual(outcome.processes[0].transitions, [
    { from: 'review', to: 'approve', label: '通过 / 金额小于 100 万' },
  ]);
  assert.equal(outcome.processes[0].trigger, '收到申请');
  assert.deepEqual(outcome.processes[0].outputs, ['生成结果']);
  assert.match(files['diagrams/授信审批.mmd'], /review -->\|"通过 \/ 金额小于 100 万"\| approve/);
  assert.deepEqual(JSON.parse(files['diagrams/processes.json']), document);
  assert.match(files['diagrams/index.md'], /\[授信审批\]\(<授信审批\.mmd>\)/);
});

test('同名或不能安全落盘的流程名称拒绝生成派生文件', () => {
  const diagram = {
    id: 'one', title: '授信审批', nodes: [], edges: [],
  };
  assert.throws(
    () => renderDiagramFiles(normalizeFlowchartDocument({ diagrams: [
      diagram, { ...diagram, id: 'two' },
    ] })),
    /流程名称重复/u,
  );
  assert.throws(
    () => renderDiagramFiles(normalizeFlowchartDocument({ diagrams: [
      { ...diagram, title: '申请/审批' },
    ] })),
    /不能用于流程图文件名/u,
  );
});
