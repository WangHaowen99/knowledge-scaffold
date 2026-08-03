import assert from 'node:assert/strict';
import test from 'node:test';

import {
  additionalImpact, describeImpact, directDependents, propagateImpact,
} from '../src/documents/knowledge-dependency.js';
import type { PackageSpec } from '../src/types.js';

/**
 * 一份最小但引用关系完整的知识包：
 *   规则 R ← 操作 OP → 指标 M
 *            OP ↔ 脚本 S（相互引用）
 *   对象 O ← OP
 * 另有一条与 OP 无关的操作 OTHER，用来验证「没被碰到的不受影响」。
 */
const spec = {
  objects: [
    { key: 'customer', relations: [{ relation: 'owns' }] },
    { key: 'unrelated_object', relations: [] },
  ],
  operations: [
    {
      key: 'OP', usesRules: ['R'], usesMetrics: ['M'],
      relatedObjects: ['customer'], script: { key: 'S' },
    },
    { key: 'OTHER', usesRules: ['R2'], usesMetrics: [], relatedObjects: ['unrelated_object'] },
  ],
  rules: [{ key: 'R' }, { key: 'R2' }],
  metrics: [{ key: 'M' }],
  scripts: [{ key: 'S', boundOperation: 'OP' }],
} as unknown as PackageSpec;

test('改规则会波及引用它的操作，以及该操作绑定的脚本', () => {
  const impact = propagateImpact(spec, [{ kind: 'rule', key: 'R' }]);
  assert.deepEqual(impact.map((ref) => `${ref.kind}:${ref.key}`).sort(),
    ['operation:OP', 'rule:R', 'script:S']);
});

test('没被引用到的内容不受影响——这一条决定了增量是不是真的增量', () => {
  const impact = propagateImpact(spec, [{ kind: 'rule', key: 'R' }]);
  const ids = impact.map((ref) => `${ref.kind}:${ref.key}`);
  assert.equal(ids.includes('operation:OTHER'), false);
  assert.equal(ids.includes('rule:R2'), false);
  assert.equal(ids.includes('object:unrelated_object'), false);
});

test('操作与脚本相互引用，传播不能绕死循环', () => {
  // 从脚本出发同样要收敛：S → OP → S（已访问，停）
  const fromScript = propagateImpact(spec, [{ kind: 'script', key: 'S' }]);
  assert.deepEqual(fromScript.map((ref) => `${ref.kind}:${ref.key}`).sort(),
    ['operation:OP', 'script:S']);
});

test('改对象会波及引用它的操作', () => {
  const impact = propagateImpact(spec, [{ kind: 'object', key: 'customer' }]);
  assert.ok(impact.some((ref) => ref.kind === 'operation' && ref.key === 'OP'));
});

test('改对象关系等同于改它所属的对象', () => {
  const dependents = directDependents(spec, { kind: 'relation', key: 'customer.owns' });
  assert.deepEqual(dependents, [{ kind: 'operation', key: 'OP' }]);
});

test('多个改动点合并传播，结果去重', () => {
  const impact = propagateImpact(spec, [
    { kind: 'rule', key: 'R' }, { kind: 'metric', key: 'M' },
  ]);
  const ids = impact.map((ref) => `${ref.kind}:${ref.key}`);
  assert.equal(new Set(ids).size, ids.length, '不能出现重复');
  assert.deepEqual(ids.sort(), ['metric:M', 'operation:OP', 'rule:R', 'script:S']);
});

test('additionalImpact 只给出改动点之外的部分', () => {
  const extra = additionalImpact(spec, [{ kind: 'rule', key: 'R' }]);
  assert.deepEqual(extra.map((ref) => `${ref.kind}:${ref.key}`).sort(),
    ['operation:OP', 'script:S'], '不应把改动点本身算成「额外受影响」');
});

test('空知识包和空改动点都不炸', () => {
  const empty = { objects: [], operations: [], rules: [], metrics: [], scripts: [] } as unknown as PackageSpec;
  assert.deepEqual(propagateImpact(empty, []), []);
  assert.deepEqual(propagateImpact(empty, [{ kind: 'rule', key: 'missing' }]),
    [{ kind: 'rule', key: 'missing' }], '引用不到的资源只返回它自己');
  assert.deepEqual(propagateImpact(spec, []), []);
});

test('影响面说明按类型归并，不列出几十条散装 key', () => {
  assert.equal(describeImpact([]), '没有其他内容受影响');
  assert.match(describeImpact([
    { kind: 'operation', key: 'OP' }, { kind: 'script', key: 'S' },
  ]), /业务操作：OP；脚本：S/);
  const many = Array.from({ length: 5 }, (_, index) => ({ kind: 'rule' as const, key: `R${index}` }));
  assert.match(describeImpact(many), /规则：R0、R1、R2 等 5 个/);
});
