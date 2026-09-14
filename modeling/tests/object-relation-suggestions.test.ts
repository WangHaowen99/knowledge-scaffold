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

test('共享键列的星型 schema 能产出关系候选，且方向由唯一性决定', () => {
  /* 中文业务对象名 + 星型 schema 会让所有基于命名猜测的生成器同时落空：
     词干取自英文 key（application_no → "application"）而对象叫《业务受理》；
     改用中文名也不行，判据是「对象名以词干结尾」，而中文是「计量点 + 方案」，
     关键词在前。再叠一层：共享键既是本表主键又是指向主表的外键，字段匹配那条
     遇到本表主键直接跳过。实测 9 张表产出 0 条候选（见 #47）。 */
  const objectOf = (key: string, primaryKey: string, columns: string[]): SpecObject => ({
    key,
    title: key,
    description: `${key} 合成对象`,
    primaryKey: [primaryKey],
    fields: columns.map((column) => ({ key: column, type: 'string', required: false, sourceColumn: column })),
    sourceRefs: [{ kind: 'table' as const, documentId: 'doc_star', table: key }],
  });
  const tableOf = (name: string, columns: string[], rowCount: number, distinct: Record<string, number>): DataDictTable => ({
    name,
    documentId: 'doc_star',
    rowCount,
    fields: columns.map((column) => ({
      name: column, sourceColumn: column, nonEmptyRatio: 1, distinctRatio: distinct[column] ?? 0.1,
    })),
    rows: [],
  } as unknown as DataDictTable);

  const layout: [string, string, string[], number, Record<string, number>][] = [
    ['业务受理', 'application_no', ['application_no', 'consumer_name'], 200, { application_no: 1 }],
    ['接入方案', 'application_no', ['application_no', 'substation'], 200, { application_no: 1 }],
    ['互感器方案', 'application_no', ['application_no', 'current_ratio'], 664, { application_no: 0.3 }],
    ['变压器方案', 'equipment_name', ['application_no', 'equipment_name'], 229, { application_no: 0.87, equipment_name: 0.9 }],
  ];
  const spec = {
    id: 'star-package', name: '星型 schema', version: '0.1.0', status: 'draft',
    description: '共享 application_no 的星型 schema。',
    objects: layout.map(([key, primaryKey, columns]) => objectOf(key, primaryKey, columns)),
    rules: [], operations: [], metrics: [],
  } as unknown as PackageSpec;
  const tables = layout.map(([key, , columns, rowCount, distinct]) => tableOf(key, columns, rowCount, distinct));

  const result = suggestObjectRelations(spec, tables, { limit: 20 });
  const shared = result.candidates.filter((item) => item.matchMode === 'shared_key');
  assert.equal(shared.length, 3, JSON.stringify(result.candidates, null, 1));
  // 唯一性最高的那张表当枢纽，其余指向它
  assert.ok(shared.every((item) => item.targetObject === '业务受理'));
  assert.deepEqual(
    shared.map((item) => item.sourceObject).sort(),
    ['互感器方案', '变压器方案', '接入方案'],
  );
  // 方向说明来自实测唯一性，不是命名猜测
  const manyToOne = shared.find((item) => item.sourceObject === '互感器方案');
  assert.match(manyToOne!.note, /多行对一行/);
  assert.match(manyToOne!.reason, /sourceUniqueRatio=0\.300/);
  // 列名用原始写法，不暴露归一化后的内部形态
  assert.match(manyToOne!.note, /application_no/);
  assert.doesNotMatch(manyToOne!.note, /applicationno/);
});
