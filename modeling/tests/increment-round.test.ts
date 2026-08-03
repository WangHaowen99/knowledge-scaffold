import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assertIncrementChangesAllowed, buildIncrementRequirement, IncrementRemovalNotAllowedError,
} from '../src/ai-construction/increment.js';
import type { PackageSpec } from '../src/types.js';

const spec = {
  rules: [{
    key: 'Eligibility', title: '准入规则', content: '资产达标才可推荐',
    conditions: [{ condition: '资产 ≥ 100 万', conclusion: '可推荐高阶产品' }],
  }],
} as unknown as PackageSpec;

test('提示词把边界写死：只动本轮资料涉及的内容', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '新规则.md', summary: '门槛调整通知' }],
    affected: [],
  });
  assert.match(requirement, /增量补充/);
  assert.match(requirement, /新规则\.md：门槛调整通知/);
  assert.match(requirement, /与本轮资料无关的内容一律不要动/);
  assert.match(requirement, /不得使用 remove/);
  assert.match(requirement, /优先 modify/);
  // 拿不出依据的改动不该提——否则增量会变成模型的自由发挥
  assert.match(requirement, /无法追溯到资料的改动不要提/);
});

test('带上相关已有内容，模型才可能提出「怎么改」而不是重写一遍', () => {
  const requirement = buildIncrementRequirement({
    materials: [{ filename: '通知.md' }],
    affected: [{ targetKind: 'rule', targetKey: 'Eligibility', action: 'modify' }],
    spec,
  });
  assert.match(requirement, /rule Eligibility 当前内容/);
  assert.match(requirement, /资产 ≥ 100 万/);
  assert.match(requirement, /不要新增一条语义重复的规则/);
});

test('没有新资料时也能构造需求，不产出空指令', () => {
  const requirement = buildIncrementRequirement({ materials: [], affected: [] });
  assert.match(requirement, /本轮没有新增资料/);
});

test('服务端挡下删除，而不是静默过滤', () => {
  const changes = [
    { targetKind: 'rule' as const, targetKey: 'A', action: 'modify' },
    { targetKind: 'rule' as const, targetKey: 'B', action: 'remove' },
  ];
  // 静默丢弃会让专家以为方案已经完整，实际少了一部分——所以抛错而不是过滤
  assert.throws(() => assertIncrementChangesAllowed(changes),
    (error: unknown) => error instanceof IncrementRemovalNotAllowedError);
  try {
    assertIncrementChangesAllowed(changes);
  } catch (error) {
    // 报错要说清替代做法，否则又是一条没有出口的红字
    assert.match((error as Error).message, /B/);
    assert.match((error as Error).message, /修改它们的适用条件或例外/);
  }
});

test('全是新增和修改时放行', () => {
  assert.doesNotThrow(() => assertIncrementChangesAllowed([
    { targetKind: 'rule', targetKey: 'A', action: 'add' },
    { targetKind: 'operation', targetKey: 'B', action: 'modify' },
  ]));
  assert.doesNotThrow(() => assertIncrementChangesAllowed([]));
});
