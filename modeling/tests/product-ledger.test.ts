import assert from 'node:assert/strict';
import test from 'node:test';
import { validateProductLedger } from '../src/agent/product-ledger.js';

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
