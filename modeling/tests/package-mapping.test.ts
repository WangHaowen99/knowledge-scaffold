import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extractGraphImportSchema } from '../src/graph-import/package-schema.js';
import {
  assertObjectDataMappings,
  findObjectDataMappingProblems,
  normalizeObjectDataMappings,
} from '../src/workflow/data-mapping.js';
import { packageGenerationContract } from '../src/workflow/package-contract.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { suggestSemanticBindings, suggestSemanticFieldRefs } from '../src/workflow/semantic-binding.js';
import {
  applySemanticBindingSelection,
  clearSemanticBindings,
  previewSemanticBindingCandidates,
} from '../src/workflow/semantic-binding-service.js';
import { validateTermTables } from '../src/workflow/term-table.js';
import type { DataDictTable, PackageSpec } from '../src/types.js';

function dataTable(
  name: string,
  columns: string[],
  rows: Record<string, string>[] = [],
  materialPurpose?: DataDictTable['materialPurpose'],
): DataDictTable {
  return {
    name,
    ...(materialPurpose ? { materialPurpose } : {}),
    rowCount: rows.length || 1,
    fields: columns.map((column) => ({
      name: column,
      inferredType: 'string',
      sampleValues: rows.length ? [...new Set(rows.map((row) => row[column] ?? '').filter(Boolean))] : ['sample'],
      distinctRatio: 1,
    })),
    rows: rows.map((row) => columns.map((column) => row[column] ?? '')),
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
        relations: [{ relation: '引用', target: 'ConceptObject' }],
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
  const relationSchema = schema.properties.relations.items.properties.relation;
  assert.match(relationSchema.description, /模型根据材料证据生成.*中文谓词/);
  assert.match(relationSchema.description, /不得包含源对象或目标对象.*连读/);
  assert.equal(relationSchema.enum, undefined);
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
  assert.equal(graphSchema.relations[0]?.relationName, '引用');
});

test('package mapping format：纯渲染产物符合语义 mapping 契约', () => {
  const spec = packageSpec();
  assert.deepEqual(runFormatChecks(spec, renderPackage(spec)), []);
});

test('package mapping contract：mapping 只在物理 Schema 模式下生成', () => {
  const contract = packageGenerationContract();

  assert.equal(contract.validatorVersion, 'package-contract-v6');
  assert.ok(!contract.packageFormat.requiredFiles.includes('mappings/object-data-mappings.json'));
  assert.equal(contract.packageFormat.mappings.required, 'when_physical_schema_exists');
  assert.equal(contract.packageFormat.mappings.forbidden, 'when_physical_schema_absent');
  assert.equal(contract.packageFormat.mappings.allowEmptyObjects, false);
});

test('package mapping format：mapping 缺失或为空时阻断知识包', () => {
  const spec = packageSpec();
  const missing = renderPackage(spec);
  delete missing['mappings/object-data-mappings.json'];
  const missingFindings = runFormatChecks(spec, missing);
  assert.ok(missingFindings.some((finding) => (
    finding.category === '必选文件缺失'
    && finding.target === 'mappings/object-data-mappings.json'
  )));

  const empty = renderPackage(spec);
  empty['mappings/object-data-mappings.json'] = '   ';
  const emptyFindings = runFormatChecks(spec, empty);
  assert.ok(emptyFindings.some((finding) => (
    finding.category === '必选文件缺失'
    && finding.target === 'mappings/object-data-mappings.json'
  )));
});

test('package mapping format：无物理对象时不生成 mapping', () => {
  const spec = packageSpec();
  spec.objects = [];
  const files = renderPackage(spec);
  assert.equal(files['mappings/object-data-mappings.json'], undefined);
  assert.deepEqual(runFormatChecks(spec, files), []);
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

test('package mapping format：对象来源在规格、详情、目录和运行时 mapping 之间必须一致', () => {
  const spec = packageSpec();
  const wrongIndex = renderPackage(spec);
  wrongIndex['objects/index.md'] = wrongIndex['objects/index.md'].replace('| DataSheet |', '| OtherSheet |');
  assert.ok(runFormatChecks(spec, wrongIndex).some((finding) => (
    finding.category === '对象来源不一致' && /DataObject.*DataSheet.*OtherSheet/.test(finding.message)
  )));

  const textOnlySpec = packageSpec();
  delete textOnlySpec.objects[0].sourceTable;
  const textOnlyFiles = renderPackage(textOnlySpec);
  textOnlyFiles['objects/DataObject.md'] = textOnlyFiles['objects/DataObject.md'].replace(
    '\n---\n# 业务定义',
    '\nsources:\n  - "表格：DataSheet"\n---\n# 业务定义',
  );
  textOnlyFiles['objects/index.md'] = textOnlyFiles['objects/index.md'].replace(
    '| DataObject | 数据实体 | 由 Excel 数据表支持的实体 | — |',
    '| DataObject | 数据实体 | 由 Excel 数据表支持的实体 | DataSheet |',
  );
  const findings = runFormatChecks(textOnlySpec, textOnlyFiles);
  assert.ok(findings.some((finding) => (
    finding.category === '对象来源不一致'
    && finding.target === 'objects/DataObject.md'
    && /未声明结构化表格来源/.test(finding.message)
  )));
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

test('package mapping：未声明来源角色和逐表字段的多来源对象继续阻断', () => {
  const spec = packageSpec();
  spec.objects[0] = {
    ...spec.objects[0],
    sourceTable: undefined,
    sourceRefs: [
      { kind: 'table', table: 'EntityBase' },
      { kind: 'table', table: 'EntityExtension' },
    ],
    primaryKey: ['event_uid'],
    fields: [
      { key: 'event_uid', cnName: '事件标识', sourceColumn: 'event_id', type: 'string', required: true },
      { key: 'customer_name', cnName: '企业名称', sourceColumn: 'company_name', type: 'string' },
    ],
  };

  const problems = findObjectDataMappingProblems(spec, [
    dataTable('EntityBase', ['event_id', 'company_name']),
    dataTable('EntityExtension', ['event_id', 'company_name']),
    dataTable('DataSheet', ['ID', 'NAME', 'CODE']),
  ]).filter((problem) => problem.objectKey === 'DataObject');

  assert.equal(problems.length, 1);
  assert.match(problems[0].message, /同时绑定了 2 张来源表/);
  assert.match(problems[0].message, /当前数据源类型不支持跨 Sheet 对象/);
});

test('package mapping：Neo4j 对象保存主表和一对一扩展表的完整运行时映射', () => {
  const spec = packageSpec();
  spec.dataSourceType = 'neo4j';
  spec.objects[0] = {
    ...spec.objects[0],
    sourceTable: undefined,
    sourceRefs: [
      {
        kind: 'table', table: 'EntityBase', role: 'primary',
        fieldBindings: [
          { fieldKey: 'entity_id', sourceColumn: 'base_id' },
          { fieldKey: 'entity_name', sourceColumn: 'base_name' },
        ],
      },
      {
        kind: 'table', table: 'EntityExtension', role: 'extension',
        fieldBindings: [
          { fieldKey: 'entity_id', sourceColumn: 'extension_id' },
          { fieldKey: 'detail', sourceColumn: 'detail_text' },
        ],
      },
    ],
    primaryKey: ['entity_id'],
    fields: [
      { key: 'entity_id', cnName: '实体标识', sourceColumn: 'base_id', type: 'string', required: true },
      { key: 'entity_name', cnName: '实体名称', sourceColumn: 'base_name', type: 'string' },
      { key: 'detail', cnName: '补充说明', sourceColumn: 'detail_text', type: 'string' },
    ],
  };
  const tables = [
    dataTable('EntityBase', ['base_id', 'base_name']),
    dataTable('EntityExtension', ['extension_id', 'detail_text']),
    dataTable('DataSheet', ['ID', 'NAME', 'CODE']),
  ];

  assert.deepEqual(findObjectDataMappingProblems(spec, tables), []);
  const files = renderPackage(spec);
  const mapping = JSON.parse(files['mappings/object-data-mappings.json']).objects.DataObject;
  assert.deepEqual(mapping.sources.map((source: any) => [source.role, source.resource]), [
    ['primary', 'EntityBase'],
    ['extension', 'EntityExtension'],
  ]);
  assert.equal(mapping.sources[1].fields.detail.source_column, 'detail_text');
  assert.match(files['objects/index.md'], /EntityBase、EntityExtension/u);
  assert.deepEqual(runFormatChecks(spec, files), []);
});

test('package mapping：无物理表映射的派生业务对象可汇总逐来源明细对象', () => {
  const spec = packageSpec();
  spec.objects.push({
    key: 'AggregatedEvent',
    title: '汇总线索事件',
    description: '通过关系和操作汇总各来源明细对象，不直接执行物理表绑定',
    primaryKey: ['event_uid'],
    fields: [
      { key: 'event_uid', cnName: '业务事件标识', type: 'string', required: true },
      { key: 'customer_name', cnName: '企业名称', type: 'string' },
    ],
    relations: [
      { relation: 'aggregates', target: 'DataObject' },
      { relation: 'aggregates', target: 'ConceptObject' },
    ],
  });

  assert.deepEqual(
    findObjectDataMappingProblems(spec, [dataTable('DataSheet', ['ID', 'NAME', 'CODE'])]),
    [],
  );
  const files = renderPackage(spec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);
  assert.equal(mappings.objects.AggregatedEvent, undefined);
  assert.match(files['objects/index.md'], /\| AggregatedEvent \| 汇总线索事件 \| .* \| — \|/);
  assert.deepEqual(runFormatChecks(spec, files), []);
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

test('package mapping：渲染用户确认的术语 binding 和字段引用', () => {
  const spec = packageSpec();
  spec.valueBindings = [{
    key: 'airport-code',
    providerAlias: 'data',
    resource: '机场中文代号',
    valueField: 'ARP_CD',
    semanticField: '机场名',
    aliasFields: ['英文名'],
  }];
  spec.objects[0].fields[0].key = 'ARP_CD';
  spec.objects[0].fields[0].sourceColumn = 'ARP_CD';
  spec.objects[0].fields[0].valueBindingRef = 'airport-code';
  spec.objects[0].primaryKey = ['ARP_CD'];

  const files = renderPackage(spec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.equal(mappings.schema_version, '0.1');
  assert.deepEqual(mappings.value_bindings, {
    'airport-code': {
      provider_alias: 'data',
      resource: '机场中文代号',
      value_field: 'ARP_CD',
      semantic_field: '机场名',
      alias_fields: ['英文名'],
    },
  });
  assert.equal(mappings.objects.DataObject.fields.ARP_CD.value_binding_ref, 'airport-code');
  assert.deepEqual(runFormatChecks(spec, files), []);
});

test('package mapping format：术语 binding 引用和 provider 必须合法', () => {
  const spec = packageSpec();
  const files = renderPackage(spec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);
  mappings.value_bindings = {
    DataObject: { resource: '机场中文代号', value_field: 'ARP_CD', semantic_field: '机场名' },
    'airport-code': { provider_alias: 'other', resource: '机场中文代号', value_field: 'ARP_CD', semantic_field: '机场名' },
  };
  mappings.objects.DataObject.fields.id.value_binding_ref = 'airport-code';
  files['mappings/object-data-mappings.json'] = JSON.stringify(mappings, null, 2);

  const messages = runFormatChecks(spec, files).map((finding) => finding.message);
  assert.ok(messages.some((message) => /value_bindings\.DataObject.*冲突/.test(message)));
  assert.ok(messages.some((message) => /provider_alias.*不一致/.test(message)));
});

test('semantic binding：普通 lookup 表不再自动推荐 binding', () => {
  const spec: PackageSpec = {
    id: 'item-demo', name: 'item demo', description: 'item demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [
      {
        key: 'transaction', title: '交易', description: '交易',
        sourceTable: 'transaction', fields: [
          { key: 'PRIMARY_ITEM_CODE', sourceColumn: 'PRIMARY_ITEM_CODE' },
          { key: 'BACKUP_ITEM_CODE', sourceColumn: 'BACKUP_ITEM_CODE' },
        ],
      },
      {
        key: 'item', title: '物料', description: '物料',
        sourceTable: 'item', fields: [
          { key: 'ITEM_CODE', sourceColumn: 'ITEM_CODE' },
          { key: 'CATEGORY_CODE', sourceColumn: 'CATEGORY_CODE' },
        ],
      },
    ],
    operations: [],
  };
  const tables = [
    dataTable('transaction', ['PRIMARY_ITEM_CODE', 'BACKUP_ITEM_CODE'], [
      { PRIMARY_ITEM_CODE: 'I001', BACKUP_ITEM_CODE: 'I002' },
      { PRIMARY_ITEM_CODE: 'I003', BACKUP_ITEM_CODE: 'I001' },
    ]),
    dataTable('item', ['ITEM_CODE', 'CATEGORY_CODE'], [
      { ITEM_CODE: 'I001', CATEGORY_CODE: 'C01' },
      { ITEM_CODE: 'I002', CATEGORY_CODE: 'C01' },
      { ITEM_CODE: 'I003', CATEGORY_CODE: 'C02' },
    ]),
    dataTable('item_lookup', ['ITEM_CODE', 'ITEM_NAME', 'ITEM_ALIAS'], [
      { ITEM_CODE: 'I001', ITEM_NAME: '物料一', ITEM_ALIAS: 'A1' },
      { ITEM_CODE: 'I002', ITEM_NAME: '物料二', ITEM_ALIAS: 'A2' },
      { ITEM_CODE: 'I003', ITEM_NAME: '物料三', ITEM_ALIAS: 'A3' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
});

test('semantic binding：知识结构尚未抽取对象字段时不推荐 binding', () => {
  const spec: PackageSpec = {
    id: 'item-demo', name: 'item demo', description: 'item demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
  const tables = [
    dataTable('item_lookup', ['ITEM_CODE', 'ITEM_NAME'], [
      { ITEM_CODE: 'I001', ITEM_NAME: '物料一' },
      { ITEM_CODE: 'I002', ITEM_NAME: '物料二' },
    ]),
  ];

  const earlyPreview = previewSemanticBindingCandidates(spec, tables);
  assert.equal(earlyPreview.bindingCount, 0);
  assert.equal(earlyPreview.fieldRefCount, 0);
  assert.deepEqual(earlyPreview.suggestions, []);
  assert.deepEqual(earlyPreview.fieldSuggestions, []);

  const modeledPreview = previewSemanticBindingCandidates({
    ...spec,
    objects: [{
      key: 'item', title: '物料', description: '物料',
      sourceTable: 'item',
      fields: [{ key: 'ITEM_CODE', sourceColumn: 'ITEM_CODE' }],
    }],
  }, tables);
  assert.equal(modeledPreview.bindingCount, 0);
  assert.deepEqual(modeledPreview.suggestions, []);
});

test('semantic binding：上传术语表后按固定列顺序生成 binding 并保留 alias 字段', () => {
  const spec: PackageSpec = {
    id: 'term-demo', name: 'term demo', description: 'term demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'Record', title: '记录', description: '记录',
      sourceTable: 'records',
      fields: [{ key: 'status_code', sourceColumn: 'STATUS_CODE' }],
    }],
    operations: [],
  };
  const tables = [
    dataTable('records', ['STATUS_CODE'], [
      { STATUS_CODE: 'A' },
      { STATUS_CODE: 'I' },
      { STATUS_CODE: 'X' },
    ]),
    dataTable('status_terms', ['STATUS_CODE', 'STATUS_NAME', 'STATUS_ALIAS', 'STATUS_EN_NAME'], [
      { STATUS_CODE: 'A', STATUS_NAME: '启用', STATUS_ALIAS: '有效', STATUS_EN_NAME: 'Active' },
      { STATUS_CODE: 'I', STATUS_NAME: '停用', STATUS_ALIAS: '无效', STATUS_EN_NAME: 'Inactive' },
    ], 'term'),
    dataTable('category_lookup', ['CATEGORY_CODE', 'CATEGORY_NAME'], [
      { CATEGORY_CODE: 'C01', CATEGORY_NAME: '类别一' },
    ]),
  ];

  const suggestions = suggestSemanticBindings(spec, tables);
  assert.equal(suggestions.length, 1);
  assert.equal(suggestions[0].resource, 'status_terms');
  assert.equal(suggestions[0].valueField, 'STATUS_CODE');
  assert.equal(suggestions[0].semanticField, 'STATUS_NAME');
  assert.deepEqual(suggestions[0].aliasFields, ['STATUS_ALIAS', 'STATUS_EN_NAME']);

  const withBinding = applySemanticBindingSelection(spec, tables, {
    bindingKeys: [suggestions[0].key],
    fieldRefs: [],
  }).spec;
  const fieldRefs = suggestSemanticFieldRefs(withBinding, tables);
  assert.equal(fieldRefs[0]?.objectKey, 'Record');
  assert.equal(fieldRefs[0]?.fieldKey, 'status_code');
  assert.equal(fieldRefs[0]?.coverage, 0.667);
  assert.equal(fieldRefs[0]?.matchedValueCount, 2);
  assert.equal(fieldRefs[0]?.sourceValueCount, 3);
});

test('semantic binding：术语表校验跳过空值并阻止非法 sheet 与一值多义', () => {
  assert.throws(() => validateTermTables([]), /至少需要包含一个 sheet/);
  assert.throws(() => validateTermTables([dataTable('bad_terms', ['STATUS_CODE'], [], 'term')]), /至少需要两列/);
  assert.doesNotThrow(() => validateTermTables([
    dataTable('status_terms', ['STATUS_CODE', 'STATUS_NAME'], [
      { STATUS_CODE: 'A', STATUS_NAME: '启用' },
      { STATUS_CODE: '', STATUS_NAME: '空值跳过' },
      { STATUS_CODE: 'B', STATUS_NAME: '' },
      { STATUS_CODE: 'A', STATUS_NAME: '启用' },
    ], 'term'),
  ]));
  assert.throws(() => validateTermTables([
    dataTable('status_terms', ['STATUS_CODE', 'STATUS_NAME'], [
      { STATUS_CODE: 'A', STATUS_NAME: '启用' },
      { STATUS_CODE: 'A', STATUS_NAME: '停用' },
    ], 'term'),
  ]), /对应多个 semantic/);
});

test('semantic binding：Neo4j 数据源下术语表字段名必须符合属性名格式', () => {
  const termTable = dataTable('status_terms', ['STATUS_CODE', 'STATUS_NAME', '别名/禁用写法'], [
    { STATUS_CODE: 'A', STATUS_NAME: '启用', '别名/禁用写法': '有效' },
  ], 'term');

  assert.throws(() => validateTermTables([termTable], { dataSourceType: 'neo4j' }), /不能作为 Neo4j 属性名/);
  assert.doesNotThrow(() => validateTermTables([termTable], { dataSourceType: 'nl2sql' }));
});

test('semantic binding：未标记为术语表的 code/name 资源表不自动推荐 binding', () => {
  const spec: PackageSpec = {
    id: 'entity-demo', name: 'entity demo', description: 'entity demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'record', title: '记录', description: '记录',
      sourceTable: 'record', fields: [
        { key: 'REGION_CODE', sourceColumn: 'REGION_CODE' },
        { key: 'MARKET_CODE', sourceColumn: 'MARKET_CODE' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('record', ['REGION_CODE', 'MARKET_CODE'], [
      { REGION_CODE: 'R01', MARKET_CODE: 'M01' },
      { REGION_CODE: 'R02', MARKET_CODE: 'M02' },
    ]),
    dataTable('entity_lookup', ['REGION_CODE', 'MARKET_CODE', 'REGION_NAME', 'MARKET_NAME'], [
      { REGION_CODE: 'R01', MARKET_CODE: 'M01', REGION_NAME: '区域一', MARKET_NAME: '市场一' },
      { REGION_CODE: 'R02', MARKET_CODE: 'M02', REGION_NAME: '区域二', MARKET_NAME: '市场二' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
});

test('semantic binding：业务对象表不作为术语绑定资源表', () => {
  const spec: PackageSpec = {
    id: 'movement-demo', name: 'movement demo', description: 'movement demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'movement_record', title: '流转记录', description: '流转记录',
      sourceTable: 'movement_record',
      fields: [
        { key: 'SOURCE_CODE', sourceColumn: 'SOURCE_CODE' },
        { key: 'TARGET_CODE', sourceColumn: 'TARGET_CODE' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('movement_record', ['SOURCE_CODE', 'TARGET_CODE'], [
      { SOURCE_CODE: 'S01', TARGET_CODE: 'T01' },
      { SOURCE_CODE: 'S02', TARGET_CODE: 'T02' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
});

test('semantic binding：普通业务表仅字段命中术语特征时不推荐 binding', () => {
  const spec: PackageSpec = {
    id: 'business-demo', name: 'business demo', description: 'business demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'business_record', title: '业务记录', description: '业务记录',
      sourceTable: 'business_record',
      fields: [
        { key: 'CUSTOMER_ID', sourceColumn: 'CUSTOMER_ID' },
        { key: 'CUSTOMER_NAME', sourceColumn: 'CUSTOMER_NAME' },
        { key: 'PROJECT_CODE', sourceColumn: 'PROJECT_CODE' },
        { key: 'PROJECT_NAME', sourceColumn: 'PROJECT_NAME' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('business_record', ['CUSTOMER_ID', 'CUSTOMER_NAME', 'PROJECT_CODE', 'PROJECT_NAME'], [
      { CUSTOMER_ID: 'C001', CUSTOMER_NAME: '客户一', PROJECT_CODE: 'P001', PROJECT_NAME: '项目一' },
      { CUSTOMER_ID: 'C002', CUSTOMER_NAME: '客户二', PROJECT_CODE: 'P002', PROJECT_NAME: '项目二' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
});

test('semantic binding：英文术语特征必须按完整 token 命中', () => {
  const spec: PackageSpec = {
    id: 'equipment-demo', name: 'equipment demo', description: 'equipment demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'gas_drive_end_comp', title: '驱动端组件', description: '驱动端组件',
      sourceTable: 'gas_drive_end_comp',
      fields: [
        { key: 'end_id', sourceColumn: 'end_id' },
        { key: 'end_name', sourceColumn: 'end_name' },
        { key: 'system_id', sourceColumn: 'system_id' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('gas_drive_end_comp', ['end_id', 'end_name', 'system_id'], [
      { end_id: 'E01', end_name: '驱动端', system_id: 'S01' },
      { end_id: 'E02', end_name: '非驱动端', system_id: 'S01' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
});

test('semantic binding：code/name 字段缺少一致前缀时不推荐 binding', () => {
  const spec: PackageSpec = {
    id: 'lookup-demo', name: 'lookup demo', description: 'lookup demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'business_record', title: '业务记录', description: '业务记录',
      sourceTable: 'business_record',
      fields: [{ key: 'CODE', sourceColumn: 'CODE' }],
    }],
    operations: [],
  };
  const tables = [
    dataTable('status_lookup', ['CODE', 'NAME'], [
      { CODE: 'A', NAME: '启用' },
      { CODE: 'I', NAME: '停用' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
});

test('semantic binding：已确认的同等 binding 不再重复推荐字段关联', () => {
  const spec: PackageSpec = {
    id: 'airport-demo', name: 'airport demo', description: 'airport demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    valueBindings: [
      { key: 'airport-name', providerAlias: 'data', resource: '机场中文', valueField: 'ARP_CD', semanticField: '机场名' },
      { key: 'airport-name-2', providerAlias: 'data', resource: '机场中文', valueField: 'ARP_CD', semanticField: '机场名' },
    ],
    objects: [{
      key: 'airport', title: '机场能力', description: '机场能力',
      sourceTable: 'airport',
      fields: [{ key: 'ARP_CD', sourceColumn: 'ARP_CD' }],
    }],
    operations: [],
  };
  const tables = [
    dataTable('airport', ['ARP_CD'], [
      { ARP_CD: 'CAN' },
      { ARP_CD: 'CGO' },
    ]),
    dataTable('机场中文', ['ARP_CD', '机场名'], [
      { ARP_CD: 'CAN', 机场名: '广州白云国际机场' },
      { ARP_CD: 'CGO', 机场名: '郑州新郑国际机场' },
    ]),
  ];

  assert.deepEqual(suggestSemanticBindings(spec, tables), []);
  assert.deepEqual(
    suggestSemanticFieldRefs(spec, tables).map((ref) => `${ref.objectKey}.${ref.fieldKey}->${ref.bindingKey}`),
    ['airport.ARP_CD->airport-name'],
  );
});

test('semantic binding：批量确认后写入 value_bindings 和字段引用', () => {
  const spec: PackageSpec = {
    id: 'airport-demo', name: 'airport demo', description: 'airport demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'airport', title: '机场能力', description: '机场能力',
      sourceTable: 'airport', primaryKey: ['ARP_CD'],
      fields: [
        { key: 'ARP_CD', sourceColumn: 'ARP_CD' },
        { key: 'ACTYPE_SALE', sourceColumn: 'ACTYPE_SALE' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('airport', ['ARP_CD', 'ACTYPE_SALE'], [
      { ARP_CD: 'CAN', ACTYPE_SALE: '789' },
      { ARP_CD: 'CGO', ACTYPE_SALE: '789' },
    ]),
    dataTable('机场中文代号', ['ARP_CD', '机场名'], [
      { ARP_CD: 'CAN', 机场名: '广州白云国际机场' },
      { ARP_CD: 'CGO', 机场名: '郑州新郑国际机场' },
    ], 'term'),
  ];
  const suggestion = suggestSemanticBindings(spec, tables)[0];
  const bindingApplied = applySemanticBindingSelection(spec, tables, {
    bindingKeys: [suggestion.key],
    fieldRefs: [],
  });
  const fieldRefs = suggestSemanticFieldRefs(bindingApplied.spec, tables);
  const applied = applySemanticBindingSelection(bindingApplied.spec, tables, {
    bindingKeys: [],
    fieldRefs: fieldRefs.map((ref) => ({
      objectKey: ref.objectKey,
      fieldKey: ref.fieldKey,
      bindingKey: ref.bindingKey,
    })),
  });
  const files = renderPackage(applied.spec);
  const mappings = JSON.parse(files['mappings/object-data-mappings.json']);

  assert.equal(mappings.schema_version, '0.1');
  assert.equal(applied.spec.valueBindings?.[0]?.key, suggestion.key);
  assert.equal(applied.spec.objects[0].fields[0].valueBindingRef, suggestion.key);
  assert.equal(mappings.value_bindings[suggestion.key].resource, '机场中文代号');
  assert.equal(mappings.objects.airport.fields.ARP_CD.value_binding_ref, suggestion.key);
});

test('semantic binding：清空已确认 binding 时同步清除字段引用', () => {
  const spec: PackageSpec = {
    id: 'term-clear-demo', name: 'term clear demo', description: 'term clear demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    valueBindings: [
      { key: 'item-code', providerAlias: 'data', resource: 'item_terms', valueField: 'ITEM_CODE', semanticField: 'ITEM_NAME' },
    ],
    objects: [{
      key: 'item', title: '物料', description: '物料',
      sourceTable: 'item', primaryKey: ['ITEM_CODE'],
      fields: [
        { key: 'ITEM_CODE', sourceColumn: 'ITEM_CODE', valueBindingRef: 'item-code' },
        { key: 'CATEGORY_CODE', sourceColumn: 'CATEGORY_CODE' },
      ],
    }],
    operations: [],
  };

  const cleared = clearSemanticBindings(spec);
  const mappings = JSON.parse(renderPackage(cleared.spec)['mappings/object-data-mappings.json']);

  assert.equal(cleared.clearedBindings, 1);
  assert.equal(cleared.clearedFieldRefs, 1);
  assert.equal(cleared.spec.valueBindings, undefined);
  assert.equal(cleared.spec.objects[0].fields[0].valueBindingRef, undefined);
  assert.equal(mappings.value_bindings, undefined);
  assert.equal(mappings.objects.item.fields.ITEM_CODE.value_binding_ref, undefined);
});
