import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_PRODUCT_LEDGER_ENTRIES, validateProductLedger } from '../src/agent/product-ledger.js';

test('product ledger closes raw, canonical and merged counts deterministically', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v1',
    sourceRevision: 'revision-synthetic',
    scope: 'product_catalog',
    rawCount: 3,
    canonicalCount: 2,
    mergedGroupCount: 1,
    entries: [
      {
        canonicalItemId: 'product-a',
        ruleId: 'rule-a',
        sourceRecordIds: ['source-a'],
        sourceLocator: 'manual#item-a',
        evidenceStatus: 'complete',
        processingStatus: 'ready',
      },
      {
        canonicalItemId: 'product-b',
        ruleId: 'rule-b',
        sourceRecordIds: ['source-b', 'source-c'],
        sourceLocator: 'manual#item-b',
        mergeGroupId: 'merge-1',
        evidenceStatus: 'insufficient',
        insufficiencyReason: '触发条件需要专家补充',
        processingStatus: 'blocked',
      },
    ],
    required: true,
  });

  assert.equal(result.pass, true);
  assert.deepEqual(result.counts, {
    raw: 3,
    canonical: 2,
    mergedGroups: 1,
    complete: 1,
    insufficient: 1,
  });
});

test('product ledger rejects duplicate IDs and contradictory counts', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v1',
    sourceRevision: 'revision-synthetic',
    scope: 'product_catalog',
    rawCount: 4,
    canonicalCount: 2,
    mergedGroupCount: 1,
    entries: [
      {
        canonicalItemId: 'product-a',
        ruleId: 'rule-a',
        sourceRecordIds: ['source-a'],
        sourceLocator: 'manual#item-a',
        evidenceStatus: 'complete',
        processingStatus: 'ready',
      },
      {
        canonicalItemId: 'product-a',
        ruleId: 'rule-a',
        sourceRecordIds: ['source-b'],
        sourceLocator: 'manual#item-b',
        evidenceStatus: 'complete',
        processingStatus: 'ready',
      },
    ],
  });

  assert.equal(result.pass, false);
  assert.ok(result.errors.some((message) => message.includes('canonicalItemId 重复')));
  assert.ok(result.errors.some((message) => message.includes('rawCount - canonicalCount')));
});

test('product-ledger-v2 允许多个条目共享同一条规则引用', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v2', sourceRevision: 'revision-v2', scope: 'product_catalog',
    rawCount: 2, canonicalCount: 2, mergedGroupCount: 0,
    entries: [
      { canonicalItemId: 'product-a', name: '合成产品 A', ruleRefs: ['rule-shared'], sourceRecordIds: ['source-a'], sourceLocator: 'manual#a', evidenceStatus: 'complete', processingStatus: 'ready' },
      { canonicalItemId: 'product-b', name: '合成产品 B', ruleRefs: ['rule-shared'], sourceRecordIds: ['source-b'], sourceLocator: 'manual#b', evidenceStatus: 'complete', processingStatus: 'ready' },
    ],
  });
  assert.equal(result.pass, true);
});

test('台账条目支持业务字段和结构化缺口', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v2', sourceRevision: 'revision-business', scope: 'product_catalog',
    rawCount: 1, canonicalCount: 1, mergedGroupCount: 0,
    entries: [{
      canonicalItemId: 'product-business', ruleRefs: ['rule-business'], sourceRecordIds: ['source-business'],
      sourceLocator: 'manual#business', name: '合成产品', applicableSubjects: ['合成主体'],
      triggerScenarios: ['合成场景'], businessNeeds: ['合成需求'], structuredGaps: [],
      evidenceStatus: 'complete', processingStatus: 'ready',
    }],
  });
  assert.equal(result.pass, true);
});

test('空台账不得通过：所有闭合等式在 0 处成立不代表盘点过', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v2', sourceRevision: 'revision-empty', scope: 'product_catalog',
    rawCount: 0, canonicalCount: 0, mergedGroupCount: 0, entries: [],
  });
  assert.equal(result.pass, false);
  assert.equal(result.blocking, true);
  assert.ok(result.errors.some((message) => message.includes('entries 不能为空')));
});

test('required 为 false 也不能让空台账通过', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v1', sourceRevision: 'revision-empty', scope: 'product_catalog',
    rawCount: 0, canonicalCount: 0, mergedGroupCount: 0, entries: [], required: false,
  });
  assert.equal(result.pass, false);
});

test('非法计数直接报错，不静默折算成零条目台账', () => {
  for (const rawCount of [-1, Number.NaN, '3', null, undefined, 1.5]) {
    const result = validateProductLedger({
      schemaVersion: 'product-ledger-v1', sourceRevision: 'revision-count', scope: 'product_catalog',
      rawCount, canonicalCount: 1, mergedGroupCount: 0,
      entries: [{
        canonicalItemId: 'product-a', ruleRefs: ['rule-a'], sourceRecordIds: ['source-a'],
        sourceLocator: 'manual#a', evidenceStatus: 'complete', processingStatus: 'ready',
      }],
    });
    assert.equal(result.pass, false, `rawCount=${String(rawCount)} 不应通过`);
    assert.ok(
      result.errors.some((message) => message.includes('rawCount 必须是非负整数')),
      `rawCount=${String(rawCount)} 应报非负整数错误，实际：${result.errors.join('；')}`,
    );
  }
});

test('entries 不是数组时如实报错', () => {
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v1', sourceRevision: 'revision-shape', scope: 'product_catalog',
    rawCount: 1, canonicalCount: 1, mergedGroupCount: 0, entries: { canonicalItemId: 'product-a' },
  });
  assert.equal(result.pass, false);
  assert.ok(result.errors.some((message) => message.includes('entries 必须是数组')));
});

test('条目数超过上限时报错，不靠继续追加掩盖提交方式错误', () => {
  const count = MAX_PRODUCT_LEDGER_ENTRIES + 1;
  const entries = Array.from({ length: count }, (_, index) => ({
    canonicalItemId: `product-${index}`, ruleRefs: ['rule-bulk'], sourceRecordIds: [`source-${index}`],
    sourceLocator: `manual#${index}`, evidenceStatus: 'complete', processingStatus: 'ready',
  }));
  const result = validateProductLedger({
    schemaVersion: 'product-ledger-v1', sourceRevision: 'revision-bulk', scope: 'product_catalog',
    rawCount: count, canonicalCount: count, mergedGroupCount: 0, entries,
  });
  assert.equal(result.pass, false);
  assert.ok(result.errors.some((message) => message.includes(`超过上限 ${MAX_PRODUCT_LEDGER_ENTRIES}`)));
});

test('blocking 区分「结构自洽」和「能不能往下走」', () => {
  const entry = (overrides: Record<string, unknown>) => ({
    canonicalItemId: 'product-a', ruleRefs: ['rule-a'], sourceRecordIds: ['source-a'],
    sourceLocator: 'manual#a', name: '合成产品 A',
    evidenceStatus: 'complete', processingStatus: 'ready', ...overrides,
  });
  const base = {
    schemaVersion: 'product-ledger-v2' as const, sourceRevision: 'revision-blocking', scope: 'product_catalog',
    rawCount: 1, canonicalCount: 1, mergedGroupCount: 0,
  };
  const clean = validateProductLedger({ ...base, entries: [entry({})] });
  assert.equal(clean.pass, true);
  assert.equal(clean.blocking, false);

  const insufficient = validateProductLedger({
    ...base,
    entries: [entry({ evidenceStatus: 'insufficient', insufficiencyReason: '原文未给出准入条件' })],
  });
  assert.equal(insufficient.pass, true, `计数自洽的不足条目仍属结构合格：${insufficient.errors.join('；')}`);
  assert.equal(insufficient.blocking, true, '存在资料不足条目时不得据此宣布覆盖完整');

  const blocked = validateProductLedger({ ...base, entries: [entry({ processingStatus: 'blocked' })] });
  assert.equal(blocked.pass, true);
  assert.equal(blocked.blocking, true);
});

