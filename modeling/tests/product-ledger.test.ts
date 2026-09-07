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
