import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildModelingBriefQuestions,
  buildProcessInterviewQuestions,
} from '../src/ai-construction/interaction-options.js';
import type { AnalysisResult } from '../src/types.js';

const analysis: AnalysisResult = {
  domainOverview: '授信审批领域', candidateObjects: [], openQuestions: [],
  candidateGoals: [{
    title: '提升授信审批一致性', problem: '统一授信审批判断口径',
    targetUsers: ['客户经理', '审批人员'], scenarios: ['授信申请', '授信复审'],
    successCriteria: ['审批结论可追溯', '相同输入得到一致结论'], rationale: '授信制度和审批表均反复出现。',
  }],
  candidateProcesses: [{
    title: '授信审批流程', trigger: '客户提交授信申请', actors: ['客户经理', '审批人员'],
    inputs: ['授信申请', '客户资料'], steps: ['受理申请', '核验资料', '形成审批结论'],
    exceptions: ['资料缺失时退回补充'], outputs: ['审批结论'], handoffs: ['客户经理提交审批人员'],
    rationale: '制度文档明确描述了该流程。',
  }],
};

test('资料分析前先用一段自由文本收集建模目标', () => {
  const questions = buildModelingBriefQuestions(null);

  assert.equal(questions.length, 1);
  assert.equal(questions[0].id, 'goal_text');
  assert.equal(questions[0].kind, 'text');
  assert.match(String(questions[0].q), /建模目标/);
  assert.equal(questions[0].required, true);
});

test('建模目标问题优先给出少量资料假设并允许其他输入', () => {
  const questions = buildModelingBriefQuestions(analysis);
  assert.deepEqual(questions.map((item) => item.id), [
    'problem', 'target_users', 'scenarios', 'success_criteria',
  ]);
  assert.ok(questions.every((item) => ['single', 'multi'].includes(String(item.kind))));
  assert.ok(questions.every((item) => item.allowOther === true));
  assert.ok(questions.every((item) => Array.isArray(item.options) && item.options.length <= 4));
  assert.equal((questions[0].options as any[])[0].value, '统一授信审批判断口径');
});

test('流程访谈按阶段提供可选择的候选边界和完整步骤', () => {
  const core = buildProcessInterviewQuestions('core', analysis.candidateProcesses ?? []);
  assert.deepEqual(core.map((item) => item.id), ['selected_processes']);
  assert.ok(core.every((item) => item.allowOther === true));
  assert.equal(core[0].otherLabel, '补充其他流程');
  assert.match(String(core[0].otherPlaceholder), /补充说明/);
  assert.equal((core[0].options as any[])[0].value, '授信审批流程');
  assert.deepEqual(core[0].defaultValue, ['授信审批流程']);

  const details = buildProcessInterviewQuestions('details', analysis.candidateProcesses ?? [], '授信审批流程');
  assert.deepEqual(details.map((item) => item.id), ['inputs', 'steps', 'exceptions', 'handoffs']);
  assert.equal((details[1].options as any[])[0].value, '受理申请\n核验资料\n形成审批结论');
  assert.ok(details.every((item) => item.allowOther === true));
});

test('流程候选完整时一次最多提供三个单选流程且默认选择第一个', () => {
  const candidates = Array.from({ length: 4 }, (_, index) => ({
    title: `流程 ${index + 1}`, trigger: `触发 ${index + 1}`, actors: ['业务人员'],
    inputs: ['业务输入'], steps: ['受理', '判断条件', '形成结果'], exceptions: [],
    outputs: ['业务结果'], handoffs: [], rationale: `资料证据 ${index + 1}`,
  }));

  const questions = buildProcessInterviewQuestions('core', candidates);

  assert.equal(questions.length, 1);
  assert.equal(questions[0].kind, 'single');
  assert.equal((questions[0].options as any[]).length, 3);
  assert.deepEqual(questions[0].defaultValue, ['流程 1']);
});

test('同名流程候选以稳定 ID 作为选项值并保留标题作为展示文字', () => {
  const candidates = [
    {
      candidateId: 'candidate-review-first', title: '结果复核', trigger: '收到初审结果',
      actors: ['复核人'], inputs: ['初审结果'], steps: ['核对结果'], exceptions: [],
      outputs: ['复核结论'], handoffs: [], rationale: '初审复核流程',
    },
    {
      candidateId: 'candidate-review-final', title: '结果复核', trigger: '收到终审结果',
      actors: ['审批人'], inputs: ['终审结果'], steps: ['核对终审结果'], exceptions: [],
      outputs: ['终审结论'], handoffs: [], rationale: '终审复核流程',
    },
  ];

  const [question] = buildProcessInterviewQuestions('core', candidates);

  assert.deepEqual((question.options as any[]).map((item) => item.value), [
    'candidate-review-first', 'candidate-review-final',
  ]);
  assert.deepEqual((question.options as any[]).map((item) => item.label), ['结果复核', '结果复核']);
  assert.deepEqual(question.defaultValue, ['candidate-review-first']);
});

test('重新确认时把当前答案作为首选并默认选中', () => {
  const questions = buildModelingBriefQuestions(analysis, {
    problem: '保留当前问题', targetUsers: '现有用户', scenarios: '现有场景', successCriteria: '现有标准',
  });
  assert.equal((questions[0].options as any[])[0].value, '保留当前问题');
  assert.deepEqual(questions[0].defaultValue, ['保留当前问题']);
});
