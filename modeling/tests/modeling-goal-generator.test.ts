import assert from 'node:assert/strict';
import test from 'node:test';

import { generateModelingBriefFromGoal } from '../src/ai-construction/modeling-goal-generator.js';
import { buildFauxModeler } from '../src/llm/provider.js';

test('自由建模目标由任务模型整理成后续资料分析可使用的目标说明', async () => {
  const brief = await generateModelingBriefFromGoal({
    goal: '建立业务申请处理知识模型，帮助业务经办人员和审核人员结合申请资料、适用规则及异常条件形成可执行处理方案。',
  }, { llm: buildFauxModeler() });

  assert.deepEqual(brief, {
    problem: '建立业务申请处理知识模型',
    targetUsers: '业务经办人员、审核人员',
    scenarios: '根据申请资料、业务规则和异常条件形成可执行处理方案',
    successCriteria: '生成可执行、可追溯且约束明确的业务处理方案',
  });
});
