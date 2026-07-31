import assert from 'node:assert/strict';
import { test } from 'node:test';

import { buildFlowchart } from '../src/authoring/workflow-extras.js';
import { WORKFLOW_STEPS } from '../src/authoring/workflow-definition.js';
import { buildProjectWorkflow } from '../src/authoring/workflow-session.js';
import type { AuthoringSnapshot } from '../src/authoring/types.js';
import type { DocumentRecord, Project, WorkflowNode } from '../src/types.js';

const project: Project = {
  id: 'pj_process_first', owner: 'alice', kind: 'modeling', name: '流程建模',
  domain: '业务流程', background: '', sensitivityLevel: 'standard',
  workflowProfile: {
    enabledSteps: [], requiredSteps: ['scenario', 'data_source'],
    releaseRequested: true, externalResearch: false,
  },
  stage: 'interview', createdAt: '2026-07-23T00:00:00.000Z', updatedAt: '2026-07-23T00:00:00.000Z',
};

const document: DocumentRecord = {
  id: 'doc_process', projectId: project.id, filename: '流程说明.md', kind: 'markdown',
  parseStatus: 'parsed', textContent: '流程说明', createdAt: '2026-07-23T00:00:00.000Z',
};

const authoring: AuthoringSnapshot = {
  projectId: project.id, activeRuns: [], pendingInteractions: [], lastEventSeq: 0,
};

const interviewNode: WorkflowNode = {
  id: 'nd_interview_process', projectId: project.id, type: 'interview', status: 'done', ordinal: 1,
  title: '建模访谈', output: {
    summary: '已确认营销审批流程',
    processes: [{
      id: 'marketing-approval', title: '营销审批', trigger: '客户提交申请',
      actors: ['客户经理', '审批人'], inputs: ['客户申请'],
      steps: [
        { id: 'collect', label: '收集资料', kind: 'action' },
        { id: 'review', label: '审批判断', kind: 'decision' },
        { id: 'notify', label: '通知结果', kind: 'action' },
      ],
      transitions: [
        { from: 'collect', to: 'review' },
        { from: 'review', to: 'notify', label: '通过' },
      ],
      exceptions: ['资料不全时退回补充'], outputs: ['审批结果'], handoffs: ['交付客户经理'],
    }],
  },
  createdAt: '2026-07-23T00:00:00.000Z', updatedAt: '2026-07-23T00:00:00.000Z',
};

function workflow(progress: any[] = []) {
  return Object.fromEntries(buildProjectWorkflow({
    project, documents: [document], nodes: [interviewNode],
    currentPackage: null, packageCheck: null, dataSource: null, testcases: [], rounds: [],
    latestResults: [], diagnoses: [], suggestions: [], authoring, progress,
  }).steps.map((step) => [step.id, step])) as Record<string, any>;
}

test('主时间线直接使用唯一九步流程', () => {
  const visible = WORKFLOW_STEPS.filter((step) => step.linear !== false).map((step) => step.name);
  assert.deepEqual(visible.slice(0, 5), [
    '项目创建', '建模资料上传', 'AI辅助构建', '场景推演', '数据源绑定',
  ]);
  for (const removed of ['领域调研', '渐进提取', '专家访谈', '流程图绘制与确认', '知识包生成']) {
    assert.equal(visible.includes(removed), false);
  }
});

test('内部流程图状态不再作为第二套可见运行时暴露', () => {
  const reviewing = workflow([{
    projectId: project.id, stepId: 'flowchart', status: 'waiting_review',
    output: { sourceInterviewNodeId: interviewNode.id }, updatedAt: '2026-07-23T00:01:00.000Z',
  }]);
  const confirmed = workflow([{
    projectId: project.id, stepId: 'flowchart', status: 'completed',
    output: { sourceInterviewNodeId: interviewNode.id }, updatedAt: '2026-07-23T00:02:00.000Z',
  }]);
  assert.equal(reviewing.ai_construction.status, 'ready');
  assert.equal(confirmed.ai_construction.status, 'ready');
  assert.equal('flowchart' in reviewing, false);
  assert.equal('generate' in confirmed, false);
});

test('流程图直接由访谈确认的流程快照构建', () => {
  const output = buildFlowchart(undefined, undefined, (interviewNode.output as any).processes);
  assert.equal(output.diagrams.length, 1);
  assert.equal(output.diagrams[0].title, '营销审批');
  assert.deepEqual(output.diagrams[0].nodes.map((node) => [node.label, node.kind]), [
    ['客户提交申请', 'start'], ['收集资料', 'action'], ['审批判断', 'decision'],
    ['通知结果', 'action'], ['审批结果', 'end'],
  ]);
  assert.ok(output.diagrams[0].edges.some((edge) => edge.label === '通过'));
});

test('知识包中的 Mermaid 流程图默认使用竖向布局', async () => {
  const { buildProcessDiagramFiles } = await import('../src/ai-construction/process-interview.js');
  const files = buildProcessDiagramFiles(interviewNode.output as any);
  assert.match(files['diagrams/process_1.mmd'], /^flowchart TD/m);
  assert.doesNotMatch(files['diagrams/process_1.mmd'], /^flowchart LR/m);
});
