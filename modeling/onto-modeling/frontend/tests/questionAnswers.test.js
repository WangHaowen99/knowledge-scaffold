import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildQuestionSubmission, isCustomAnswerOption } from '../src/lib/questionAnswers.js';

// 全部为合成控件数据，无外部项目、账号或环境信息。
const questions = [
  { question: '合成单选', options: [{ label: '方案一' }, { label: '方案二' }] },
  { question: '合成多选', multiSelect: true, options: [{ label: '渠道一' }, { label: '渠道二' }] },
  { question: '合成文字一', options: [] },
  { question: '合成文字二', options: [] },
];

test('混合题提交全部真实答案，文字逐题绑定', () => {
  const result = buildQuestionSubmission(questions, { 0: ['方案二'], 1: ['渠道一', '渠道二'] },
    { 2: '  人工合成回答一  ', 3: '人工合成回答二' });
  assert.deepEqual(result, {
    answers: { 合成单选: '方案二', 合成多选: '渠道一,渠道二', 合成文字一: '人工合成回答一', 合成文字二: '人工合成回答二' },
    answerDetails: {
      合成单选: { selected_options: ['方案二'], custom_text: '' },
      合成多选: { selected_options: ['渠道一', '渠道二'], custom_text: '' },
      合成文字一: { selected_options: [], custom_text: '人工合成回答一' },
      合成文字二: { selected_options: [], custom_text: '人工合成回答二' },
    },
    missing: [], invalid: false,
  });
});

test('清空一题不影响其他题，也不能提交缺失答案', () => {
  const result = buildQuestionSubmission(questions, { 0: ['方案一'], 1: ['渠道二'] },
    { 2: '  ', 3: '人工合成回答二' });
  assert.deepEqual(result.missing, [2]);
  assert.equal(result.answers.合成文字二, '人工合成回答二');
  assert.equal(Object.hasOwn(result.answers, '合成文字一'), false);
});

test('自定义文字替代单选，且不会遗漏多选中的补充文字', () => {
  const result = buildQuestionSubmission(questions.slice(0, 2),
    { 0: ['方案一'], 1: ['渠道一'] }, { 0: '自定义方案', 1: '自定义渠道' });
  assert.deepEqual(result.answers, { 合成单选: '自定义方案', 合成多选: '渠道一,自定义渠道' });
  assert.deepEqual(result.missing, []);
  assert.deepEqual(result.answerDetails, {
    合成单选: { selected_options: [], custom_text: '自定义方案' },
    合成多选: { selected_options: ['渠道一'], custom_text: '自定义渠道' },
  });
});

test('结构化答案区分带逗号的选项和内容相同的自由文字', () => {
  const list = [{ question: '合成多选', multiSelect: true, options: [{ label: '甲,乙' }] }];
  const selected = buildQuestionSubmission(list, { 0: ['甲,乙'] });
  const typed = buildQuestionSubmission(list, {}, { 0: '甲,乙' });
  assert.deepEqual(selected.answers, typed.answers);
  assert.deepEqual(selected.answerDetails.合成多选, { selected_options: ['甲,乙'], custom_text: '' });
  assert.deepEqual(typed.answerDetails.合成多选, { selected_options: [], custom_text: '甲,乙' });
});

test('选项标签中的逗号保持完整，未知选项不作为有效答案', () => {
  const list = [{ question: '合成选项', multiSelect: true, options: [{ label: '值一,值二' }] }];
  assert.equal(buildQuestionSubmission(list, { 0: ['值一,值二'] }).answers.合成选项, '值一,值二');
  assert.deepEqual(buildQuestionSubmission(list, { 0: ['不存在的选项'] }).missing, [0]);
});

test('不要求文字题选择占位项，文字优先于模型生成的选项', () => {
  const list = [{ question: '合成文字', options: [{ label: '占位示例' }] }];
  assert.equal(buildQuestionSubmission(list, {}, { 0: '人工合成回答' }).answers.合成文字, '人工合成回答');
  assert.equal(buildQuestionSubmission(list, { 0: ['占位示例'] }, { 0: '人工合成回答' }).answers.合成文字, '人工合成回答');
  assert.deepEqual(buildQuestionSubmission(list, { 0: ['占位示例'] }).missing, [0]);
});

test('Other 输入指引不能冒充答案，普通业务选项不被误判', () => {
  for (const label of ['Other', '（点 Other 输入自由文本）', '占位示例一']) {
    assert.equal(isCustomAnswerOption({ label }), true);
  }
  for (const label of ['方案一', '其他', '其它', '其他方案继续使用', 'Another option']) {
    assert.equal(isCustomAnswerOption({ label }), false);
  }
});

test('正常的其他选项与多选补充文字可以同时提交', () => {
  const list = [{ question: '合成多选', multiSelect: true, options: [{ label: '其他' }, { label: '方案一' }] }];
  assert.equal(buildQuestionSubmission(list, { 0: ['其他'] }, { 0: '人工合成说明' })
    .answers.合成多选, '其他,人工合成说明');
});

test('非字符串选项说明不阻止填写正式选项或文字', () => {
  const list = [{ question: '合成问题', options: [{ label: '方案一', description: 42 }] }];
  assert.equal(buildQuestionSubmission(list, { 0: ['方案一'] }).answers.合成问题, '方案一');
  assert.equal(buildQuestionSubmission(list, {}, { 0: '人工合成回答' }).answers.合成问题, '人工合成回答');
});

test('模型只在说明中标记占位时，也必须填写真实文字', () => {
  const option = { label: '示例一', description: '占位示例选项，仅示意，实际请通过 Other 输入。' };
  const list = [{ question: '合成文字', options: [option] }];
  assert.equal(isCustomAnswerOption(option), true);
  assert.deepEqual(buildQuestionSubmission(list, { 0: ['示例一'] }).missing, [0]);
  assert.equal(buildQuestionSubmission(list, {}, { 0: '人工合成回答' }).answers.合成文字, '人工合成回答');
  assert.equal(isCustomAnswerOption({ label: '方案一', description: '正式业务选择，不是占位选项。' }), false);
});

test('问题文本包含对象原型名称时仍能安全绑定自己的答案', () => {
  const result = buildQuestionSubmission([{ question: '__proto__' }], {}, { 0: '人工合成回答' });
  assert.deepEqual(result.answers, JSON.parse('{"__proto__":"人工合成回答"}'));
});

test('缺失题目、重复问题和非法选项明确标记为无效', () => {
  for (const list of [undefined, [], [{ question: '' }], [null],
    [{ question: '重复' }, { question: '重复' }],
    [{ question: '合成问题', options: 'invalid' }],
    [{ question: '合成问题', options: [null] }]]) {
    assert.equal(buildQuestionSubmission(list).invalid, true);
  }
});
