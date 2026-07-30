import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extractGraphImportSchema } from '../src/graph-import/package-schema.js';
import {
  assertObjectDataMappings,
  findObjectDataMappingProblems,
  normalizeObjectDataMappings,
} from '../src/workflow/data-mapping.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import { renderPackage } from '../src/workflow/package-render.js';
import type { DataDictTable, PackageSpec } from '../src/types.js';

function dataTable(name: string, columns: string[]): DataDictTable {
  return {
    name,
    rowCount: 1,
    fields: columns.map((column) => ({
      name: column,
      inferredType: 'string',
      sampleValues: ['sample'],
      distinctRatio: 1,
    })),
  };
}

function packageSpec(): PackageSpec {
  return {
    id: 'mapping-demo', name: 'mapping demo', description: 'mapping demo package',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [
      {
        key: 'DataObject',
        title: '数据实体',
        description: '由 Excel 数据表支持的实体',
        sourceTable: 'DataSheet',
        primaryKey: ['id'],
        fields: [
          { key: 'id', cnName: '数据ID', sourceColumn: 'ID', type: 'string', required: true },
          { key: 'name', cnName: '名称', sourceColumn: 'NAME', type: 'string' },
        ],
        relations: [{ relation: 'references', target: 'ConceptObject' }],
      },
      {
        key: 'ConceptObject',
        title: '概念实体',
        description: '与数据实体来自同一 Excel Sheet 的第二个实体',
        sourceTable: 'DataSheet',
        primaryKey: ['code'],
        fields: [{ key: 'code', cnName: '编码', sourceColumn: 'CODE' }],
      },
    ],
    operations: [],
  };
}

test('package mapping：对象、字段和主键统一使用稳定 key', () => {
  const files = renderPackage(packageSpec());
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.deepEqual(Object.keys(mappings.objects), ['DataObject', 'ConceptObject']);
  assert.equal(mappings.objects.DataObject.resource, 'DataObject');
  assert.deepEqual(mappings.objects.DataObject.primary_key, ['id']);
  assert.deepEqual(Object.keys(mappings.objects.DataObject.fields), ['id', 'name']);
  assert.equal(mappings.objects.DataObject.fields.id.source, 'id');
  assert.equal(mappings.objects.DataObject.fields.name.source, 'name');
  assert.match(files['objects/DataObject.md'], /mapping:\n  provider_alias: data\n  resource: DataObject/);
});

test('package mapping：NL2SQL 与 MindsDB 使用来源 Sheet，Neo4j 使用 object key', () => {
  for (const dataSourceType of ['nl2sql', 'mindsdb'] as const) {
    const spec = { ...packageSpec(), dataSourceType };
    const files = renderPackage(spec);
    const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

    assert.equal(mappings.objects.DataObject.resource, 'DataSheet');
    assert.equal(mappings.objects.ConceptObject.resource, 'DataSheet');
    assert.match(files['objects/DataObject.md'], /mapping:\n  provider_alias: data\n  resource: DataSheet/);
    assert.deepEqual(runFormatChecks(spec, files), []);
  }

  const neo4j = { ...packageSpec(), dataSourceType: 'neo4j' as const };
  const neo4jFiles = renderPackage(neo4j);
  const neo4jMappings = JSON.parse(neo4jFiles['mappings/object-data-mappings.json']);
  assert.equal(neo4jMappings.objects.DataObject.resource, 'DataObject');
  assert.deepEqual(runFormatChecks(neo4j, neo4jFiles), []);
});

test('package mapping：未声明数据源类型的历史规格继续使用 object key', () => {
  const mappings = JSON.parse(renderPackage(packageSpec())['mappings/object-data-mappings.json']);
  assert.equal(mappings.objects.DataObject.resource, 'DataObject');
});

test('package mapping：中文名缺失或重复时稳定 key 不变', () => {
  const variants = [
    (spec: PackageSpec) => { delete spec.objects[0].fields[0].cnName; },
    (spec: PackageSpec) => { spec.objects[1].title = '数据实体'; },
    (spec: PackageSpec) => { spec.objects[0].fields[1].cnName = '数据ID'; },
  ];
  for (const mutate of variants) {
    const spec = packageSpec();
    mutate(spec);
    const mappings = JSON.parse(renderPackage(spec)['mappings/object-data-mappings.json']);
    assert.deepEqual(Object.keys(mappings.objects), ['DataObject', 'ConceptObject']);
    assert.deepEqual(mappings.objects.DataObject.primary_key, ['id']);
    assert.deepEqual(Object.keys(mappings.objects.DataObject.fields), ['id', 'name']);
  }
});

test('package mapping：新生成规格允许多类来源且仍要求字段中文名', async () => {
  const { specObjectSchema } = await import('../src/workflow/tools.js');
  const schema = specObjectSchema() as any;
  const fieldSchema = schema.properties.fields.items;
  assert.equal(schema.required.includes('sourceTable'), false);
  assert.ok(schema.properties.sourceTable);
  assert.ok(schema.properties.sourceRefs);
  assert.ok(fieldSchema.required.includes('cnName'));
});

test('package mapping：同一来源 Sheet 可以支持多个实体，并写入对象索引', () => {
  const spec = packageSpec();
  const files = renderPackage(spec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.deepEqual(Object.keys(mappings.objects), ['DataObject', 'ConceptObject']);
  assert.equal(mappings.objects.DataObject.resource, 'DataObject');
  assert.equal(mappings.objects.ConceptObject.resource, 'ConceptObject');
  assert.match(files['objects/index.md'], /\| object key \| 标题 \| 说明 \| 来源Sheet \|/);
  assert.match(files['objects/index.md'], /\| DataObject \| 数据实体 \| .* \| DataSheet \|/);
  assert.match(files['objects/index.md'], /\| ConceptObject \| 概念实体 \| .* \| DataSheet \|/);

  const graphSchema = extractGraphImportSchema(spec, files);
  assert.deepEqual(
    graphSchema.objects.map((object) => [object.key, object.defaultSheet]),
    [['DataObject', 'DataSheet'], ['ConceptObject', 'DataSheet']],
  );
});

test('package mapping format：纯渲染产物符合语义 mapping 契约', () => {
  const spec = packageSpec();
  assert.deepEqual(runFormatChecks(spec, renderPackage(spec)), []);
});

test('package mapping format：对象索引必须保留来源 Sheet 列和值', () => {
  const spec = packageSpec();
  const missingColumn = renderPackage(spec);
  missingColumn['objects/index.md'] = missingColumn['objects/index.md'].replace(' | 来源Sheet |', ' |');
  assert.ok(runFormatChecks(spec, missingColumn).some((finding) => /缺少 来源Sheet 列/.test(finding.message)));

  const missingValue = renderPackage(spec);
  missingValue['objects/index.md'] = missingValue['objects/index.md'].replace('| DataSheet |', '|  |');
  assert.ok(runFormatChecks(spec, missingValue).some((finding) => /DataObject.*缺少 来源Sheet/.test(finding.message)));
});

test('package mapping：已声明的数据映射必须匹配真实 Sheet、字段和主键', () => {
  const valid = packageSpec();
  assert.deepEqual(findObjectDataMappingProblems(valid, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])]), []);

  const missingSource = packageSpec();
  delete missingSource.objects[0].sourceTable;
  assert.deepEqual(
    findObjectDataMappingProblems(missingSource, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])]),
    [],
  );

  const missingTable = packageSpec();
  missingTable.objects[0].sourceTable = 'MissingSheet';
  assert.match(
    findObjectDataMappingProblems(missingTable, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])])[0].message,
    /MissingSheet.*不存在/,
  );

  const missingColumn = packageSpec();
  assert.match(
    findObjectDataMappingProblems(missingColumn, [dataTable('DataSheet', ['ID'])])[0].message,
    /属性.*name.*NAME.*不存在/,
  );

  const invalidPrimary = packageSpec();
  invalidPrimary.objects[0].primaryKey = ['missing'];
  assert.match(
    findObjectDataMappingProblems(invalidPrimary, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])])[0].message,
    /主键.*missing.*不属于对象字段/,
  );

  assert.throws(
    () => assertObjectDataMappings(missingTable, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])]),
    /知识包数据映射无效.*MissingSheet/,
  );
});

test('package mapping：字段名仅大小写不同时规范化为 Excel 真实列名', () => {
  const source = packageSpec();
  source.objects[0].fields[0].sourceColumn = 'id';
  delete source.objects[0].fields[1].sourceColumn;
  source.objects[1].fields[0].sourceColumn = 'code';

  const normalized = normalizeObjectDataMappings(
    source,
    [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])],
  );

  assert.deepEqual(
    normalized.objects.flatMap((object) => object.fields.map((field) => field.sourceColumn)),
    ['ID', 'NAME', 'CODE'],
  );
  assert.equal(source.objects[0].fields[0].sourceColumn, 'id');
  assert.equal(source.objects[0].fields[1].sourceColumn, undefined);
  assert.deepEqual(findObjectDataMappingProblems(normalized, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])]), []);

  const graphSchema = extractGraphImportSchema(normalized, renderPackage(normalized));
  assert.deepEqual(
    graphSchema.objects.flatMap((object) => object.propertyMappings.map((mapping) => mapping.sourceColumn)),
    ['ID', 'NAME', 'CODE'],
  );
});

test('package mapping：仅大小写不同的重名列存在歧义时不自动选择', () => {
  const source = packageSpec();
  source.objects[0].fields[0].sourceColumn = 'Id';

  const normalized = normalizeObjectDataMappings(
    source,
    [dataTable('DataSheet', ['ID', 'id', 'NAME', 'CODE'])],
  );

  assert.equal(normalized.objects[0].fields[0].sourceColumn, 'Id');
  assert.match(
    findObjectDataMappingProblems(normalized, [dataTable('DataSheet', ['ID', 'id', 'NAME', 'CODE'])])[0].message,
    /来源字段.*Id.*不存在/,
  );
});

test('package mapping：最终 mapping 覆盖不能伪造 resource、字段或对象', () => {
  const spec = packageSpec();
  const files = renderPackage(spec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);
  mappings.objects.DataObject.resource = 'OtherObject';
  mappings.objects.DataObject.fields.id.source = 'ID';
  mappings.objects.GhostObject = {
    ...mappings.objects.DataObject,
    resource: 'GhostObject',
  };
  files['mappings/object-data-mappings.json'] = JSON.stringify(mappings, null, 2);

  const messages = runFormatChecks(spec, files).map((finding) => finding.message);
  assert.ok(messages.some((message) => /DataObject.*resource.*DataObject.*OtherObject/.test(message)));
  assert.ok(messages.some((message) => /DataObject.*字段.*id.*source.*id.*ID/.test(message)));
  assert.ok(messages.some((message) => /不存在的 object key GhostObject/.test(message)));
});
