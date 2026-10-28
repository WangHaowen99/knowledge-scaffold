import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { PackageSpec } from '../src/types.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { parseRenderedPackageFiles } from '../src/workflow/package-import.js';
import { extractGraphImportSchema } from '../src/graph-import/package-schema.js';
import { validateGraphImportMapping } from '../src/graph-import/validator.js';
import { parseExcel } from '../src/documents/parser.js';
import { workbookBuffer } from './helpers/graph-import-fixtures.js';
import { addKnownRelationMappings, graphImportMappingPayload } from '../frontend/src/graph-import-form.js';
import { toSpecObjectRelations } from '../src/workflow/ontology-model-validation.js';
import { validateObjectBatch, validateSpecIntegrity } from '../src/workflow/spec-validation.js';

// All records and schemas in this file are synthetic.
function fixture(): PackageSpec {
  return {
    id: 'knowledge_package_demo', name: '合成关系测试', description: '合成数据',
    version: '0.1.0', language: 'zh-CN', defaultProviderAlias: 'graph', operations: [],
    objects: [
      {
        key: 'Request', title: '请求', description: '合成请求', sourceTable: 'Requests',
        primaryKey: ['request_id'],
        fields: ['request_id', 'source_event_id', 'group_id'].map((key) => ({ key, sourceColumn: key })),
        relations: [{
          relation: 'references', target: 'Event', note: '按来源事件关联', evidence: ['synthetic'],
          sourceJoinProperty: 'source_event_id', targetJoinProperty: 'event_id',
        }],
      },
      {
        key: 'Event', title: '事件', description: '合成事件', sourceTable: 'Events',
        primaryKey: ['event_id'],
        fields: ['event_id', 'group_id'].map((key) => ({ key, sourceColumn: key })),
      },
    ],
  };
}

test('explicit relation fields survive package render, import, and re-render', () => {
  const spec = fixture();
  const files = renderPackage(spec);
  const restored = parseRenderedPackageFiles(files);
  assert.deepEqual(restored.objects.find((object) => object.key === 'Request')?.relations, spec.objects[0].relations);
  const schema = extractGraphImportSchema(restored, renderPackage(restored));
  assert.equal(schema.relations[0].sourceJoinProperty, 'source_event_id');
  assert.equal(schema.relations[0].targetJoinProperty, 'event_id');
  assert.equal(schema.relations[0].note, '按来源事件关联');
  assert.match(files['objects/Request.md'], /关系 \| 目标对象 \| 说明 \| 证据 \| 源关联字段 \| 目标关联字段/);
});

test('same-named relations with distinct join fields survive validation and graph mapping', () => {
  const spec = fixture();
  spec.objects[0].relations!.push({
    relation: 'references', target: 'Event', sourceJoinProperty: 'group_id', targetJoinProperty: 'group_id',
  });
  assert.deepEqual(validateObjectBatch([], spec.objects), []);
  assert.doesNotThrow(() => validateSpecIntegrity(spec));
  const files = renderPackage(spec);
  const restored = parseRenderedPackageFiles(files);
  assert.equal(restored.objects.find((object) => object.key === 'Request')?.relations?.length, 2);
  const schema = extractGraphImportSchema(restored, files);
  const tables = parseExcel(workbookBuffer({
    Requests: [['request_id', 'source_event_id', 'group_id'], ['R0', 'E0', 'G0']],
    Events: [['event_id', 'group_id'], ['E0', 'G0']],
  }));
  const preview = { schema, sheets: tables.map((table) => ({
    name: table.name, columns: table.fields.map((field) => field.name), rowCount: table.rows!.length,
  })) };
  const mappings = addKnownRelationMappings([], preview);
  assert.equal(mappings.length, 2);
  assert.equal(addKnownRelationMappings(mappings, preview).length, 2);
  const mapping = graphImportMappingPayload(mappings);
  assert.equal(validateGraphImportMapping(schema, tables, mapping).relationshipCount, 2);
  assert.throws(() => validateGraphImportMapping(schema, tables, {
    relations: [mapping.relations[0], mapping.relations[0]],
  }), /不能重复添加/);
  spec.objects[0].relations!.push({ ...spec.objects[0].relations![0] });
  assert.throws(() => validateSpecIntegrity(spec), /重复/);
});

test('self relations survive graph schema and mapping with or without explicit join fields', () => {
  for (const explicit of [false, true]) {
    const spec = fixture();
    spec.objects[0].relations = [{
      relation: 'reports_to', target: 'Request',
      ...(explicit ? { sourceJoinProperty: 'source_event_id', targetJoinProperty: 'request_id' } : {}),
    }];
    assert.doesNotThrow(() => validateSpecIntegrity(spec));
    const schema = extractGraphImportSchema(spec, renderPackage(spec));
    const tables = parseExcel(workbookBuffer({
      Requests: [['request_id', 'source_event_id', 'group_id'], ['R0', 'R0', 'G0']],
      Events: [['event_id', 'group_id'], ['E0', 'G0']],
    }));
    const preview = { schema, sheets: tables.map((table) => ({
      name: table.name, columns: table.fields.map((field) => field.name), rowCount: table.rows!.length,
    })) };
    const mapping = graphImportMappingPayload(addKnownRelationMappings([], preview));
    const validation = validateGraphImportMapping(schema, tables, mapping);
    assert.equal(validation.relationshipCount, 1);
    assert.equal(validation.relations[0].sourceObject, 'Request');
    assert.equal(validation.relations[0].targetObject, 'Request');
  }
});

test('old relation tables keep their notes and evidence', () => {
  const spec = fixture();
  spec.objects[0].relations = [{ relation: '关联', target: 'Event', note: '旧格式', evidence: ['synthetic'] }];
  const files = renderPackage(spec);
  assert.ok(!files['objects/Request.md'].includes('源关联字段'));
  assert.deepEqual(parseRenderedPackageFiles(files).objects.find((object) => object.key === 'Request')?.relations, spec.objects[0].relations);
  assert.equal(extractGraphImportSchema(spec, files).relations[0].relationName, '关联');
});

test('partial or missing relation fields cannot silently fall back to inference', () => {
  for (const field of ['', 'missing']) {
    const spec = fixture();
    spec.objects[0].relations![0].targetJoinProperty = field;
    const files = renderPackage(spec);
    assert.throws(() => extractGraphImportSchema(spec, files), /同时声明|关联字段不存在/);
  }
});

test('explicit fields build the correct eight edges instead of joining all rows in a group', () => {
  const spec = fixture();
  const schema = extractGraphImportSchema(spec, renderPackage(spec));
  const tables = parseExcel(workbookBuffer({
    Requests: [
      ['request_id', 'source_event_id', 'group_id'],
      ...Array.from({ length: 8 }, (_, i) => [`R${i}`, `E${7 - i}`, 'group_demo']),
    ],
    Events: [
      ['event_id', 'group_id'],
      ...Array.from({ length: 9 }, (_, i) => [`E${i}`, 'group_demo']),
    ],
  }));
  const preview = {
    schema,
    sheets: tables.map((table) => ({ name: table.name, columns: table.fields.map((f) => f.name), rowCount: table.rows!.length })),
  };
  const mapping = graphImportMappingPayload(addKnownRelationMappings([], preview));
  assert.equal(mapping.relations[0].sourceJoinProperty, 'source_event_id');
  assert.equal(mapping.relations[0].targetJoinProperty, 'event_id');
  const result = validateGraphImportMapping(schema, tables, mapping);
  assert.equal(result.relationshipCount, 8);
  assert.equal(result.skippedUnmatchedJoinCount, 0);
  assert.deepEqual(result.relations[0].pairs, Array.from({ length: 8 }, (_, i) => ({
    sourceRecordKey: `node:Request:${String(i + 1).padStart(8, '0')}`,
    targetRecordKey: `node:Event:${String(8 - i).padStart(8, '0')}`,
  })));
});

test('known same-sheet relations still honor explicit fields', () => {
  const spec = fixture();
  const schema = extractGraphImportSchema(spec, renderPackage(spec));
  schema.objects.forEach((object) => { object.defaultSheet = 'Wide'; });
  const mapping = addKnownRelationMappings([], {
    schema,
    sheets: [{ name: 'Wide', columns: ['request_id', 'source_event_id', 'event_id', 'group_id'], rowCount: 2 }],
  });
  assert.equal(mapping[0].matchMode, 'field_match');
  assert.equal(mapping[0].sourceJoinProperty, 'source_event_id');
  assert.equal(mapping[0].targetJoinProperty, 'event_id');
});

test('valid relations with no matching data report unmatched rows', () => {
  const spec = fixture();
  const schema = extractGraphImportSchema(spec, renderPackage(spec));
  const tables = parseExcel(workbookBuffer({
    Requests: [['request_id', 'source_event_id', 'group_id'], ['R0', 'unmatched', 'group_demo']],
    Events: [['event_id', 'group_id'], ['E0', 'group_demo']],
  }));
  const result = validateGraphImportMapping(schema, tables, { relations: [{
    ...schema.relations[0], matchMode: 'field_match',
    sourceJoinProperty: 'source_event_id', targetJoinProperty: 'event_id',
  }] });
  assert.equal(result.relationshipCount, 0);
  assert.equal(result.skippedUnmatchedJoinCount, 1);
  assert.match(result.warnings.join('\n'), /未匹配/);
});

test('Studio updates preserve join fields for unchanged relation identities', () => {
  const previous = fixture().objects[0].relations!;
  const result = toSpecObjectRelations([{
    kind: 'object_relation', targetKind: 'object', targetKey: 'Event',
    label: 'references', description: '修改说明',
  }], previous);
  assert.equal(result[0].sourceJoinProperty, 'source_event_id');
  assert.equal(result[0].targetJoinProperty, 'event_id');
});

test('explicit relation preflight rejects duplicate target keys and incompatible types', () => {
  const spec = fixture();
  const schema = extractGraphImportSchema(spec, renderPackage(spec));
  const mapping = { relations: [{
    ...schema.relations[0], matchMode: 'field_match' as const,
    sourceJoinProperty: 'source_event_id', targetJoinProperty: 'event_id',
  }] };
  const duplicate = parseExcel(workbookBuffer({
    Requests: [['request_id', 'source_event_id', 'group_id'], ['R0', 'E0', 'group_demo']],
    Events: [['event_id', 'group_id'], ['E0', 'group_demo'], ['E0', 'group_demo']],
  }));
  assert.throws(() => validateGraphImportMapping(schema, duplicate, mapping), /主键重复/);
  const incompatible = parseExcel(workbookBuffer({
    Requests: [['request_id', 'source_event_id', 'group_id'], ['R0', 'E0', 'group_demo']],
    Events: [['event_id', 'group_id'], [10, 'group_demo']],
  }));
  assert.throws(() => validateGraphImportMapping(schema, incompatible, mapping), /类型不兼容/);
});
