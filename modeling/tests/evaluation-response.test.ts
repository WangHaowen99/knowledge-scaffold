import { test } from 'node:test';
import assert from 'node:assert/strict';
import { evaluationFinalAnswer } from '../src/evaluation/engine.js';

test('评测响应保留文本答案', () => {
  assert.equal(evaluationFinalAnswer({ status: 'success', final_answer: '完成' }), '完成');
});

test('评测响应将待交互结果识别为缺少输入而不是对象字符串', () => {
  assert.throws(
    () => evaluationFinalAnswer({
      status: 'awaiting_input',
      final_answer: '',
      pending_user_interaction: { prompt: '请选择处理模式' },
    }),
    /评测用例缺少运行所需输入：请选择处理模式/,
  );
});

test('评测响应拒绝非文本 final_answer', () => {
  assert.throws(
    () => evaluationFinalAnswer({ status: 'success', final_answer: { answer: '完成' } }),
    /评测服务返回了非文本答案/,
  );
});
