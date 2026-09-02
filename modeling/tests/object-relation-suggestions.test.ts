import assert from 'node:assert/strict';
import test from 'node:test';

import type { DataDictTable, PackageSpec, SpecObject } from '../src/types.js';

const [{ suggestObjectRelations, collectPreviousObjectRelations }] = await Promise.all([
  import('../src/workflow/object-relation-suggestions.js'),
]);

test('object relation suggestions are deterministic and do not expose sample values', () => {
  const spec: PackageSpec = {
    id: 'synthetic-package',
    name: 'Synthetic package',
    version: '0.1.0',
    status: 'draft',
    description: 'Synthetic package for relation suggestions.',
    objects: [{
      key: 'customer',
      title: 'Customer',
      description: 'Synthetic customer.',
      primaryKey: ['customer_id'],
      fields: [
        { key: 'customer_id', type: 'string', required: true, sourceColumn: 'customer_id' },
        { key: 'customer_name', type: 'string', required: false, sourceColumn: 'customer_name' },
      ],
      sourceRefs: [{ kind: 'table' as const, documentId: 'doc_synthetic', table: 'customers' }],
    }, {
      key: 'invoice',
      title: 'Invoice',
      description: 'Synthetic invoice.',
      primaryKey: ['invoice_id'],
      fields: [
        { key: 'invoice_id', type: 'string', required: true, sourceColumn: 'invoice_id' },
        { key: 'customer_id', type: 'string', required: false, sourceColumn: 'customer_id' },
      ],
      sourceRefs: [{ kind: 'table' as const, documentId: 'doc_synthetic', table: 'invoices' }],
    }],
    operations: [],
    rules: [],
    metrics: [],
  };
  const tables: DataDictTable[] = [{
    name: 'customers',
    documentId: 'doc_synthetic',
    materialPurpose: 'schema' as const,
    rowCount: 2,
    fields: [
      { name: 'customer_id', inferredType: 'string' as const, sampleValues: ['C-001', 'C-002'], distinctRatio: 1, nonEmptyRatio: 1 },
      { name: 'customer_name', inferredType: 'string' as const, sampleValues: ['Alice', 'Bob'], distinctRatio: 1, nonEmptyRatio: 1 },
    ],
    rows: [['C-001', 'Alice'], ['C-002', 'Bob']],
  }, {
    name: 'invoices',
    documentId: 'doc_synthetic',
    materialPurpose: 'schema' as const,
    rowCount: 2,
    fields: [
      { name: 'invoice_id', inferredType: 'string' as const, sampleValues: ['I-001', 'I-002'], distinctRatio: 1, nonEmptyRatio: 1 },
      { name: 'customer_id', inferredType: 'string' as const, sampleValues: ['C-001', 'C-002'], distinctRatio: 1, nonEmptyRatio: 1 },
    ],
    rows: [['I-001', 'C-001'], ['I-002', 'C-002']],
  }];

  const first = suggestObjectRelations(spec, tables, { limit: 1 });
  assert.equal(first.returnedCount, 1);
  assert.equal(first.candidates[0].sourceObject, 'invoice');
  assert.equal(first.candidates[0].targetObject, 'customer');
  assert.equal(first.candidates[0].matchMode, 'field_match');
  assert.equal(first.candidates[0].evidence, undefined);
  assert.match(first.candidates[0].reason, /joinHitRatio=1\.000/);
  assert.doesNotMatch(JSON.stringify(first), /C-001|C-002|Alice|Bob|I-001|I-002/);

  const second = suggestObjectRelations(spec, tables, { limit: 1 });
  assert.equal(second.candidates[0].candidateFingerprint, first.candidates[0].candidateFingerprint);
  assert.ok(!first.candidates[0].candidateFingerprint.includes(first.schemaInputHash));

  const changedTables = structuredClone(tables);
  changedTables[1].rows = [['I-001', 'C-001'], ['I-002', 'C-404']];
  const changed = suggestObjectRelations(spec, changedTables, { limit: 1 });
  assert.notEqual(changed.schemaInputHash, first.schemaInputHash);
  assert.equal(changed.candidates[0].candidateFingerprint, first.candidates[0].candidateFingerprint);
  assert.match(changed.candidates[0].reason, /joinHitRatio=0\.500/);
  assert.doesNotMatch(JSON.stringify(changed), /C-001|C-002|C-404|Alice|Bob|I-001|I-002/);

  const rejected = suggestObjectRelations(spec, tables, {
    rejectedFingerprints: [first.candidates[0].candidateFingerprint],
  });
  assert.equal(rejected.totalCandidateCount, 0);
});

test('previous object relations are summarized without restoring them', () => {
  const previous: SpecObject[] = [{
    key: 'line_item',
    title: 'Line item',
    description: 'Synthetic line item.',
    fields: [{ key: 'line_id', type: 'string', required: true }],
    relations: [
      { relation: 'belongs_to', target: 'order', note: 'Previously confirmed.' },
      { relation: 'belongs_to', target: 'legacy_order', note: 'Stale target.' },
    ],
  }];
  const next: SpecObject[] = [{
    key: 'line_item',
    title: 'Line item',
    description: 'Synthetic line item.',
    fields: [{ key: 'line_id', type: 'string', required: true }],
  }, {
    key: 'order',
    title: 'Order',
    description: 'Synthetic order.',
    fields: [{ key: 'order_id', type: 'string', required: true }],
  }];

  assert.deepEqual(collectPreviousObjectRelations(previous, next).map((item) => item.status), [
    'previous',
    'discarded',
  ]);

  const suggested = suggestObjectRelations({
    id: 'synthetic-package',
    name: 'Synthetic package',
    version: '0.1.0',
    status: 'draft',
    description: 'Synthetic package for previous relations.',
    objects: next,
    operations: [],
    rules: [],
    metrics: [],
  }, [], {
    previousRelations: [{
      sourceObject: 'line_item',
      targetObject: 'order',
      relation: 'belongs_to',
      evidence: ['material:synthetic'],
    }],
  });
  assert.deepEqual(suggested.candidates[0].evidence, ['material:synthetic']);
});
