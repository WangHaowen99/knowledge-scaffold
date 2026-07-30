import assert from 'node:assert/strict';
import test from 'node:test';

import { parseModelingBriefResponse, summarizeModelingBrief } from '../src/ai-construction/modeling-brief.js';

test('建模目标回答按稳定问题 ID 整理成正式说明', () => {
  const brief = parseModelingBriefResponse({
    answers: [
      { questionId: 'problem', selected: [], other: '沉淀授信审批知识', skipped: false },
      { questionId: 'target_users', selected: ['客户经理', '审批人员'], skipped: false },
      { questionId: 'scenarios', selected: [], other: '授信申请和异常复核', skipped: false },
      { questionId: 'success_criteria', selected: [], other: '稳定回答流程与规则问题', skipped: false },
    ],
  });

  assert.deepEqual(brief, {
    problem: '沉淀授信审批知识',
    targetUsers: '客户经理；审批人员',
    scenarios: '授信申请和异常复核',
    successCriteria: '稳定回答流程与规则问题',
  });
  assert.match(summarizeModelingBrief(brief), /核心问题：沉淀授信审批知识/);
});

test('建模目标缺少必答项时拒绝推进任务', () => {
  assert.throws(() => parseModelingBriefResponse({
    answers: [
      { questionId: 'problem', selected: [], other: '只有问题描述', skipped: false },
    ],
  }), /请填写目标用户/);
});
