import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildInterviewOutcome,
  buildInterviewOutcomeFromCandidates,
  buildProcessDiagramFiles,
  parseProcessInterviewCoreResponse,
  parseProcessInterviewDetailsResponse,
  parseProcessSelectionResponse,
  processInterviewQuestions,
} from '../src/ai-construction/process-interview.js';

test('流程访谈通过稳定问题 ID 整理核心流程边界', () => {
  const core = parseProcessInterviewCoreResponse({
    answers: [
      { questionId: 'process_title', selected: [], other: '月度生产计划编制' },
      { questionId: 'trigger', selected: [], other: '收到月度生产任务' },
      { questionId: 'actors', selected: [], other: '计划员、装置工程师；调度员' },
      { questionId: 'outputs', selected: [], other: '已确认的生产计划\n装置负荷安排' },
    ],
  });

  assert.deepEqual(core, {
    title: '月度生产计划编制',
    trigger: '收到月度生产任务',
    actors: ['计划员', '装置工程师', '调度员'],
    outputs: ['已确认的生产计划', '装置负荷安排'],
  });
});

test('流程访谈详情生成稳定节点、判断节点和顺序连线', () => {
  const core = {
    title: '月度生产计划编制',
    trigger: '收到月度生产任务',
    actors: ['计划员'],
    outputs: ['生产计划'],
  };
  const details = parseProcessInterviewDetailsResponse({
    answers: [
      { questionId: 'inputs', selected: [], other: '订单计划、仓储能力' },
      {
        questionId: 'steps', selected: [],
        other: '1. 汇总生产约束\n2. 判断：装置能力是否满足\n3. 形成排产方案',
      },
      { questionId: 'exceptions', selected: [], other: '装置检修；原料不足' },
      { questionId: 'handoffs', selected: [], other: '提交调度员确认' },
    ],
  });
  const outcome = buildInterviewOutcome(core, details);

  assert.equal(outcome.processes.length, 1);
  assert.deepEqual(outcome.processes[0].steps.map((step) => [step.id, step.label, step.kind]), [
    ['step_1', '汇总生产约束', 'action'],
    ['step_2', '装置能力是否满足', 'decision'],
    ['step_3', '形成排产方案', 'action'],
  ]);
  assert.deepEqual(outcome.processes[0].transitions, [
    { from: 'step_1', to: 'step_2' },
    { from: 'step_2', to: 'step_3' },
  ]);
});

test('流程访谈问题支持把已知答案带回选择式修改表单', () => {
  const questions = processInterviewQuestions('details', {
    inputs: ['客户资料'],
    stepLines: ['核对资料', '判断：资料是否完整'],
    exceptions: ['资料缺失'],
    handoffs: ['交给审批人'],
  });

  assert.deepEqual(questions.map((question) => [question.id, question.defaultValue]), [
    ['inputs', ['客户资料']],
    ['steps', ['核对资料\n判断：资料是否完整']],
    ['exceptions', ['资料缺失']],
    ['handoffs', ['交给审批人']],
  ]);
  assert.ok(questions.every((question) => question.allowOther === true));
});

test('已确认流程生成独立 Mermaid 文件和目录索引', () => {
  const outcome = buildInterviewOutcome({
    title: '授信审批', trigger: '客户提交申请', actors: ['客户经理'], outputs: ['审批结论'],
  }, {
    inputs: ['申请资料'],
    stepLines: ['受理申请', '判断：资料是否完整', '给出审批结论'],
    exceptions: ['资料不完整时退回'],
    handoffs: ['通知客户经理'],
  });
  const files = buildProcessDiagramFiles(outcome);

  assert.deepEqual(Object.keys(files).sort(), ['diagrams/index.md', 'diagrams/授信审批.mmd']);
  assert.match(files['diagrams/index.md'], /\[授信审批\]\(<授信审批\.mmd>\)/);
  assert.match(files['diagrams/授信审批.mmd'], /^flowchart TD/m);
  assert.match(files['diagrams/授信审批.mmd'], /资料是否完整/);
  assert.match(files['diagrams/授信审批.mmd'], /-->/);
});

test('完整候选流程通过一次多选直接形成多个独立流程和纵向 Mermaid', () => {
  const candidates = [
    {
      title: '申请处理', trigger: '收到申请', actors: ['经办人'], inputs: ['申请资料'],
      steps: ['受理申请', '判断：资料是否完整', '形成结论'], exceptions: ['资料缺失时退回'],
      outputs: ['处理结论'], handoffs: ['提交复核人'], rationale: '资料明确描述申请处理。',
    },
    {
      title: '结果复核', trigger: '收到待复核结果', actors: ['复核人'], inputs: ['处理结论'],
      steps: ['核对依据', '判断：结论是否合理', '发布结果'], exceptions: ['结论异常时退回'],
      outputs: ['已发布结果'], handoffs: ['通知经办人'], rationale: '资料明确描述复核流程。',
    },
  ];
  const selection = parseProcessSelectionResponse({ answers: [{
    questionId: 'selected_processes', selected: ['申请处理', '结果复核'],
  }] }, candidates);
  const outcome = buildInterviewOutcomeFromCandidates(selection.candidates);
  const files = buildProcessDiagramFiles(outcome);

  assert.equal(outcome.processes.length, 2);
  assert.deepEqual(outcome.processes.map((process) => process.id), ['process_1', 'process_2']);
  assert.deepEqual(Object.keys(files).sort(), [
    'diagrams/index.md', 'diagrams/申请处理.mmd', 'diagrams/结果复核.mmd',
  ]);
  assert.match(files['diagrams/申请处理.mmd'], /^flowchart TD/m);
  assert.match(files['diagrams/结果复核.mmd'], /^flowchart TD/m);
});

test('资料候选不完整时生成可编辑占位流程而不是阻断流程图', () => {
  const outcome = buildInterviewOutcomeFromCandidates([{
    title: '月度排产', trigger: '', actors: [], inputs: [], steps: [],
    exceptions: [], outputs: [], handoffs: [], rationale: '资料只提到了流程名称。',
  }]);

  assert.equal(outcome.processes[0].title, '月度排产');
  assert.equal(outcome.processes[0].trigger, '待确认触发条件');
  assert.deepEqual(outcome.processes[0].actors, ['待确认参与角色']);
  assert.deepEqual(outcome.processes[0].steps.map((step) => step.label), ['待确认业务步骤']);
  assert.deepEqual(outcome.processes[0].outputs, ['待确认输出']);
});

test('新流程候选每次只选择一个并由后续添加卡片继续补充', () => {
  const candidates = [
    {
      title: '申请处理', trigger: '收到申请', actors: ['经办人'], inputs: ['申请资料'],
      steps: ['受理申请', '形成结论'], exceptions: [], outputs: ['处理结论'], handoffs: [],
      rationale: '资料明确描述申请处理。',
    },
    {
      title: '结果复核', trigger: '收到结果', actors: ['复核人'], inputs: ['处理结论'],
      steps: ['核对结果', '发布结果'], exceptions: [], outputs: ['已发布结果'], handoffs: [],
      rationale: '资料明确描述结果复核。',
    },
  ];

  const [question] = processInterviewQuestions('core', {}, candidates);

  assert.equal(question.id, 'selected_processes');
  assert.equal(question.kind, 'single');
  assert.deepEqual(question.defaultValue, ['申请处理']);
  assert.match(question.q, /先添加哪个/);
});

test('其他内容作为补充说明而不直接当作流程标题', () => {
  const candidates = [{
    title: '申请处理', trigger: '收到申请', actors: ['经办人'], inputs: ['申请资料'],
    steps: ['受理申请', '形成结论'], exceptions: [], outputs: ['处理结论'], handoffs: [],
    rationale: '资料明确描述申请处理。',
  }];

  const customNote = '需要另外补充一条与月度排产有关的流程，其中需要综合考虑库存约束、装置负荷约束、检修窗口、原料可用性以及计划人员的人工调整意见。'.repeat(2);
  const selection = parseProcessSelectionResponse({ answers: [{
    questionId: 'selected_processes',
    selected: ['申请处理'],
    other: customNote,
  }] }, candidates);

  assert.deepEqual(selection.candidates, candidates);
  assert.equal(selection.customNote, customNote);
});

test('同名资料候选按稳定 ID 选择且旧标题只在唯一匹配时兼容', () => {
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

  const selected = parseProcessSelectionResponse({ answers: [{
    questionId: 'selected_processes', selected: ['candidate-review-final'],
  }] }, candidates);

  assert.equal(selected.candidates[0].candidateId, 'candidate-review-final');
  assert.equal(selected.candidates[0].trigger, '收到终审结果');
  assert.throws(() => parseProcessSelectionResponse({ answers: [{
    questionId: 'selected_processes', selected: ['结果复核'],
  }] }, candidates), /存在同名候选流程/);

  const legacy = parseProcessSelectionResponse({ answers: [{
    questionId: 'selected_processes', selected: ['申请处理'],
  }] }, [{ ...candidates[0], candidateId: undefined, title: '申请处理' }]);
  assert.equal(legacy.candidates[0].title, '申请处理');
});

test('缺少必答流程内容时在推进任务前拒绝', () => {
  assert.throws(() => parseProcessInterviewCoreResponse({ answers: [] }), /请完整填写核心流程/);
  assert.throws(() => parseProcessInterviewCoreResponse({ answers: [
    { questionId: 'process_title', other: '无角色流程' },
    { questionId: 'trigger', other: '开始' },
    { questionId: 'actors', other: '无' },
    { questionId: 'outputs', other: '处理结果' },
  ] }), /请填写参与角色/);
  assert.throws(() => parseProcessInterviewDetailsResponse({ answers: [] }), /请填写流程步骤/);
});

test('流程细节只强制要求步骤，输入、异常和交接可以留空', () => {
  const details = parseProcessInterviewDetailsResponse({ answers: [{
    questionId: 'steps', other: '接收任务\n形成结果',
  }] });

  assert.deepEqual(details, {
    inputs: [],
    stepLines: ['接收任务', '形成结果'],
    exceptions: [],
    handoffs: [],
  });
});
