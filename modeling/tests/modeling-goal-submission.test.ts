import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ModelingGoalInputError,
  resolveModelingBriefSubmission,
} from '../src/ai-construction/modeling-goal-submission.js';

test('自由目标输入缺失时返回可映射为 400 的输入错误', async () => {
  await assert.rejects(
    resolveModelingBriefSubmission({
      answers: [{ questionId: 'goal_text', selected: [], other: '  ', skipped: false }],
    }, async () => {
      throw new Error('不应调用模型');
    }),
    (error: unknown) => error instanceof ModelingGoalInputError
      && error.code === 'INVALID_GOAL_INPUT'
      && /请填写建模目标/.test(error.message),
  );
});

test('目标生成模型或系统异常原样传播为服务端错误', async () => {
  const upstreamError = new Error('任务模型暂时不可用');
  await assert.rejects(
    resolveModelingBriefSubmission({
      answers: [{
        questionId: 'goal_text', selected: [], other: '建立授信审批知识模型', skipped: false,
      }],
    }, async () => {
      throw upstreamError;
    }),
    (error: unknown) => error === upstreamError,
  );
});
