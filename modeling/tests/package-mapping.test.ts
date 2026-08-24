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
import { suggestSemanticBindings, suggestSemanticFieldRefs } from '../src/workflow/semantic-binding.js';
import {
  applySemanticBindingSelection,
  previewSemanticBindingCandidates,
} from '../src/workflow/semantic-binding-service.js';
import type { DataDictTable, PackageSpec } from '../src/types.js';

function dataTable(name: string, columns: string[], rows: Record<string, string>[] = []): DataDictTable {
  return {
    name,
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

test('package mapping：多来源汇总对象不能伪装成可直接绑定的单个物理对象', () => {
  const spec = packageSpec();
  spec.objects[0] = {
    ...spec.objects[0],
    sourceTable: undefined,
    sourceRefs: [
      { kind: 'table', table: 'TenderEvents' },
      { kind: 'table', table: 'ParkEvents' },
    ],
    primaryKey: ['event_uid'],
    fields: [
      { key: 'event_uid', cnName: '事件标识', sourceColumn: 'event_id', type: 'string', required: true },
      { key: 'customer_name', cnName: '企业名称', sourceColumn: 'company_name', type: 'string' },
    ],
  };

  const problems = findObjectDataMappingProblems(spec, [
    dataTable('TenderEvents', ['event_id', 'company_name']),
    dataTable('ParkEvents', ['event_id', 'company_name']),
    dataTable('DataSheet', ['ID', 'NAME', 'CODE']),
  ]).filter((problem) => problem.objectKey === 'DataObject');

  assert.equal(problems.length, 1);
  assert.match(problems[0].message, /同时绑定了 2 张来源表/);
  assert.match(problems[0].message, /当前数据绑定一次只能执行一张物理表/);
  assert.match(problems[0].message, /逐来源明细对象/);
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

test('semantic binding：知识结构抽取出对象字段后先推荐 binding，再推荐字段关联', () => {
  const spec: PackageSpec = {
    id: 'airport-demo', name: 'airport demo', description: 'airport demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [
      {
        key: 'flight', title: '航班', description: '航班',
        sourceTable: 'flight', fields: [
          { key: 'LATEST_DEP_ARP_CD', sourceColumn: 'LATEST_DEP_ARP_CD' },
          { key: 'LATEST_ARV_ARP_CD', sourceColumn: 'LATEST_ARV_ARP_CD' },
        ],
      },
      {
        key: 'airport', title: '机场能力', description: '机场能力',
        sourceTable: 'airport', fields: [
          { key: 'ARP_CD', sourceColumn: 'ARP_CD' },
          { key: 'ACTYPE_SALE', sourceColumn: 'ACTYPE_SALE' },
        ],
      },
    ],
    operations: [],
  };
  const tables = [
    dataTable('flight', ['LATEST_DEP_ARP_CD', 'LATEST_ARV_ARP_CD'], [
      { LATEST_DEP_ARP_CD: 'CAN', LATEST_ARV_ARP_CD: 'CGO' },
      { LATEST_DEP_ARP_CD: 'LUX', LATEST_ARV_ARP_CD: 'CAN' },
    ]),
    dataTable('airport', ['ARP_CD', 'ACTYPE_SALE'], [
      { ARP_CD: 'CAN', ACTYPE_SALE: '789' },
      { ARP_CD: 'CGO', ACTYPE_SALE: '789' },
      { ARP_CD: 'LUX', ACTYPE_SALE: '789' },
    ]),
    dataTable('机场中文代号', ['ARP_CD', '机场名', '英文名'], [
      { ARP_CD: 'CAN', 机场名: '广州白云国际机场', 英文名: 'Guangzhou Baiyun' },
      { ARP_CD: 'CGO', 机场名: '郑州新郑国际机场', 英文名: 'Zhengzhou Xinzheng' },
      { ARP_CD: 'LUX', 机场名: '卢森堡芬德尔机场', 英文名: 'Luxembourg Findel' },
    ]),
  ];

  const suggestions = suggestSemanticBindings(spec, tables);
  const airport = suggestions.find((item) => item.resource === '机场中文代号' && item.valueField === 'ARP_CD');

  assert.ok(airport);
  assert.equal(airport.semanticField, '机场名');
  assert.deepEqual(airport.aliasFields, []);
  assert.deepEqual(airport.candidateFieldRefs, []);

  const withBinding = applySemanticBindingSelection(spec, tables, {
    bindingKeys: [airport.key],
    fieldRefs: [],
  }).spec;
  const fieldRefs = suggestSemanticFieldRefs(withBinding, tables);
  const expectedFieldRefs = ['airport.ARP_CD', 'flight.LATEST_ARV_ARP_CD', 'flight.LATEST_DEP_ARP_CD']
    .map((ref) => `${ref}->${airport.key}`)
    .sort();
  assert.deepEqual(
    fieldRefs.map((ref) => `${ref.objectKey}.${ref.fieldKey}->${ref.bindingKey}`).sort(),
    expectedFieldRefs,
  );
});

test('semantic binding：知识结构尚未抽取对象字段时不推荐 binding', () => {
  const spec: PackageSpec = {
    id: 'airport-demo', name: 'airport demo', description: 'airport demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
  const tables = [
    dataTable('机场中文代号', ['ARP_CD', '机场名'], [
      { ARP_CD: 'CAN', 机场名: '广州白云国际机场' },
      { ARP_CD: 'CGO', 机场名: '郑州新郑国际机场' },
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
      key: 'airport', title: '机场能力', description: '机场能力',
      sourceTable: 'airport',
      fields: [{ key: 'ARP_CD', sourceColumn: 'ARP_CD' }],
    }],
  }, tables);
  assert.equal(modeledPreview.bindingCount, 1);
  assert.equal(modeledPreview.suggestions[0]?.resource, '机场中文代号');
});

test('semantic binding：同一资源表多组 code/name 按字段语义配对', () => {
  const spec: PackageSpec = {
    id: 'airport-demo', name: 'airport demo', description: 'airport demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'airport', title: '机场能力', description: '机场能力',
      sourceTable: 'airport', fields: [
        { key: 'ARP_CD', sourceColumn: 'ARP_CD' },
        { key: 'COUNTRY_CODE', sourceColumn: 'COUNTRY_CODE' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('airport', ['ARP_CD', 'COUNTRY_CODE'], [
      { ARP_CD: 'CAN', COUNTRY_CODE: 'CN' },
      { ARP_CD: 'LUX', COUNTRY_CODE: 'LU' },
    ]),
    dataTable('机场中文', ['ARP_CD', 'COUNTRY_REGION_CODE', '国家', '机场名'], [
      { ARP_CD: 'CAN', COUNTRY_REGION_CODE: 'CN', 国家: '中国', 机场名: '广州白云国际机场' },
      { ARP_CD: 'LUX', COUNTRY_REGION_CODE: 'LU', 国家: '卢森堡', 机场名: '卢森堡芬德尔机场' },
    ]),
  ];

  const suggestions = suggestSemanticBindings(spec, tables);
  const pairs = suggestions.map((item) => `${item.valueField}->${item.semanticField}`).sort();

  assert.deepEqual(pairs, ['ARP_CD->机场名', 'COUNTRY_REGION_CODE->国家']);
  assert.deepEqual(suggestions.flatMap((item) => item.candidateFieldRefs), []);

  const withBindings = applySemanticBindingSelection(spec, tables, {
    bindingKeys: suggestions.map((item) => item.key),
    fieldRefs: [],
  }).spec;
  const fieldRefs = suggestSemanticFieldRefs(withBindings, tables);

  assert.ok(fieldRefs.some((ref) => ref.objectKey === 'airport' && ref.fieldKey === 'ARP_CD'
    && ref.bindingKey === suggestions.find((item) => item.valueField === 'ARP_CD')?.key));
  assert.ok(fieldRefs.some((ref) => ref.objectKey === 'airport' && ref.fieldKey === 'COUNTRY_CODE'
    && ref.bindingKey === suggestions.find((item) => item.valueField === 'COUNTRY_REGION_CODE')?.key));
});

test('semantic binding：业务对象表不作为术语绑定资源表', () => {
  const spec: PackageSpec = {
    id: 'route-demo', name: 'route demo', description: 'route demo',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'flight_route', title: '航线', description: '航线',
      sourceTable: 'flight_route',
      fields: [
        { key: 'DEP_ARP_CD', sourceColumn: 'DEP_ARP_CD' },
        { key: 'ARV_ARP_CD', sourceColumn: 'ARV_ARP_CD' },
      ],
    }],
    operations: [],
  };
  const tables = [
    dataTable('flight_route', ['DEP_ARP_CD', 'ARV_ARP_CD'], [
      { DEP_ARP_CD: 'CAN', ARV_ARP_CD: 'CGO' },
      { DEP_ARP_CD: 'CGO', ARV_ARP_CD: 'LUX' },
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
    ]),
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
