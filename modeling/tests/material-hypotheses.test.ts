import assert from 'node:assert/strict';
import test from 'node:test';

import { ensureMaterialHypotheses } from '../src/ai-construction/material-hypotheses.js';

test('资料分析缺少候选时生成少量待确认的业务目标和流程假设', () => {
  const analysis = ensureMaterialHypotheses({
    domainOverview: '围绕企业授信审批开展风险核验和决策。',
    candidateObjects: [],
    openQuestions: [],
    candidateArtifacts: [{
      kind: 'operation', name: '企业授信审批', rationale: '文档给出了审批步骤',
      sources: ['授信手册.docx'], readiness: 'extractable',
    }],
  });

  assert.equal(analysis.candidateGoals?.length, 1);
  assert.equal(analysis.candidateProcesses?.length, 1);
  assert.match(analysis.candidateGoals?.[0].title ?? '', /授信审批/);
  assert.match(analysis.candidateProcesses?.[0].rationale ?? '', /待确认假设/);
});

test('资料分析只保留最多三个模型提出的候选', () => {
  const analysis = ensureMaterialHypotheses({
    domainOverview: '测试领域', candidateObjects: [], openQuestions: [],
    candidateGoals: Array.from({ length: 5 }, (_, index) => ({
      title: `目标 ${index + 1}`, problem: `问题 ${index + 1}`, targetUsers: ['业务人员'],
      scenarios: ['业务处理'], successCriteria: ['形成结果'], rationale: '材料依据',
    })),
    candidateProcesses: Array.from({ length: 5 }, (_, index) => ({
      title: `流程 ${index + 1}`, trigger: '收到请求', actors: ['业务人员'], inputs: [],
      steps: ['处理请求'], exceptions: [], outputs: ['处理结果'], handoffs: [], rationale: '材料依据',
    })),
  });

  assert.equal(analysis.candidateGoals?.length, 3);
  assert.equal(analysis.candidateProcesses?.length, 3);
});

test('同名资料流程候选获得不同稳定 ID，标题变化不改变已有身份', () => {
  const analysis = ensureMaterialHypotheses({
    domainOverview: '结果复核领域', candidateObjects: [], openQuestions: [],
    candidateProcesses: [
      {
        title: '结果复核', trigger: '收到初审结果', actors: ['复核人'], inputs: [],
        steps: ['核对初审依据'], exceptions: [], outputs: ['复核结论'], handoffs: [], rationale: '初审资料',
      },
      {
        title: '结果复核', trigger: '收到终审结果', actors: ['审批人'], inputs: [],
        steps: ['核对终审依据'], exceptions: [], outputs: ['终审结论'], handoffs: [], rationale: '终审资料',
      },
    ],
  });

  const [first, second] = analysis.candidateProcesses ?? [];
  assert.ok(first.candidateId);
  assert.ok(second.candidateId);
  assert.notEqual(first.candidateId, second.candidateId);

  const renamed = ensureMaterialHypotheses({
    ...analysis,
    candidateProcesses: [{ ...first, title: '初审结果复核' }, second],
  });
  assert.equal(renamed.candidateProcesses?.[0].candidateId, first.candidateId);
});

test('候选流程保留重复步骤和原始顺序', () => {
  const analysis = ensureMaterialHypotheses({
    domainOverview: '循环复核流程', candidateObjects: [], openQuestions: [],
    candidateProcesses: [{
      title: '循环复核', trigger: '提交资料', actors: ['审核人'], inputs: ['资料'],
      steps: ['核验资料', '补充资料', '核验资料'], exceptions: [], outputs: ['结论'], handoffs: [],
      rationale: '资料明确存在补充后复核。',
    }],
  });
  assert.deepEqual(analysis.candidateProcesses?.[0].steps, ['核验资料', '补充资料', '核验资料']);
});

test('资料分析结果携带已确认目标并围绕目标生成流程假设', () => {
  const brief = {
    problem: '根据原油性质和装置能力生成减一线分流方案',
    targetUsers: '生产计划人员',
    scenarios: '编制日度生产方案',
    successCriteria: '方案可执行、可追溯',
  };

  const analysis = ensureMaterialHypotheses({
    domainOverview: '资料介绍原油、装置和逻辑罐数据。',
    candidateObjects: [], openQuestions: [],
  }, brief);

  assert.deepEqual(analysis.modelingBrief, brief);
  assert.equal(analysis.candidateGoals?.length, 1);
  assert.equal(analysis.candidateGoals?.[0].problem, brief.problem);
  assert.match(analysis.candidateProcesses?.[0].title ?? '', /日度生产方案/);
  assert.match(analysis.candidateProcesses?.[0].rationale ?? '', /已确认建模目标/);
});
