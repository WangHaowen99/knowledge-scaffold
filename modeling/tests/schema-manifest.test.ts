import assert from 'node:assert/strict';
import test from 'node:test';
import type { DataDictTable } from '../src/types.js';
import { buildPhysicalSchemaObjects, detectSchemaManifests } from '../src/workflow/schema-manifest.js';

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
