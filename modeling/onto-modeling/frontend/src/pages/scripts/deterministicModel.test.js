import assert from 'node:assert/strict';
import test from 'node:test';
import {
  draftKey, draftOf, hasDraftErrors, isDirty, joinTerms, newContract, payloadOf, splitTerms,
  responseMode, termSource, testVectorsPatch, verificationText,
  assistantDraftCommand, businessExamples, executionNextStep, selectionAfterRefresh,
} from './deterministicModel.js';

test('保存排序和刷新后仍选中同一操作，删除后才回退位置', () => {
  const before = [{ key: 'fee' }, { key: 'count' }];
  assert.equal(selectionAfterRefresh(before, 1, [...before].reverse()), 0);
  assert.equal(selectionAfterRefresh(before, 1, [before[0]]), 0);
  assert.equal(selectionAfterRefresh(before, 1, []), 0);
});

test('业务摘要按字段说明展示结果，缺参和不匹配不冒充成功', () => {
  const item = { output_schema: '{"properties":{"value":{"title":"应付金额（分）"}}}', test_vectors: [
    { name: '正常', text: '计算费用', expected_output: { value: 120 } },
    { name: '缺参', expected_issues: [{ field: 'amount', message: '请提供金额' }] },
    { name: '解释', expected_matched: false },
  ] };
  assert.deepEqual(businessExamples(item).map((entry) => entry.expected), ['应付金额（分）：120', '请提供金额', '不执行此操作']);
  assert.deepEqual(businessExamples({ test_vectors_source: 'broken' }), []);
  assert.deepEqual(businessExamples({ test_vectors_source: '[null, 1, []]' }), []);
  assert.match(businessExamples({ test_vectors: [{ text: '合成请求', known_arguments: { amount: 2 } }] })[0].known, /amount：2/);
});

test('修改后不得把旧验证通过显示为当前可发布', () => {
  const state = { enabled: true, verification: { status: 'passed', current: true } };
  assert.match(executionNextStep({ ...state, dirty: true }), /尚未验证/);
  assert.match(executionNextStep({ ...state, newer: true }), /已有更新/);
  assert.match(executionNextStep({ ...state, verification: { status: 'passed', current: false } }), /失效/);
  assert.match(executionNextStep(state), /发布页面/);
});

test('助手修复命令携带所选业务与失败原因，不请求直接写受管契约', () => {
  const command = assistantDraftCommand({ operation: '汇总', handler: 'summary', key: 'summary' },
    [{ message: '输入未定义' }], { cases: [{ ok: false, name: '边界', reason: '结果不一致' }, { ok: true, name: '正常' }] });
  assert.equal(command.command, '/ontology-deterministic-generate');
  assert.equal(command.args.operation, '汇总');
  assert.deepEqual(command.args.failed_cases, [{ name: '边界', reason: '结果不一致' }]);
  assert.match(command.text, /不编写代码/);
});

test('确定性契约草稿保持后端结构并检测变化', () => {
  const view = { operations: [{
    key: 'summary', operation: 'summary', handler: 'summary_handler', data_access: 'none', enabled: true,
    aliases: ['汇总'], routing: { all_terms: ['本月'], any_terms: ['汇总', '统计'] },
    parser_source: 'print(1)\n', input_schema: '{"type":"object"}\n', output_schema: '{"type":"object"}\n',
    test_vectors: [{ name: '正常请求', text: '汇总本月', expected_output: {} }],
    verification: { status: 'passed', current: true, passed: 1, failed: 0 },
  }] };
  const draft = draftOf(view);
  assert.equal(isDirty(view, draft), false);
  assert.equal(payloadOf(draft)[0].routing.any_terms[1], '统计');
  assert.equal(JSON.parse(payloadOf(draft)[0].test_vectors)[0].name, '正常请求');
  assert.equal('verification' in payloadOf(draft)[0], false);
  assert.notEqual(draftKey([{ ...draft[0], data_access: 'workspace_provider' }]), draftKey(draft));
  assert.equal(isDirty(view, [{ ...draft[0], aliases: ['月度汇总'] }]), true);
  assert.equal(isDirty(view, [{ ...draft[0], verification: { status: 'failed', current: true } }]), false);
});

test('词列表逐字输入保留分隔符，提交时才拆分且不泄漏编辑字段', () => {
  const item = newContract();
  for (const field of ['aliases', 'any_terms', 'all_terms']) {
    const sourceKey = `${field}_source`;
    let draft = { ...item };
    for (const char of '合成甲，\n合成乙') {
      draft = { ...draft, [sourceKey]: termSource(draft, field) + char };
      assert.ok(termSource(draft, field).endsWith(char));
    }
    const payload = payloadOf([draft])[0];
    assert.deepEqual(field === 'aliases' ? payload.aliases : payload.routing[field], ['合成甲', '合成乙']);
    assert.equal(sourceKey in payload, false);
    assert.notEqual(draftKey([{ ...item, [sourceKey]: '\n' }]), draftKey([item]));
    const cleared = payloadOf([{ ...draft, [sourceKey]: '' }])[0];
    assert.deepEqual(field === 'aliases' ? cleared.aliases : cleared.routing[field], []);
  }
});

test('非法测试向量文本参与脏状态但不会进入提交载荷', () => {
  const view = { operations: [{
    key: 'first', operation: 'first', handler: 'first', test_vectors: [{ name: 'old' }],
  }, {
    key: 'second', operation: 'second', handler: 'second', test_vectors: [{ name: 'valid' }],
  }] };
  const draft = draftOf(view);
  const editing = [
    { ...draft[0], test_vectors_source: '[{', test_vectors_error: '测试向量必须是有效 JSON' },
    { ...draft[1], aliases: ['已修改'] },
  ];

  assert.equal(isDirty(view, editing), true);
  assert.equal(hasDraftErrors(editing), true);
  assert.equal(payloadOf(editing)[0].test_vectors, '[{');
  assert.equal('test_vectors_source' in payloadOf(editing)[0], false);
  assert.equal(hasDraftErrors(draft), false);
});

test('测试向量要求 JSON 数组根节点并保留错误文本', () => {
  assert.deepEqual(testVectorsPatch('[{"name":"synthetic"}]'), {
    test_vectors_source: '[{"name":"synthetic"}]', test_vectors_error: '',
  });
  assert.deepEqual(testVectorsPatch('{"name":"synthetic"}'), {
    test_vectors_source: '{"name":"synthetic"}', test_vectors_error: '测试向量根节点必须是 JSON 数组',
  });
  assert.equal(testVectorsPatch('[{').test_vectors_error, '测试向量必须是有效 JSON');
});

test('测试向量提交原始文本，避免大整数和重复键被浏览器改写', () => {
  const largeInteger = '[{"known_arguments":{"amount":9007199254740993}}]';
  const duplicateKey = '[{"expected_output":{"value":1,"value":2}}]';
  const largePatch = testVectorsPatch(largeInteger);
  const duplicatePatch = testVectorsPatch(duplicateKey);

  assert.equal(largePatch.test_vectors_source, largeInteger);
  assert.equal(largePatch.test_vectors_error, '');
  assert.equal(duplicatePatch.test_vectors_source, duplicateKey);
  assert.equal(duplicatePatch.test_vectors_error, '');
  assert.equal(payloadOf([{ ...newContract(), ...largePatch }])[0].test_vectors, largeInteger);
  assert.equal(payloadOf([{ ...newContract(), ...duplicatePatch }])[0].test_vectors, duplicateKey);
});

test('测试向量拒绝非有限数值并保留原始文本', () => {
  const positive = '[{"known_arguments":{"amount":1e309}}]';
  const negative = '[{"expected_output":[{"value":-1e309}]}]';
  assert.deepEqual(testVectorsPatch(positive), {
    test_vectors_source: positive,
    test_vectors_error: '测试向量 $[0].known_arguments.amount 包含超出有限范围的数值',
  });
  assert.equal(
    testVectorsPatch(negative).test_vectors_error,
    '测试向量 $[0].expected_output[0].value 包含超出有限范围的数值',
  );
  assert.equal(testVectorsPatch('[{"value":1.7976931348623157e308}]').test_vectors_error, '');
  assert.equal(testVectorsPatch('[{"value":1e-309}]').test_vectors_error, '');
});

test('请求返回时只有未发生新编辑才替换草稿', () => {
  assert.equal(responseMode(3, 3), 'replace');
  assert.equal(responseMode(3, 4), 'preserve');
});

test('新契约只声明 provider 中立的数据访问模式', () => {
  const item = newContract({ available_operations: ['calculate'], available_handlers: ['calculate'], operations: [] });
  assert.equal(item.operation, 'calculate');
  assert.equal(item.handler, 'calculate');
  assert.equal(item.data_access, 'none');
  assert.match(item.parser_source, /--request-json/);
  assert.equal(JSON.parse(item.input_schema).type, 'object');
  assert.equal('provider' in item, false);
});

test('别名和路由词支持逗号及换行录入', () => {
  assert.deepEqual(splitTerms('汇总, 统计\n月报，年报'), ['汇总', '统计', '月报', '年报']);
  assert.equal(joinTerms(['汇总', '统计']), '汇总\n统计');
});

test('验证状态明确区分通过、失败、失效和缺失', () => {
  assert.equal(verificationText({ status: 'passed', current: true }), '执行闭包验证通过');
  assert.equal(verificationText({ status: 'failed', current: true }), '执行闭包验证未通过');
  assert.equal(verificationText({ status: 'passed', current: false }), '执行依赖已变化，需要重新验证');
  assert.equal(verificationText({ status: 'missing', current: false }), '尚未验证执行闭包');
});
