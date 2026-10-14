// 资料分析验收对阶段展示的影响（lib/researchGate.js）。
// 跑法：cd modeling/onto-modeling/frontend && npm test
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isResearchBlocking, orderResearchActions } from '../src/lib/researchGate.js';

const ACTIONS = [
  { label: '联网资料分析', command: '/ontology-research ' },
  { label: '仅本地资料', command: '/ontology-research 仅基于本地文档 ' },
  { label: '追加分析', command: '/ontology-research 追加' },
  { label: '核验报告', verify: true, command: '核验' },
];
const labels = (actions) => actions.map((action) => action.label);

test('挡不挡只认后端给的 blocking，不按状态自行推断', () => {
  assert.equal(isResearchBlocking({ status: 'pending', blocking: true }), true);
  // 旧项目或回执失效，但对象已提取：后端给 blocking=false，前端不能再拦
  assert.equal(isResearchBlocking({ status: 'pending', blocking: false }), false);
  assert.equal(isResearchBlocking({ status: 'failed', blocking: false }), false);
  assert.equal(isResearchBlocking({ status: 'passed', blocking: false }), false);
});

test('取不到验收信息时不拦，沿用原有的目录探针', () => {
  assert.equal(isResearchBlocking(null), false);
  assert.equal(isResearchBlocking(undefined), false);
  // 后端未返回 blocking 字段时同样不拦
  assert.equal(isResearchBlocking({ status: 'pending' }), false);
});

test('报告挡着后续阶段时，核验入口排到最前，其余顺序不变', () => {
  const ordered = orderResearchActions(ACTIONS, { status: 'failed', blocking: true });
  assert.deepEqual(labels(ordered), ['核验报告', '联网资料分析', '仅本地资料', '追加分析']);
  // 聊天区最多展示 4 个建议且第一个是主推：核验入口不能被挤掉
  assert.equal(ordered[0].verify, true);
});

test('不挡的时候保持原有顺序，也不改动传入的数组', () => {
  const before = labels(ACTIONS);
  assert.deepEqual(labels(orderResearchActions(ACTIONS, { status: 'passed', blocking: false })), before);
  assert.deepEqual(labels(orderResearchActions(ACTIONS, null)), before);
  orderResearchActions(ACTIONS, { status: 'pending', blocking: true });
  assert.deepEqual(labels(ACTIONS), before);
});
