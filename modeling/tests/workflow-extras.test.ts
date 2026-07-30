import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildFlowchart,
  buildScenarioDrafts,
} from '../src/authoring/index.js';

const spec = {
  id: 'marketing', name: '对公营销', description: '对公营销', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'data',
  objects: [
    { key: 'Customer', title: '企业客户', description: '企业客户', fields: [] },
    { key: 'Product', title: '金融产品', description: '金融产品', fields: [] },
  ],
  operations: [{
    key: 'Recommend', title: '产品推荐', description: '推荐匹配产品',
    inputs: [{ param: 'customer_id', cnName: '客户号', type: 'string', required: true }],
    outputs: [{ field: 'products', cnName: '产品清单', type: 'array' }],
    flow: ['读取客户', '匹配产品'], relatedObjects: ['Customer', 'Product'],
  }],
} as any;

test('流程图按操作拆分输入、对象、步骤和输出', () => {
  const output = buildFlowchart(spec, undefined);
  assert.equal(output.diagrams.length, 1);
  assert.equal(output.diagrams[0].title, '产品推荐');
  assert.deepEqual(output.diagrams[0].nodes.map((node) => [node.label, node.kind]), [
    ['输入', 'start'],
    ['企业客户', 'object'],
    ['金融产品', 'object'],
    ['读取客户', 'action'],
    ['匹配产品', 'action'],
    ['输出', 'end'],
  ]);
  assert.deepEqual(output.diagrams[0].edges.map((edge) => [edge.from, edge.to, edge.label]), [
    ['start:Recommend', 'step:Recommend:0', undefined],
    ['object:Recommend:Customer', 'step:Recommend:0', '数据'],
    ['object:Recommend:Product', 'step:Recommend:0', '数据'],
    ['step:Recommend:0', 'step:Recommend:1', undefined],
    ['step:Recommend:1', 'end:Recommend', undefined],
  ]);
});

test('不同操作保持独立，不按数组顺序制造跨操作连线', () => {
  const output = buildFlowchart({
    ...spec,
    operations: [
      ...spec.operations,
      {
        ...spec.operations[0],
        key: 'Review', title: '人工复核', flow: ['检查推荐结果'], relatedObjects: ['Customer'],
      },
    ],
  }, undefined);

  assert.deepEqual(output.diagrams.map((diagram) => diagram.id), ['Recommend', 'Review']);
  assert.equal(output.diagrams.some((diagram) => diagram.edges.some((edge) => (
    edge.from.includes('Recommend') && edge.to.includes('Review')
  ))), false);
});

test('场景草稿使用操作的输入、输出和步骤并覆盖标准、边界和异常变体', () => {
  const [scenario] = buildScenarioDrafts({
    ...spec,
    operations: [{
      ...spec.operations[0],
      usesRules: ['EligibleCustomer'],
      usesMetrics: ['FitScore'],
      notes: ['数据缺失时停止推荐并说明缺少字段'],
      script: { key: 'CalculateFit', required: true },
    }],
    rules: [{
      key: 'EligibleCustomer', title: '客户适用规则', content: '满足条件后才能推荐',
      exceptions: ['资料不完整时不得直接推荐'],
    }],
    metrics: [{
      key: 'FitScore', title: '适配评分', caliber: '按输入字段计算',
      boundaries: ['缺少必要输入时不计算'],
    }],
  });

  assert.deepEqual(
    { name: scenario.name, input: scenario.input, expected: scenario.expected, output: scenario.output },
    { name: '产品推荐', input: '客户号', expected: '产品清单', output: '读取客户 -> 匹配产品' },
  );
  assert.deepEqual(scenario.checks.map((check) => check.kind), ['standard', 'boundary', 'exception']);
  assert.match(scenario.checks[1].expected, /FitScore/);
  assert.match(scenario.checks[2].expected, /资料不完整|缺少字段/);
  assert.deepEqual(scenario.coverage, {
    rules: ['EligibleCustomer'], metrics: ['FitScore'], scripts: ['CalculateFit'],
  });
});
