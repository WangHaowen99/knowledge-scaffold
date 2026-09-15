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
  assert.ok(['field_match', 'shared_key'].includes(first.candidates[0].matchMode ?? ''));
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

test('shared key creates a non-low-confidence candidate without object-name matching', () => {
  const spec: PackageSpec = {
    id: 'synthetic-shared-key',
    name: 'Synthetic shared key package',
    version: '0.1.0',
    status: 'draft',
    description: '',
    objects: [{
      key: 'alpha',
      title: 'Alpha',
      description: '',
      primaryKey: ['record_code'],
      fields: [{ key: 'record_code', type: 'string', required: true, sourceColumn: 'record_code' }],
      sourceRefs: [{ kind: 'table', documentId: 'doc_shared', table: 'alpha_rows' }],
    }, {
      key: 'beta',
      title: 'Beta',
      description: '',
      primaryKey: ['record_code'],
      fields: [{ key: 'record_code', type: 'string', required: true, sourceColumn: 'record_code' }],
      sourceRefs: [{ kind: 'table', documentId: 'doc_shared', table: 'beta_rows' }],
    }],
    operations: [],
    rules: [],
    metrics: [],
  };
  const tables: DataDictTable[] = [{
    name: 'alpha_rows', documentId: 'doc_shared', materialPurpose: 'schema', rowCount: 2,
    fields: [{ name: 'record_code', inferredType: 'string', distinctRatio: 1, nonEmptyRatio: 1 }],
    rows: [['R-1'], ['R-2']],
  }, {
    name: 'beta_rows', documentId: 'doc_shared', materialPurpose: 'schema', rowCount: 4,
    fields: [{ name: 'record_code', inferredType: 'string', distinctRatio: 0.5, nonEmptyRatio: 1 }],
    rows: [['R-1'], ['R-1'], ['R-2'], ['R-2']],
  }];
  const result = suggestObjectRelations(spec, tables);
  const candidate = result.candidates.find((item) => item.matchMode === 'shared_key');
  assert.ok(candidate);
  assert.notEqual(candidate.confidence, 'low');
  assert.equal(candidate.sourceField, 'record_code');
  assert.equal(candidate.targetField, 'record_code');
  assert.doesNotMatch(JSON.stringify(candidate), /R-1|R-2/);
});

test('rule and operation text creates semantic relation candidates', () => {
  const spec: PackageSpec = {
    id: 'synthetic-semantic-text',
    name: 'Synthetic semantic text package',
    version: '0.1.0',
    status: 'draft',
    description: '业务说明：两个资源通过关联键建立关系。',
    objects: [{
      key: 'resource_a', title: 'Resource A', description: '第一类资源。',
      fields: [{ key: 'link_code', type: 'string', required: false, sourceColumn: 'link_code' }],
    }, {
      key: 'resource_b', title: 'Resource B', description: '第二类资源。',
      fields: [{ key: 'link_code', type: 'string', required: false, sourceColumn: 'link_code' }],
    }],
    operations: [{
      key: 'link_resources', title: 'Link resources',
      description: '按关联键匹配 Resource A 和 Resource B。',
      inputs: [], outputs: [], flow: ['读取两个资源并连接'], relatedObjects: ['resource_a', 'resource_b'],
    }],
    rules: [{
      key: 'r_link', title: '关联规则', description: '两个资源通过关联键关联。',
      content: '只有关联键匹配时才建立关系。',
      appliesToObjects: ['resource_a', 'resource_b'],
    }],
    metrics: [],
  };
  const result = suggestObjectRelations(spec, []);
  const candidate = result.candidates.find((item) => item.matchMode === 'semantic_text');
  assert.ok(candidate);
  assert.equal(candidate.confidence, 'high');
  assert.ok(candidate.evidence?.some((item) => item.startsWith('rule:') || item.startsWith('operation:')));
  assert.doesNotMatch(JSON.stringify(candidate), /关联键匹配时/);
});
