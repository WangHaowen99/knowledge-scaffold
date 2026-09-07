import assert from 'node:assert/strict';
import test from 'node:test';
import type { DataDictTable } from '../src/types.js';
import {
  buildPhysicalSchemaObjects,
  detectSchemaManifests,
  preservePhysicalSchemaSemantics,
} from '../src/workflow/schema-manifest.js';

function table(name: string, fields: DataDictTable['fields'], rows: string[][], documentId = 'doc-workbook'):
  DataDictTable {
  return { name, fields, rows, rowCount: rows.length, documentId };
}

const manifestFields = [
  { name: 'SheetName', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
  { name: 'SheetType', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
  { name: 'DeviceID', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
  { name: 'DeviceName', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
  { name: 'Description', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
];

test('通过结构特征识别 Schema Manifest，不依赖固定名称', () => {
  const tables = [
    table('目录', manifestFields, [
      ['装置表', 'Device', 'd1', '一号装置', '装置定义'],
      ['产品表', 'Product', 'd1', '一号产品', '产品定义'],
      ['Energy', 'Energy', '', '能源', '能源定义'],
    ]),
    table('装置表', [{ name: 'DeviceID', inferredType: 'string', sampleValues: ['d1'], distinctRatio: 1 }], [['d1']]),
    table('产品表', [{ name: 'ProductID', inferredType: 'string', sampleValues: ['p1'], distinctRatio: 1 }], [['p1']]),
    table('Energy', [{ name: 'EnergyID', inferredType: 'string', sampleValues: ['e1'], distinctRatio: 1 }], [['e1']]),
  ];
  const manifests = detectSchemaManifests(tables);
  assert.equal(manifests.length, 1);
  assert.equal(manifests[0].table.name, '目录');
  assert.equal(manifests[0].entries.length, 3);
});

test('表名和标识列使用通用中文列名时也能识别 Manifest', () => {
  const fields = [
    { name: '表名', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
    { name: '类型', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
    { name: '标识', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
    { name: '描述', inferredType: 'string' as const, sampleValues: [], distinctRatio: 1 },
  ];
  const tables = [
    table('元数据', fields, [
      ['客户', 'master', 'customer', '客户主数据'],
      ['订单', 'event', 'order', '订单事件'],
      ['产品', 'master', 'product', '产品主数据'],
    ]),
    table('客户', [{ name: 'id', inferredType: 'string', sampleValues: ['C1'], distinctRatio: 1 }], [['C1']]),
    table('订单', [{ name: 'id', inferredType: 'string', sampleValues: ['O1'], distinctRatio: 1 }], [['O1']]),
    table('产品', [{ name: 'id', inferredType: 'string', sampleValues: ['P1'], distinctRatio: 1 }], [['P1']]),
  ];

  assert.equal(detectSchemaManifests(tables).length, 1);
  assert.deepEqual(buildPhysicalSchemaObjects(tables).map((item) => item.object.key), [
    '元数据', 'customer', 'order', 'product',
  ]);
});

test('Manifest Sheet 本身也属于确定 Schema，每个 Sheet 都形成一个物理对象', () => {
  const tables = [
    table('目录', manifestFields, [
      ['设备A', 'Device', 'd1', '设备A', '设备'],
      ['设备B', 'Device', 'd2', '设备B', '设备'],
      ['空表', 'Product', 'p1', '空表', '暂无数据'],
    ]),
    table('设备A', [{ name: 'DeviceID', inferredType: 'string', sampleValues: ['d1'], distinctRatio: 1 }], [['d1']]),
    table('设备B', [{ name: 'DeviceID', inferredType: 'string', sampleValues: ['d2'], distinctRatio: 1 }], [['d2']]),
    table('空表', [{ name: 'ProductID', inferredType: 'string', sampleValues: [], distinctRatio: 0 }], []),
  ];
  const objects = buildPhysicalSchemaObjects(tables);
  assert.deepEqual(objects.map((item) => item.table.name), ['目录', '设备A', '设备B', '空表']);
  assert.deepEqual(objects.map((item) => item.object.key), ['目录', 'd1', 'd2', 'p1']);
  assert.deepEqual(objects.map((item) => item.object.sourceTable), ['目录', '设备A', '设备B', '空表']);
  assert.equal(objects[1].object.fields[0].sourceColumn, 'DeviceID');
  assert.equal(objects[3].object.fields[0].sourceColumn, 'ProductID');
});

test('没有 Manifest 的普通多 Sheet 工作簿仍按一表一对象', () => {
  const tables = [
    table('A', [{ name: 'id', inferredType: 'string', sampleValues: ['1'], distinctRatio: 1 }], [['1']]),
    table('B', [{ name: 'code', inferredType: 'string', sampleValues: ['x'], distinctRatio: 1 }], [['x']]),
  ];
  assert.equal(detectSchemaManifests(tables).length, 0);
  assert.equal(buildPhysicalSchemaObjects(tables).length, 2);
});

test('存在数据库约束来源时只有明确约束产生必填，推断主键不参与回退', () => {
  const fields: DataDictTable['fields'] = [
    { name: 'explicit_required', required: true, inferredType: 'string', sampleValues: [], distinctRatio: 0 },
    { name: 'explicit_nullable', nullable: true, inferredType: 'string', sampleValues: [], distinctRatio: 0 },
    { name: 'inferred_primary_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 },
  ];
  const object = buildPhysicalSchemaObjects([
    table('record_sheet', fields, [['', '', 'record_0001']]),
  ])[0].object;

  assert.deepEqual(object.fields.map((field) => field.required), [true, false, false]);
  assert.deepEqual(object.primaryKey, ['inferred_primary_id']);
});

test('数据库和数据字典均不存在时推断主键产生必填结论', () => {
  const physical = buildPhysicalSchemaObjects([
    table('record_sheet', [
      { name: 'record_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 },
      { name: 'status_code', inferredType: 'string', sampleValues: ['enabled'], distinctRatio: 1 },
    ], [['record_0001', 'enabled']]),
  ]);
  const object = physical[0].object;

  assert.deepEqual(object.primaryKey, ['record_id']);
  assert.deepEqual(object.fields.map((field) => field.required), [true, false]);
  const previous = structuredClone(object);
  previous.fields = previous.fields.map((field) => ({ ...field, required: false }));
  const resynced = preservePhysicalSchemaSemantics(physical, [previous])[0].object;
  assert.deepEqual(resynced.fields.map((field) => field.required), [true, false]);
  previous.fields[1].required = true;
  const preservesUserDecision = preservePhysicalSchemaSemantics(physical, [previous])[0].object;
  assert.deepEqual(preservesUserDecision.fields.map((field) => field.required), [true, true]);
});

test('无约束来源时唯一率可支撑主键必填，任意首列弱兜底不能', () => {
  const uniqueObject = buildPhysicalSchemaObjects([
    table('unique_record', [
      { name: 'description', inferredType: 'string', sampleValues: ['alpha', 'beta'], distinctRatio: 1 },
      { name: 'category', inferredType: 'string', sampleValues: ['same'], distinctRatio: 0.5 },
    ], [['alpha', 'same'], ['beta', 'same']]),
  ])[0].object;
  assert.deepEqual(uniqueObject.primaryKey, ['description']);
  assert.deepEqual(uniqueObject.fields.map((field) => field.required), [true, false]);

  const weakFallback = buildPhysicalSchemaObjects([
    table('non_unique_record', [
      { name: 'description', inferredType: 'string', sampleValues: ['same'], distinctRatio: 0.5 },
      { name: 'category', inferredType: 'string', sampleValues: ['same'], distinctRatio: 0.5 },
    ], [['same', 'same'], ['same', 'same']]),
  ])[0].object;
  assert.deepEqual(weakFallback.primaryKey, ['description']);
  assert.deepEqual(weakFallback.fields.map((field) => field.required), [false, false]);
});

test('数据库明确声明的复合主键全部直接视为必填', () => {
  const object = buildPhysicalSchemaObjects([
    {
      ...table('record_sheet', [
        { name: 'tenant_id', nullable: true, inferredType: 'string', sampleValues: [], distinctRatio: 0 },
        { name: 'record_id', required: false, inferredType: 'string', sampleValues: [], distinctRatio: 0 },
        { name: 'status_code', primaryKey: false, inferredType: 'string', sampleValues: ['enabled'], distinctRatio: 1 },
      ], []),
      constraintSource: 'database',
      primaryKey: ['tenant_id', 'record_id'],
    },
  ])[0].object;

  assert.deepEqual(object.primaryKey, ['tenant_id', 'record_id']);
  assert.deepEqual(object.fields.map((field) => field.required), [true, true, false]);
});

test('最小数据字典可以明确声明主键并关闭样例主键必填回退', () => {
  const physical: DataDictTable = {
    ...table('record_sheet', [
      { name: 'record_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 },
      { name: 'alternate_id', inferredType: 'string', sampleValues: ['alternate_0001'], distinctRatio: 1 },
    ], [['record_0001', 'alternate_0001']]),
    materialPurpose: 'schema',
  };
  const dictionary: DataDictTable = {
    ...table('constraint_dictionary', [
      { name: 'sheet_name', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'column_name', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'primary_key', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
    ], [
      ['record_sheet', 'record_id', 'true'],
      ['record_sheet', 'alternate_id', 'false'],
    ]),
    materialPurpose: 'evidence',
  };

  const object = buildPhysicalSchemaObjects([physical], [physical, dictionary])[0].object;
  assert.deepEqual(object.primaryKey, ['record_id']);
  assert.deepEqual(object.fields.map((field) => field.required), [true, false]);

  dictionary.rows = [
    ['record_sheet', 'record_id', '1'],
    ['record_sheet', 'alternate_id', '2'],
  ];
  const composite = buildPhysicalSchemaObjects([physical], [physical, dictionary])[0].object;
  assert.deepEqual(composite.primaryKey, ['record_id', 'alternate_id']);
  assert.deepEqual(composite.fields.map((field) => field.required), [true, true]);

  dictionary.rows = [
    ['record_sheet', 'record_id', 'false'],
    ['record_sheet', 'alternate_id', 'false'],
  ];
  const inferredButNotRequired = buildPhysicalSchemaObjects([physical], [physical, dictionary])[0].object;
  assert.deepEqual(inferredButNotRequired.primaryKey, ['record_id']);
  assert.deepEqual(inferredButNotRequired.fields.map((field) => field.required), [false, false]);
  const previousFallback = buildPhysicalSchemaObjects([physical], [physical])[0].object;
  assert.equal(previousFallback.fields[0].required, true);
  const resynced = preservePhysicalSchemaSemantics(
    buildPhysicalSchemaObjects([physical], [physical, dictionary]),
    [previousFallback],
  )[0].object;
  assert.deepEqual(resynced.fields.map((field) => field.required), [false, false]);
});

test('79 行 Manifest 对应的 Sheet 全部生成物理对象，不触发逐对象模型调用', () => {
  const rows = Array.from({ length: 79 }, (_, index) => [
    `设备_${index + 1}`, 'Device', `device_${index + 1}`, `设备 ${index + 1}`, `第 ${index + 1} 个设备定义`,
  ]);
  const tables = [
    table('设备目录', manifestFields, rows),
    ...rows.map(([sheetName], index) => table(
      sheetName,
      [{ name: 'device_id', inferredType: 'string', sampleValues: [`D${index + 1}`], distinctRatio: 1 }],
      [[`D${index + 1}`]],
    )),
  ];

  const objects = buildPhysicalSchemaObjects(tables);
  assert.equal(objects.length, 80);
  assert.equal(objects[0].object.key, '设备目录');
  assert.equal(objects[1].object.key, 'device_1');
  assert.equal(objects[79].object.key, 'device_79');
  assert.ok(objects.every(({ object }) => object.sourceRefs?.length === 1));
});

test('显式数据字典按精确 Sheet 和列名确定性预填业务语义与必填性', () => {
  const physical: DataDictTable = {
    ...table('record_sheet', [
      { name: 'record_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1 },
      { name: 'status_code', inferredType: 'string', sampleValues: ['enabled'], distinctRatio: 1 },
    ], [['record_0001', 'enabled']]),
    materialPurpose: 'schema',
  };
  const dictionary: DataDictTable = {
    ...table('field_dictionary', [
      { name: 'sheet_name', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'column_name', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'business_name', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'description', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'aliases', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
      { name: 'required', inferredType: 'string', sampleValues: [], distinctRatio: 1 },
    ], [
      ['record_sheet', '', '业务记录', '保存合成业务处理记录。', '业务档案', ''],
      ['record_sheet', 'record_id', '记录编号', '记录的稳定业务标识。', '记录标识', 'true'],
      ['record_sheet', 'status_code', '状态代码', '记录当前所处的业务状态。', '', 'false'],
    ]),
    materialPurpose: 'evidence',
  };

  const objects = buildPhysicalSchemaObjects([physical], [physical, dictionary]);
  assert.equal(objects.length, 1);
  assert.equal(objects[0].object.title, '业务记录');
  assert.equal(objects[0].object.description, '保存合成业务处理记录。');
  assert.deepEqual(objects[0].object.aliases, ['业务档案']);
  assert.deepEqual(objects[0].object.fields, [{
    key: 'record_id', cnName: '记录编号', type: 'string', sourceColumn: 'record_id',
    note: '记录的稳定业务标识。', alias: '记录标识', required: true,
  }, {
    key: 'status_code', cnName: '状态代码', type: 'string', sourceColumn: 'status_code',
    note: '记录当前所处的业务状态。', required: false,
  }]);

  const previous = structuredClone(objects[0].object);
  previous.title = '旧记录名称';
  previous.description = '旧记录说明。';
  previous.aliases = ['旧别名'];
  previous.fields = previous.fields.map((field) => ({
    ...field,
    cnName: '旧字段名称',
    note: '旧字段说明。',
    alias: '旧字段别名',
    required: !field.required,
  }));
  const merged = preservePhysicalSchemaSemantics(objects, [previous])[0].object;
  assert.equal(merged.title, '业务记录');
  assert.equal(merged.description, '保存合成业务处理记录。');
  assert.deepEqual(merged.aliases, ['业务档案']);
  assert.equal(merged.fields[0].cnName, '记录编号');
  assert.equal(merged.fields[0].note, '记录的稳定业务标识。');
  assert.equal(merged.fields[0].alias, '记录标识');
  assert.equal(merged.fields[0].required, true);
  assert.equal(merged.fields[1].required, false);
});
