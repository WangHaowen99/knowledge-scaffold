import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import test from 'node:test';

import type { DataDictTable, PackageSpec } from '../src/types.js';
import { parseRenderedPackageFiles } from '../src/workflow/package-import.js';
import { renderPackage } from '../src/workflow/package-render.js';
import {
  buildPhysicalSchemaObjects,
  preservePhysicalSchemaSemantics,
} from '../src/workflow/schema-manifest.js';
import { analyzeSchemaSemantics } from '../src/workflow/schema-semantics.js';

function schemaTable(): DataDictTable {
  return {
    name: 'record_sheet',
    documentId: 'document-synthetic-schema',
    materialPurpose: 'schema',
    rowCount: 2,
    fields: [
      {
        name: 'record_id', inferredType: 'string',
        sampleValues: ['record_0001', 'record_0002'], distinctRatio: 1, nonEmptyRatio: 1,
      },
      {
        name: 'status_code', inferredType: 'string',
        sampleValues: ['enabled'], distinctRatio: 0.5, nonEmptyRatio: 1,
      },
    ],
  };
}

function packageSpec(table: DataDictTable): PackageSpec {
  return {
    id: 'schema-semantics-synthetic',
    name: 'Schema semantic test',
    description: 'Synthetic package for semantic validation.',
    version: '0.1.0',
    language: 'zh-CN',
    defaultProviderAlias: 'default',
    objects: buildPhysicalSchemaObjects([table]).map((item) => item.object),
    operations: [],
  };
}

test('Schema 扫描定位技术占位语义且无约束来源的推断主键归一为必填', () => {
  const table = schemaTable();
  const spec = packageSpec(table);
  const status = analyzeSchemaSemantics(spec, [table]);
  const codes = status.issues.map((item) => item.code);

  assert.equal(status.available, true);
  assert.equal(status.pass, false);
  assert.ok(codes.includes('OBJECT_TITLE_PLACEHOLDER'));
  assert.ok(codes.includes('OBJECT_DESCRIPTION_PLACEHOLDER'));
  assert.equal(codes.filter((code) => code === 'FIELD_CN_NAME_PLACEHOLDER').length, 2);
  assert.equal(spec.objects[0].fields[0].required, true);
  assert.equal(spec.objects[0].fields[1].required, false);
});

test('业务标题、字段语义和明确必填决定补齐后通过阻断门禁', () => {
  const table = schemaTable();
  const spec = packageSpec(table);
  spec.objects[0] = {
    ...spec.objects[0],
    title: '业务记录',
    description: '保存业务处理过程中的稳定记录。',
    fields: spec.objects[0].fields.map((field) => ({
      ...field,
      cnName: field.key === 'record_id' ? '记录编号' : '状态代码',
      note: field.key === 'record_id' ? '记录的稳定业务标识。' : '记录当前所处的业务状态。',
      required: field.key === 'record_id' ? true : false,
    })),
  };

  const status = analyzeSchemaSemantics(spec, [table]);
  assert.equal(status.pass, true);
  assert.equal(status.errorCount, 0);
  assert.equal(status.warningCount, 0);
});

test('字段业务说明为空时不产生警告或阻断，也不因操作引用升级', () => {
  const table = schemaTable();
  const spec = packageSpec(table);
  spec.objects[0] = {
    ...spec.objects[0],
    title: '业务记录',
    description: '保存业务处理过程中的稳定记录。',
    fields: spec.objects[0].fields.map((field) => ({
      ...field,
      cnName: field.key === 'record_id' ? '记录编号' : '状态代码',
      ...(field.key === 'record_id' ? { note: '记录的稳定业务标识。' } : {}),
      required: field.key === 'record_id',
    })),
  };
  spec.operations = [{
    key: 'inspect_record',
    title: '检查业务记录',
    description: '根据 status_code 检查记录状态。',
    inputs: [{ param: 'status_code', type: 'string', required: true }],
    outputs: [{ field: 'result', type: 'string' }],
    flow: ['读取 status_code 并返回检查结果。'],
    relatedObjects: ['record_sheet'],
  }];

  const status = analyzeSchemaSemantics(spec, [table]);
  assert.equal(status.pass, true);
  assert.equal(status.errorCount, 0);
  assert.equal(status.warningCount, 0);
  assert.deepEqual(status.issues, []);
});

test('历史非布尔必填值报告为可自动修复异常而不是用户裁决问题', () => {
  const table = schemaTable();
  const spec = packageSpec(table);
  delete spec.objects[0].fields[0].required;

  const issue = analyzeSchemaSemantics(spec, [table]).issues.find((item) => (
    item.code === 'FIELD_REQUIRED_INVALID' && item.fieldKey === 'record_id'
  ));
  assert.match(issue?.suggestion ?? '', /Core.*推断主键回退规则/u);
});

test('缺省和未知 required 统一按 false 写入对象 Markdown 与 mapping', () => {
  const table = schemaTable();
  const spec = packageSpec(table);
  const files = renderPackage(spec);
  const mapping = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.equal(mapping.objects.record_sheet.fields.status_code.required, false);
  assert.match(files['objects/record_sheet.md'], /\| status_code \| status_code \| string \| 否 \|/u);
  assert.equal(parseRenderedPackageFiles(files).objects[0].fields[1].required, false);
  const unknownLabelFiles = {
    ...files,
    'objects/record_sheet.md': files['objects/record_sheet.md'].replace(
      '| status_code | status_code | string | 否 |',
      '| status_code | status_code | string | 待确认 |',
    ),
  };
  assert.equal(parseRenderedPackageFiles(unknownLabelFiles).objects[0].fields[1].required, false);
});

test('二次 Schema 同步保留已确认语义但重新生成物理身份', () => {
  const table = schemaTable();
  const first = buildPhysicalSchemaObjects([table]);
  const previous = structuredClone(first[0].object);
  previous.title = '业务记录';
  previous.description = '保存业务处理过程中的稳定记录。';
  previous.aliases = ['业务档案'];
  previous.queryBoundary = '只用于合成测试中的记录查询。';
  previous.fields = previous.fields.map((field) => ({
    ...field,
    cnName: field.key === 'record_id' ? '记录编号' : '状态代码',
    note: field.key === 'record_id' ? '记录的稳定业务标识。' : '记录当前所处的业务状态。',
    alias: field.key === 'record_id' ? '记录标识' : undefined,
    required: field.key === 'record_id' ? true : false,
  }));

  const changedTable = structuredClone(table);
  changedTable.fields[1].inferredType = 'number';
  const synced = preservePhysicalSchemaSemantics(
    buildPhysicalSchemaObjects([changedTable]),
    [previous],
  )[0].object;

  assert.equal(synced.title, '业务记录');
  assert.equal(synced.description, '保存业务处理过程中的稳定记录。');
  assert.deepEqual(synced.aliases, ['业务档案']);
  assert.equal(synced.fields[1].cnName, '状态代码');
  assert.equal(synced.fields[1].note, '记录当前所处的业务状态。');
  assert.equal(synced.fields[0].required, true);
  assert.equal(synced.fields[1].required, false);
  assert.equal(synced.fields[1].type, 'number');
  assert.equal(synced.fields[1].sourceColumn, 'status_code');
});

test('100 个 Sheet、10000 个字段的确定性语义扫描保持线性预算', () => {
  const tables: DataDictTable[] = Array.from({ length: 100 }, (_, tableIndex) => ({
    name: `sheet_${tableIndex + 1}`,
    documentId: 'document-large-synthetic-schema',
    materialPurpose: 'schema',
    rowCount: 0,
    fields: Array.from({ length: 100 }, (_, fieldIndex) => ({
      name: `field_${fieldIndex + 1}`,
      inferredType: 'string' as const,
      sampleValues: [],
      distinctRatio: 0,
    })),
  }));
  const spec: PackageSpec = {
    id: 'large-schema-semantics-synthetic',
    name: 'Large synthetic schema',
    description: 'Synthetic performance fixture.',
    version: '0.1.0',
    language: 'zh-CN',
    defaultProviderAlias: 'default',
    objects: buildPhysicalSchemaObjects(tables).map((item) => item.object),
    operations: [],
  };

  const started = performance.now();
  const status = analyzeSchemaSemantics(spec, tables);
  const elapsed = performance.now() - started;

  assert.equal(status.objectCount, 100);
  assert.equal(status.fieldCount, 10_000);
  assert.equal(status.pass, false);
  assert.ok(elapsed < 3_000, `扫描耗时 ${elapsed.toFixed(1)} ms`);
});
