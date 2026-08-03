import assert from 'node:assert/strict';
import test from 'node:test';

import {
  currentResourceSummary, withCurrentContent,
} from '../src/ai-construction/change-plan-context.js';
import type { PackageSpec } from '../src/types.js';

const spec = {
  objects: [{ key: 'customer', title: '客户', description: '对公客户主体' }],
  operations: [{
    key: 'Recommend', title: '推荐产品', description: '按客户阶段推荐',
    flow: ['读取客户画像', '匹配产品'],
  }],
  rules: [{
    key: 'Eligibility', title: '准入规则', content: '资产达标才可推荐',
    conditions: [{ condition: '资产 ≥ 100 万', conclusion: '可推荐高阶产品' }],
    exceptions: ['战略客户不受此限'],
  }],
  metrics: [{ key: 'Yield', title: '收益率', description: '年化收益', formula: '收益 / 本金' }],
  scripts: [{ key: 'calc', title: '收益计算', description: '按公式计算', boundOperation: 'Recommend' }],
} as unknown as PackageSpec;

test('规则的当前内容包含判断条件和例外——专家要靠它判断改得对不对', () => {
  const summary = currentResourceSummary(spec, { targetKind: 'rule', targetKey: 'Eligibility' });
  assert.match(summary!, /准入规则/);
  assert.match(summary!, /资产 ≥ 100 万/);
  assert.match(summary!, /战略客户不受此限/);
});

test('各类资源都能取到当前内容', () => {
  assert.match(currentResourceSummary(spec, { targetKind: 'operation', targetKey: 'Recommend' })!, /读取客户画像/);
  assert.match(currentResourceSummary(spec, { targetKind: 'metric', targetKey: 'Yield' })!, /收益 \/ 本金/);
  assert.match(currentResourceSummary(spec, { targetKind: 'object', targetKey: 'customer' })!, /对公客户主体/);
  assert.match(currentResourceSummary(spec, { targetKind: 'script', targetKey: 'calc' })!, /绑定业务操作：Recommend/);
});

test('取不到就返回 null，不给空壳', () => {
  // 给个空字符串会让人以为「这条规则本来就是空的」，比不显示更糟
  assert.equal(currentResourceSummary(spec, { targetKind: 'rule', targetKey: 'missing' }), null);
  assert.equal(currentResourceSummary(null, { targetKind: 'rule', targetKey: 'Eligibility' }), null);
  assert.equal(currentResourceSummary(spec, { targetKind: 'rule', targetKey: '  ' }), null);
});

test('只给修改和删除类补当前内容，新增不补', () => {
  const changes = [
    { targetKind: 'rule' as const, targetKey: 'Eligibility', action: 'modify' },
    { targetKind: 'rule' as const, targetKey: 'NewRule', action: 'add' },
    { targetKind: 'operation' as const, targetKey: 'Recommend', action: 'remove' },
  ];
  const withCurrent = withCurrentContent(spec, changes);
  assert.ok(withCurrent[0].current, '修改类要有当前内容');
  assert.equal(withCurrent[1].current, undefined, '新增没有「当前内容」，硬塞只会多出一块空白');
  assert.ok(withCurrent[2].current, '删除类同样要先看清删的是什么');
});

test('当前内容过长会截断，不把整份规则灌进卡片', () => {
  const longSpec = {
    rules: [{ key: 'Long', title: 'T', content: 'x'.repeat(2_000) }],
  } as unknown as PackageSpec;
  const summary = currentResourceSummary(longSpec, { targetKind: 'rule', targetKey: 'Long' })!;
  assert.ok(summary.length <= 810, `摘要过长：${summary.length}`);
  assert.match(summary, /…$/);
});

test('模型没提供的字段缺失也不炸', () => {
  const sparse = { rules: [{ key: 'R' }] } as unknown as PackageSpec;
  assert.doesNotThrow(() => currentResourceSummary(sparse, { targetKind: 'rule', targetKey: 'R' }));
  assert.doesNotThrow(() => withCurrentContent(undefined, [
    { targetKind: 'rule', targetKey: 'R', action: 'modify' },
  ]));
});
