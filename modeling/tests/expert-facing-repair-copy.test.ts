import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import PendingInteraction from '../frontend/src/PendingInteraction.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('历史无证据确认卡转成业务专家能理解的说明和选项', () => {
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'interaction-friendly-repair',
      type: 'question_batch',
      status: 'pending',
      title: '请明确“无证据知识”的业务决策',
      description: '1. [无证据知识] content 断言没有证明 Yield_1_Percent 相同。',
      payload: {
        purpose: 'controlled_repair_expert',
        finding: { category: '无证据知识' },
        questions: [{
          id: 'expert_decision', kind: 'single', required: true, allowOther: true,
          q: '请选择应采用的业务口径',
          options: [{
            label: '从 Product 表中查询并验证 Yield_1_Percent 实际值',
            value: '从 Product 表中查询并验证 Yield_1_Percent 实际值',
            description: '从 Product 表中查询并验证 Yield_1_Percent 实际值',
          }, {
            label: '若不等则删除断言并按 LogicTankID 路由',
            value: '若不等则删除断言并按 LogicTankID 路由',
            description: '若不等则删除断言并按 LogicTankID 路由',
          }],
        }],
      },
    },
    onSubmit: () => undefined,
  }));

  assert.match(html, /资料依据不足/);
  assert.match(html, /先核对实际数据/);
  assert.match(html, /按对应储罐分别取值/);
  assert.doesNotMatch(html, /content 断言|Yield_1_Percent|LogicTankID|Product 表/);
});

test('结构化专家选项使用通俗文案但保留执行决策', () => {
  let submitted;
  const html = renderToStaticMarkup(React.createElement(PendingInteraction, {
    interaction: {
      id: 'interaction-generated-copy', type: 'question_batch', status: 'pending',
      payload: {
        purpose: 'controlled_repair_expert',
        finding: {
          category: '无证据知识',
          userMessage: '现有资料不足以证明两条收率记录相同。',
          userSuggestion: '请确认应该先核对数据，还是分别取值。',
          expertOptions: [{
            label: '先核对实际收率',
            description: '确认两条记录是否真的相同。',
            decision: '查询 Yield_1_Percent 并补充证据。',
          }],
        },
        questions: [{ id: 'expert_decision', kind: 'single', required: true, options: [] }],
      },
    },
    onSubmit: (_id, response) => { submitted = response; },
  }));

  assert.match(html, /先核对实际收率/);
  assert.match(html, /确认两条记录是否真的相同/);
  assert.doesNotMatch(html, /Yield_1_Percent/);
  assert.equal(submitted, undefined);
});
