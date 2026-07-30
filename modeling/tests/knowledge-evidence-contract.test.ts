import assert from 'node:assert/strict';
import test from 'node:test';

import {
  attachGeneratedKnowledgeEvidence,
  findKnowledgeEvidenceProblems,
} from '../src/workflow/knowledge-evidence.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { applyPackageDraftFiles } from '../src/workflow/package-draft-parser.js';
import type { AnalysisResult, PackageSpec } from '../src/types.js';

const analysis: AnalysisResult = {
  domainOverview: '通用业务处理',
  candidateObjects: [],
  openQuestions: [],
  candidateArtifacts: [
    {
      kind: 'rule', name: '适用规则', rationale: '规范定义适用条件',
      sources: ['policy.md'], evidence: '适用条件章节', readiness: 'extractable',
    },
    {
      kind: 'metric', name: '处理时长', rationale: '规范定义计算口径',
      sources: ['calculation.md'], evidence: '计算口径章节', readiness: 'extractable',
    },
  ],
};

const baseSpec = (): PackageSpec => ({
  id: 'generic-agent', name: '通用业务处理', description: '处理通用业务请求',
  version: '0.1.0', language: 'zh-CN', defaultProviderAlias: 'data',
  objects: [{
    key: 'Request', title: '请求', description: '进入业务流程的请求记录',
    fields: [{ key: 'request_id', cnName: '请求编号' }],
    sourceRefs: [{ kind: 'document', filename: 'records.md', anchors: [{ heading: '请求记录' }] }],
    relations: [{ relation: 'handled_by', target: 'Handler', note: '请求由处理人承接' }],
  }, {
    key: 'Handler', title: '处理人', description: '负责处理请求的角色', fields: [],
    sourceRefs: [{ kind: 'document', filename: 'records.md', anchors: [{ heading: '处理角色' }] }],
  }],
  operations: [{
    key: 'HandleRequest', title: '处理请求', description: '按确认流程处理请求',
    competencyQuestionId: 'process_process_1',
    inputs: [], outputs: [], flow: ['接收 object:Request', '应用 rule:Eligibility'],
    relatedObjects: ['Request', 'Handler'], usesRules: ['Eligibility'], usesMetrics: ['Duration'],
  }],
  rules: [{ key: 'Eligibility', title: '适用规则', content: '满足已确认条件后进入处理。' }],
  metrics: [{ key: 'Duration', title: '处理时长', caliber: '结束时间减开始时间', boundaries: ['仅统计已完成请求'] }],
});

test('生成链路为流程、规则、指标和关系绑定可追溯证据', () => {
  const spec = attachGeneratedKnowledgeEvidence(baseSpec(), analysis, [{
    id: 'process_1', title: '处理请求流程', trigger: '收到请求', actors: ['处理人'], inputs: [],
    nodes: [{ id: 'step_receive', label: '接收请求', kind: 'action' }],
    edges: [], exceptions: [], outputs: [], handoffs: [], mermaid: 'flowchart TD',
  }]);

  assert.deepEqual(spec.rules?.[0].evidence, ['material:policy.md', 'anchor:适用条件章节']);
  assert.deepEqual(spec.metrics?.[0].evidence, ['material:calculation.md', 'anchor:计算口径章节']);
  assert.deepEqual(spec.operations[0].evidence, ['diagram:process_1', 'flow-node:step_receive']);
  assert.deepEqual(spec.objects[0].relations?.[0].evidence, ['material:records.md#请求记录']);
  assert.deepEqual(findKnowledgeEvidenceProblems(spec), []);
});

test('资料证据绑定不可变资料修订而不是仅记录可变文件名', () => {
  const spec = attachGeneratedKnowledgeEvidence(baseSpec(), analysis, [], {
    documents: [
      { id: 'doc_records_rev2', filename: 'records.md' },
      { id: 'doc_policy_rev3', filename: 'policy.md' },
      { id: 'doc_calculation_rev1', filename: 'calculation.md' },
    ],
  });

  assert.equal(spec.objects[0].sourceRefs?.[0]?.documentId, 'doc_records_rev2');
  assert.ok(spec.objects[0].relations?.[0].evidence?.includes('material-revision:doc_records_rev2'));
  assert.ok(spec.rules?.[0].evidence?.includes('material-revision:doc_policy_rev3'));
  assert.ok(spec.metrics?.[0].evidence?.includes('material-revision:doc_calculation_rev1'));
});

test('建模完成契约能识别没有证据的确认知识', () => {
  const problems = findKnowledgeEvidenceProblems(baseSpec());

  assert.ok(problems.some((item) => item.includes('operation:HandleRequest')));
  assert.ok(problems.some((item) => item.includes('rule:Eligibility')));
  assert.ok(problems.some((item) => item.includes('metric:Duration')));
  assert.ok(problems.some((item) => item.includes('relation:Request.handled_by')));
});

test('知识包文件展示操作、规则、指标和关系的简要证据引用', () => {
  const spec = attachGeneratedKnowledgeEvidence(baseSpec(), analysis, [{
    id: 'process_1', title: '处理请求流程', trigger: '', actors: [], inputs: [],
    nodes: [{ id: 'step_receive', label: '接收请求', kind: 'action' }],
    edges: [], exceptions: [], outputs: [], handoffs: [], mermaid: 'flowchart TD',
  }]);
  const files = renderPackage(spec);

  assert.match(files['operations/HandleRequest.md'], /# 知识证据[\s\S]*diagram:process_1/);
  assert.match(files['rules/Eligibility.md'], /# 知识证据[\s\S]*material:policy\.md/);
  assert.match(files['metrics/Duration.md'], /# 知识证据[\s\S]*material:calculation\.md/);
  assert.match(files['objects/Request.md'], /handled_by[\s\S]*material:records\.md#请求记录/);
});

test('人工编辑业务正文时保留生成链路记录的知识证据', () => {
  const spec = attachGeneratedKnowledgeEvidence(baseSpec(), analysis, [{
    id: 'process_1', title: '处理请求流程', trigger: '', actors: [], inputs: [],
    nodes: [{ id: 'step_receive', label: '接收请求', kind: 'action' }],
    edges: [], exceptions: [], outputs: [], handoffs: [],
  }]);
  const path = 'operations/HandleRequest.md';
  const content = renderPackage(spec)[path].replace(
    '1. 接收 object:Request',
    '1. 接收并登记 object:Request',
  );

  const updated = applyPackageDraftFiles(spec, [{ path, content }]);

  assert.equal(updated.operations[0].flow[0], '接收并登记 object:Request');
  assert.deepEqual(updated.operations[0].evidence, spec.operations[0].evidence);
});

test('operation 按 competencyQuestionId 在不同流程图输入中稳定绑定且允许一图多操作', () => {
  const spec = baseSpec();
  spec.operations = [{
    ...spec.operations[0],
    key: 'ApproveRequest',
    competencyQuestionId: 'process_approval',
  }, {
    ...spec.operations[0],
    key: 'SettleRequest',
    competencyQuestionId: 'process_settlement',
  }, {
    ...spec.operations[0],
    key: 'NotifyApproval',
    competencyQuestionId: 'process_approval',
  }];
  const diagramInputs = [
    [{
      id: 'settlement',
      nodes: [{ id: 'first-settle', kind: 'action' }],
    }, {
      id: 'approval',
      nodes: [{ id: 'first-approve', kind: 'decision' }],
    }],
    [{
      id: 'settlement',
      nodes: [{ id: 'second-settle', kind: 'object' }],
    }, {
      id: 'approval',
      nodes: [{ id: 'second-approve', kind: 'action' }],
    }],
  ];

  const [first, second] = diagramInputs.map((diagrams) => (
    attachGeneratedKnowledgeEvidence(spec, analysis, diagrams).operations
  ));

  assert.deepEqual(first.map((operation) => operation.evidence), [
    ['diagram:approval', 'flow-node:first-approve'],
    ['diagram:settlement', 'flow-node:first-settle'],
    ['diagram:approval', 'flow-node:first-approve'],
  ]);
  assert.deepEqual(second.map((operation) => operation.evidence), [
    ['diagram:approval', 'flow-node:second-approve'],
    ['diagram:settlement'],
    ['diagram:approval', 'flow-node:second-approve'],
  ]);
});
