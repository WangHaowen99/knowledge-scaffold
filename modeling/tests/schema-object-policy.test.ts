import assert from 'node:assert/strict';
import test from 'node:test';

import type { DataDictTable, PackageSpec } from '../src/types.js';
import { renderPackage } from '../src/workflow/package-render.js';
import {
  enforceObjectSourcePolicy,
  expectedSchemaObjects,
  schemaObjectPolicyIssues,
} from '../src/workflow/schema-object-policy.js';

function schemaTable(name: string, fields: string[], rowCount = 1): DataDictTable {
  return {
    name,
    documentId: 'doc-schema',
    materialPurpose: 'schema',
    rowCount,
    fields: fields.map((field) => ({
      name: field,
      inferredType: 'string',
      sampleValues: rowCount ? [`${field}_value`] : [],
      distinctRatio: rowCount ? 1 : 0,
    })),
  };
}

function spec(objects: PackageSpec['objects']): PackageSpec {
  return {
    id: 'schema-policy', name: 'Schema policy', description: 'synthetic', version: '0.1.0',
    language: 'zh-CN', objects, operations: [], defaultProviderAlias: 'default',
  };
}

test('Schema 模式逐 Sheet 生成对象并包含只有表头的空 Sheet', () => {
  const tables = [
    schemaTable('customer_sheet', ['customer_id', 'customer_name']),
    schemaTable('empty_sheet', ['empty_id', 'empty_note'], 0),
  ];
  const objects = expectedSchemaObjects(tables).map((item) => item.object);
  const packageSpec = spec(objects);
  assert.equal(objects.length, 2);
  assert.deepEqual(schemaObjectPolicyIssues(packageSpec, renderPackage(packageSpec), tables), []);
});

test('Schema 物理策略允许业务语义和明确必填决定独立演进', () => {
  const tables = [schemaTable('record_sheet', ['record_id', 'status_code'])];
  const object = expectedSchemaObjects(tables)[0].object;
  object.title = '业务记录';
  object.description = '合成业务记录对象。';
  object.fields = object.fields.map((field) => ({
    ...field,
    cnName: field.key === 'record_id' ? '记录编号' : '状态代码',
    note: '合成字段说明。',
    required: field.key === 'record_id',
  }));
  const packageSpec = spec([object]);

  assert.deepEqual(schemaObjectPolicyIssues(packageSpec, renderPackage(packageSpec), tables), []);
});

test('Schema 策略要求对象字段和 mapping 使用一致的布尔必填标志', () => {
  const tables = [schemaTable('record_sheet', ['record_id', 'status_code'])];
  const object = expectedSchemaObjects(tables)[0].object;
  const invalidObject = structuredClone(object);
  delete invalidObject.fields[0].required;
  const invalidSpec = spec([invalidObject]);
  assert.ok(schemaObjectPolicyIssues(invalidSpec, renderPackage(invalidSpec), tables)
    .some((issue) => issue.code === 'FIELD_REQUIRED_INVALID' && issue.fieldKey === 'record_id'));

  const validSpec = spec([object]);
  const files = renderPackage(validSpec);
  const mapping = JSON.parse(files['mappings/object-data-mappings.json']);
  mapping.objects.record_sheet.fields.record_id.required = !object.fields[0].required;
  files['mappings/object-data-mappings.json'] = `${JSON.stringify(mapping, null, 2)}\n`;
  assert.ok(schemaObjectPolicyIssues(validSpec, files, tables)
    .some((issue) => issue.code === 'FIELD_REQUIRED_MAPPING_MISMATCH' && issue.fieldKey === 'record_id'));
});

test('Schema 模式拒绝漏 Sheet、额外派生对象和缺失 mapping', () => {
  const tables = [schemaTable('customer_sheet', ['customer_id']), schemaTable('order_sheet', ['order_id'])];
  const physical = expectedSchemaObjects(tables)[0].object;
  const packageSpec = spec([physical, {
    key: 'derived_summary', title: '派生汇总', description: '不允许', fields: [],
  }]);
  const codes = schemaObjectPolicyIssues(packageSpec, renderPackage(packageSpec), tables).map((issue) => issue.code);
  assert.ok(codes.includes('SCHEMA_OBJECT_MISSING'));
  assert.ok(codes.includes('DERIVED_OBJECT_FORBIDDEN_IN_SCHEMA_MODE'));

  const files = renderPackage(spec(expectedSchemaObjects(tables).map((item) => item.object)));
  delete files['mappings/object-data-mappings.json'];
  assert.ok(schemaObjectPolicyIssues(spec(expectedSchemaObjects(tables).map((item) => item.object)), files, tables)
    .some((issue) => issue.code === 'SCHEMA_MAPPING_MISSING'));
});

test('无 Schema 时只允许虚拟对象且不生成 mapping', () => {
  const packageSpec = spec([{
    key: 'virtual_customer', title: '虚拟客户', description: '纯语义对象',
    fields: [{ key: 'name', cnName: '名称', type: 'string' }],
  }]);
  const files = renderPackage(packageSpec);
  assert.equal(files['mappings/object-data-mappings.json'], undefined);
  assert.deepEqual(schemaObjectPolicyIssues(packageSpec, files, []), []);
});

test('旧批生成在无 Schema 时确定性移除模型提交的物理绑定', () => {
  const governed = enforceObjectSourcePolicy(spec([{
    key: 'virtual_customer', title: '虚拟客户', description: '纯语义对象',
    sourceTable: 'ordinary_sheet',
    sourceRefs: [{ kind: 'table', documentId: 'doc-ordinary', table: 'ordinary_sheet' }],
    primaryKey: ['customer_id'],
    fields: [{
      key: 'customer_id', cnName: '客户编号', type: 'string',
      sourceColumn: 'CUSTOMER_ID', valueBindingRef: 'customer-code',
    }],
  }]), [{
    name: 'ordinary_sheet', documentId: 'doc-ordinary', materialPurpose: 'evidence',
    rowCount: 1, fields: [{
      name: 'CUSTOMER_ID', inferredType: 'string', sampleValues: ['synthetic-001'], distinctRatio: 1,
    }],
  }]);

  assert.equal(governed.objects[0].sourceTable, undefined);
  assert.deepEqual(governed.objects[0].sourceRefs ?? [], []);
  assert.equal(governed.objects[0].fields[0].sourceColumn, undefined);
  assert.equal(governed.objects[0].fields[0].valueBindingRef, undefined);
  assert.equal(renderPackage(governed)['mappings/object-data-mappings.json'], undefined);
});

test('旧批生成在 Schema 模式下忽略模型派生对象并采用全部 Sheet', () => {
  const tables = [
    schemaTable('customer_sheet', ['customer_id']),
    schemaTable('empty_sheet', ['empty_id'], 0),
  ];
  const governed = enforceObjectSourcePolicy(spec([{
    key: 'derived_summary', title: '派生汇总', description: '模型候选', fields: [],
  }]), tables);

  assert.deepEqual(governed.objects.map((object) => object.key), ['customer_sheet', 'empty_sheet']);
  assert.deepEqual(schemaObjectPolicyIssues(governed, renderPackage(governed), tables), []);
});
