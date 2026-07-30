import assert from 'node:assert/strict';
import test from 'node:test';

import {
  generateProcessDraft,
  normalizeGeneratedProcessDraft,
} from '../src/ai-construction/process-draft-generator.js';
import { buildFauxModeler } from '../src/llm/provider.js';

test('自由流程说明由任务模型整理成可审阅的完整流程草稿', async () => {
  const draft = await generateProcessDraft({
    description: '收到业务申请后，经办人员结合申请资料、适用规则和异常条件形成处理方案，交由审核人员确认。',
    existingProcessTitles: ['申请资料完整性检查'],
  }, { llm: buildFauxModeler() });

  assert.deepEqual(draft, {
    title: '业务申请处理流程',
    trigger: '收到业务申请',
    actors: ['业务经办人员', '审核人员'],
    inputs: ['业务申请资料', '适用规则和处理约束'],
    steps: ['校验申请资料', '按规则生成候选处理方案', '审核并确认处理结果'],
    exceptions: ['关键约束缺失时标记待确认'],
    outputs: ['已确认的业务处理结果'],
    handoffs: ['业务经办人员提交审核人员确认'],
    rationale: '根据用户补充说明由模型整理，待用户确认。',
  });
});

test('模型缺少业务完整性字段时保留可编辑占位而不阻断流程图草稿', () => {
  assert.deepEqual(normalizeGeneratedProcessDraft({ title: '补充流程' }), {
    title: '补充流程',
    trigger: '待确认触发条件',
    actors: ['待确认参与角色'],
    inputs: [],
    steps: ['待确认业务步骤'],
    exceptions: [],
    outputs: ['待确认输出'],
    handoffs: [],
    rationale: '根据用户补充说明由模型整理，待用户确认。',
  });
});

test('资料分析候选也经过目标约束模型加工且保持候选业务身份', async () => {
  const draft = await generateProcessDraft({
    description: '根据资料候选形成可编辑流程图',
    candidateProcess: {
      title: '授信申请处理', trigger: '收到授信申请', actors: ['客户经理'],
      inputs: ['申请材料'], steps: ['受理申请', '形成结论'], exceptions: [],
      outputs: ['授信结论'], handoffs: ['提交审批人'], rationale: '材料中明确描述。',
    },
    modelingBrief: {
      problem: '建立授信审批知识', targetUsers: '客户经理',
      scenarios: '授信申请审批', successCriteria: '形成可执行审批结论',
    },
  }, { llm: buildFauxModeler() });

  assert.equal(draft.title, '授信申请处理');
  assert.equal(draft.trigger, '收到授信申请');
  assert.deepEqual(draft.steps, ['受理申请', '形成结论']);
});
