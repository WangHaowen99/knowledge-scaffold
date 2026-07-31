import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bindOperationCompetencyQuestions,
  packageIntentQuestions,
} from '../src/workflow/spec-validation.js';

const operation = (overrides = {}) => ({
  key: 'query_customer', title: '查询客户', description: '查询客户信息',
  inputs: [], outputs: [], flow: [], relatedObjects: [],
  ...overrides,
});

test('问题 ID 有效时服务端覆盖模型改写的问题文本', () => {
  const result = bindOperationCompetencyQuestions(
    [{ id: 'cq-1', question: '如何查询企业客户的存款余额？' }],
    [operation({ competencyQuestionId: 'cq-1', competencyQuestion: '查一下客户余额' })],
  );

  assert.deepEqual(result.issues, []);
  assert.equal(result.operations[0].competencyQuestion, '如何查询企业客户的存款余额？');
});

test('问题 ID 不属于当前批次时拒绝绑定', () => {
  const result = bindOperationCompetencyQuestions(
    [{ id: 'cq-1', question: '如何查询企业客户的存款余额？' }],
    [operation({ competencyQuestionId: 'cq-other' })],
  );

  assert.equal(result.issues.length, 1);
  assert.match(result.issues[0], /不存在的领域问题 ID cq-other/);
});

test('覆盖问题只从当前知识包 operation 提取并按首次出现顺序去重', () => {
  const spec = {
    operations: [
      operation({ key: 'query_customer', competencyQuestion: '  如何查询企业客户？  ' }),
      operation({ key: 'query_customer_again', competencyQuestion: '如何查询企业客户？' }),
      operation({ key: 'query_balance', competencyQuestion: 'How to query balance?' }),
      operation({ key: 'query_balance_again', competencyQuestion: 'how to query balance?' }),
      operation({ key: 'without_question', competencyQuestion: '   ' }),
    ],
  };

  assert.deepEqual(packageIntentQuestions(spec as any), [
    '如何查询企业客户?',
    'How to query balance?',
  ]);
});
