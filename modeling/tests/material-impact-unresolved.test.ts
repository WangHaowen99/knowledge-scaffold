import assert from 'node:assert/strict';
import test from 'node:test';

import { analyzeMaterialImpact } from '../src/documents/material-impact.js';

// 「算出来没事」和「没算出要处理什么」不能共用同一个结果：替换资料的自动
// 界定完全押在基线证据锚点质量上，锚点缺失时匹配为空——过去这里静默返回
// 已界定的空影响，门禁直接放行，假阴性无声无息。现在这类情况一律显式
// 标记待界定（scopeResolved=false），交模型读过资料后显式提交起点。

const replacedChange = (overrides: Record<string, unknown> = {}) => ({
  kind: 'replaced',
  sourceKey: 'src-a',
  semanticChanged: true,
  previous: { id: 'doc-old', filename: 'synthetic_rules.md' },
  current: { id: 'doc-new', filename: 'synthetic_rules.md' },
  ...overrides,
}) as any;

const emptySpec = () => ({
  objects: [], operations: [], rules: [], metrics: [], scripts: [],
}) as any;

test('语义未变化时确定性无影响，不惊动任何人', () => {
  const analysis = analyzeMaterialImpact(replacedChange({ semanticChanged: false }), emptySpec());
  assert.equal(analysis.semanticChanged, false);
  assert.equal(analysis.scopeResolved, undefined);
  assert.deepEqual(analysis.affectedResources, []);
});

test('语义变了但自动匹配为空：降级为待界定，不再静默判空影响', () => {
  const analysis = analyzeMaterialImpact(replacedChange(), emptySpec());
  assert.equal(analysis.semanticChanged, true);
  assert.equal(analysis.scopeResolved, false, '空匹配必须进入门禁第一档，而不是自动通过');
  assert.deepEqual(analysis.affectedResources, []);
});

test('语义变了但包 spec 解析不出来：同样降级为待界定', () => {
  const analysis = analyzeMaterialImpact(replacedChange(), null);
  assert.equal(analysis.semanticChanged, true);
  assert.equal(analysis.scopeResolved, false);
});

test('证据锚点命中时照旧自动界定并沿引用传播', () => {
  const spec = {
    objects: [],
    operations: [],
    rules: [{ key: 'synthetic-rule', evidence: ['material:synthetic_rules.md#12'] }],
    metrics: [],
    scripts: [],
  } as any;
  const analysis = analyzeMaterialImpact(replacedChange(), spec);
  assert.equal(analysis.scopeResolved, true);
  assert.deepEqual(
    analysis.affectedResources.map((item) => `${item.kind}:${item.key}`),
    ['rule:synthetic-rule'],
  );
});

test('全新资料没有历史引用：保持待界定，交模型提交起点', () => {
  const analysis = analyzeMaterialImpact(
    replacedChange({ kind: 'added', previous: undefined }),
    emptySpec(),
  );
  assert.equal(analysis.semanticChanged, true);
  assert.equal(analysis.scopeResolved, false);
});
