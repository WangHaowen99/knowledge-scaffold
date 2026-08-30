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
