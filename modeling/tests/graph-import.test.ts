import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { parseExcel } from '../src/documents/parser.js';
import { normalizeGraphImportMapping, validateGraphImportMapping } from '../src/graph-import/validator.js';
import { extractGraphImportSchema } from '../src/graph-import/package-schema.js';
import {
  GraphIsolationDataExistsError, inspectGraphIsolation, writeGraphImport,
} from '../src/graph-import/neo4j-writer.js';
import { renderPackage } from '../src/workflow/package-render.js';
import {
  createGraphImportProject, graphMapping, graphPackageSpec, graphSchema,
  neo4jConnection, recordingDriver, workbookBuffer,
} from './helpers/graph-import-fixtures.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-graph-import-'));

const importBuffer = () => workbookBuffer({
  Part: [
    ['part_id', 'relation_code', 'name'],
    ['P-001', 'R-1', '机翼'],
    ['P-002', 'NOT-FOUND', '机身'],
    ['P-003', '', '尾翼'],
  ],
  BOMItem: [['item_id', 'parent_code', 'name'], ['I-001', 'R-1', '机翼 BOM 行']],
});

const importPlan = () => validateGraphImportMapping(graphSchema, parseExcel(workbookBuffer({
    Part: [['part_id', 'relation_code', 'name'], ['P-001', 'R-1', '机翼']],
    BOMItem: [['item_id', 'parent_code', 'name'], ['I-001', 'R-1', '机翼 BOM 行']],
  }), { maxRows: 50_000, rejectOverflow: true }), graphMapping);

test('graph import Excel：保留字段注释和完整行，并拒绝静默截断', () => {
  const tables = parseExcel(workbookBuffer({
    Part: [
      ['part_id', 'name'],
      ['零件编号', '零件名称'],
      ['P-001', '机翼'],
      ['P-002', '机身'],
    ],
  }), { maxRows: 50_000, rejectOverflow: true });
  assert.equal(tables[0].rowCount, 2);
  assert.deepEqual(tables[0].fields.map((field) => field.note), ['零件编号', '零件名称']);
  assert.deepEqual(tables[0].rows, [['P-001', '机翼'], ['P-002', '机身']]);
  assert.throws(() => parseExcel(workbookBuffer({
    Part: [['id'], ['1'], ['2'], ['3']],
  }), { maxRows: 2, rejectOverflow: true }), /Part.*超过.*2/);
});

test('graph import mapping：计算节点、关系与空关联跳过数量', () => {
  const result = validateGraphImportMapping(graphSchema, parseExcel(workbookBuffer({
      Part: [
        ['part_id', 'relation_code', 'name'],
        ['P-001', 'R-1', '机翼'],
        ['P-002', '', '机身'],
      ],
      BOMItem: [['item_id', 'parent_code', 'name'], ['I-001', 'R-1', '机翼 BOM 行']],
    }), { maxRows: 50_000, rejectOverflow: true }), graphMapping);
  assert.deepEqual({
    nodes: result.nodeCount,
    relationships: result.relationshipCount,
    blanks: result.skippedBlankJoinCount,
  }, { nodes: 3, relationships: 1, blanks: 1 });
  assert.deepEqual(result.relations[0].pairs[0], {
    sourceRecordKey: 'node:Part:00000001', targetRecordKey: 'node:BOMItem:00000001',
  });
  assert.deepEqual(result.entities[0].rows[0].properties, {
    part_id: 'P-001', relation_code: 'R-1', name: '机翼',
  });
});

test('graph import mapping：无关系映射时仍校验并统计全部实体', () => {
  const result = validateGraphImportMapping(graphSchema, parseExcel(workbookBuffer({
    Part: [
      ['part_id', 'relation_code', 'name'],
      ['P-001', '', '机翼'],
      ['P-002', '', '机身'],
    ],
    BOMItem: [['item_id', 'parent_code', 'name'], ['I-001', '', '独立 BOM 行']],
  })), { relations: [] });

  assert.deepEqual({
    nodes: result.nodeCount,
    relationships: result.relationshipCount,
    entities: result.entities.map((entity) => entity.objectKey),
  }, {
    nodes: 3,
    relationships: 0,
    entities: ['Part', 'BOMItem'],
  });
});

test('graph import schema：支持多实体投影并以稳定 key 生成映射', () => {
  const wideSchema = {
    packageVersion: '1.0.0',
    objects: [
      {
        key: 'Customer', title: '客户', label: 'Customer', defaultSheet: 'Wide',
        propertyMappings: [
          { property: 'customerId', sourceColumn: 'customer_id' },
          { property: 'customerName', sourceColumn: 'customer_name' },
        ],
      },
      {
        key: 'Order', title: '订单', label: 'Order', defaultSheet: 'Wide',
        propertyMappings: [
          { property: 'customerId', sourceColumn: 'customer_id' },
          { property: 'orderId', sourceColumn: 'order_id' },
          { property: 'amount', sourceColumn: 'amount' },
          { property: 'optionalNote', sourceColumn: 'optional_note' },
        ],
      },
    ],
    relations: [],
  };
  const result = validateGraphImportMapping(wideSchema, parseExcel(workbookBuffer({
    Wide: [
      ['customer_id', 'customer_name', 'order_id', 'amount', 'unrelated'],
      ['C-1', 'Alice', 'O-1', '100', 'ignore-me'],
      ['C-1', 'Alice', 'O-2', '200', 'ignore-me-too'],
    ],
  }), { maxRows: 50_000, rejectOverflow: true }), {
    relations: [{
      sourceObject: 'Customer', relationName: '拥有订单', targetObject: 'Order',
      matchMode: 'field_match',
      sourceJoinProperty: 'customerId', targetJoinProperty: 'customerId',
    }],
  });
  assert.equal(result.nodeCount, 4);
  assert.equal(result.relationshipCount, 4);
  assert.equal(result.entities[0].rows.length, 2);
  assert.deepEqual(result.entities[0].rows.map((row) => row.recordKey), [
    'node:Customer:00000001',
    'node:Customer:00000002',
  ]);
  assert.deepEqual(result.entities[1].rows[0], {
    recordKey: 'node:Order:00000001',
    properties: { customerId: 'C-1', orderId: 'O-1', amount: '100' },
  });
  assert.deepEqual(result.relations[0].pairs, [
    { sourceRecordKey: 'node:Customer:00000001', targetRecordKey: 'node:Order:00000001' },
    { sourceRecordKey: 'node:Customer:00000001', targetRecordKey: 'node:Order:00000002' },
    { sourceRecordKey: 'node:Customer:00000002', targetRecordKey: 'node:Order:00000001' },
    { sourceRecordKey: 'node:Customer:00000002', targetRecordKey: 'node:Order:00000002' },
  ]);
  assert.deepEqual(result.warnings, [
    '实体「Order」属性 optionalNote 对应的 Excel 字段「optional_note」不存在，已忽略',
  ]);

  const spec = {
    ...graphPackageSpec,
    objects: graphPackageSpec.objects.map((object) => object.key === 'Part' ? {
      ...object,
      title: '设备零件',
      sourceTable: 'PartNode',
      relations: [{ relation: 'contains', target: 'BOMItem', note: '包含 BOM 行' }],
      fields: object.fields.map((field) => ({
        ...field,
        sourceColumn: field.key === 'part_id' ? 'PART_ID' : field.sourceColumn,
      })),
    } : { ...object, sourceTable: 'BOMItemNode' }),
  };
  const files = renderPackage(spec);
  files['objects/index.md'] = files['objects/index.md']
    .replace('| Part | 设备零件 | 零件 | PartNode |', '| Part | 设备零件 | 零件 | PartImport |')
    .replace('| BOMItem | BOM 行 | BOM 行 | BOMItemNode |', '| BOMItem | BOM 行 | BOM 行 | BOMImport |');
  const rawMapping = JSON.parse(files['mappings/object-data-mappings.json']);
  rawMapping.objects.Part.providers = { neo4j: { label: 'MappingLabelMustNotWin' } };
  files['mappings/object-data-mappings.json'] = JSON.stringify(rawMapping);

  const extracted = extractGraphImportSchema(spec, files);
  assert.deepEqual(extracted.objects.map((object) => ({
    key: object.key, label: object.label, sheet: object.defaultSheet,
  })), [
    { key: 'Part', label: 'Part', sheet: 'PartImport' },
    { key: 'BOMItem', label: 'BOMItem', sheet: 'BOMImport' },
  ]);
  assert.equal(extracted.objects[0].propertyMappings[0].sourceColumn, 'PART_ID');
  assert.deepEqual(extracted.relations, [{
    id: 'Part|contains|BOMItem|0', sourceObject: 'Part', relationName: 'contains',
    targetObject: 'BOMItem', note: '包含 BOM 行',
  }]);
  assert.throws(() => extractGraphImportSchema(spec, {
    ...files,
    'objects/index.md': files['objects/index.md'].replace(' | 来源Sheet |', ' |'),
  }), /objects\/index\.md 缺少 来源Sheet 列/);
});

test('graph import mapping：保留重复行并拒绝非法关联字段和缺失 Sheet', () => {
  const tables = parseExcel(workbookBuffer({
    Part: [
      ['part_id', 'relation_code', 'name', 'table_only'],
      ['P-001', 'NOT-FOUND', '零件', 'R-1'],
    ],
    BOMItem: [['item_id', 'parent_code', 'name'], ['I-001', 'R-1', 'BOM 行']],
  }), { maxRows: 50_000, rejectOverflow: true });
  const checks = [
    {
      label: '无匹配值脱敏告警',
      run: () => {
        const result = validateGraphImportMapping(graphSchema, tables, graphMapping);
        assert.equal(result.skippedUnmatchedJoinCount, 1);
        assert.deepEqual(result.warnings, [
          '关系「contains」有 1 行非空关联值未匹配目标实例，已跳过对应关系',
        ]);
        assert.doesNotMatch(result.warnings.join('；'), /NOT-FOUND/);
      },
    },
    {
      label: '重复业务键逐行实例化',
      run: () => {
        const result = validateGraphImportMapping(graphSchema, parseExcel(workbookBuffer({
          Part: [
            ['part_id', 'relation_code', 'name'],
            ['P-001', 'R-1', '机翼'],
            ['P-001', 'R-1', '机身'],
          ],
          BOMItem: [['item_id', 'parent_code'], ['I-001', 'R-1']],
        }), { maxRows: 50_000, rejectOverflow: true }), graphMapping);
        assert.equal(result.nodeCount, 3);
        assert.equal(result.relationshipCount, 2);
        assert.deepEqual(result.entities[0].rows, [
          {
            recordKey: 'node:Part:00000001',
            properties: { part_id: 'P-001', relation_code: 'R-1', name: '机翼' },
          },
          {
            recordKey: 'node:Part:00000002',
            properties: { part_id: 'P-001', relation_code: 'R-1', name: '机身' },
          },
        ]);
      },
    },
    {
      label: '非实体关联字段',
      run: () => assert.throws(() => validateGraphImportMapping(graphSchema, tables, {
        relations: [{ ...graphMapping.relations[0], sourceJoinProperty: 'table_only' }],
      }), /table_only.*不属于实体/),
    },
    {
      label: '跨 Sheet 不允许同行关联',
      run: () => assert.throws(() => validateGraphImportMapping(graphSchema, tables, {
        relations: [{
          sourceObject: 'Part', relationName: 'contains', targetObject: 'BOMItem',
          matchMode: 'same_row',
        }],
      }), /同一 Sheet/),
    },
    {
      label: '知识包 Sheet 缺失',
      run: () => assert.throws(() => validateGraphImportMapping({
        ...graphSchema,
        objects: graphSchema.objects.map((object) => object.key === 'Part'
          ? { ...object, defaultSheet: 'MissingPart' }
          : object),
      }, tables, graphMapping), /MissingPart/),
    },
  ];
  for (const check of checks) assert.doesNotThrow(check.run, check.label);
});

test('graph import mapping：同 Sheet 多实体按同行关联并跳过全空投影', () => {
  const schema = {
    packageVersion: '1.0.0',
    objects: [
      {
        key: 'Customer', title: '客户', label: 'Customer', defaultSheet: 'Wide',
        propertyMappings: [
          { property: 'customer_id', sourceColumn: 'customer_id' },
          { property: 'name', sourceColumn: 'name' },
        ],
      },
      {
        key: 'Order', title: '订单', label: 'Order', defaultSheet: 'Wide',
        propertyMappings: [
          { property: 'customer_id', sourceColumn: 'customer_id' },
          { property: 'order_id', sourceColumn: 'order_id' },
        ],
      },
    ],
    relations: [],
  };
  const result = validateGraphImportMapping(schema, parseExcel(workbookBuffer({
    Wide: [
      ['customer_id', 'name', 'order_id'],
      ['C-1', 'Alice', 'O-1'],
      ['C-1', 'Alice', 'O-1'],
      ['', '', 'O-2'],
      ['', 'Bob', ''],
    ],
  })), { relations: [{
    sourceObject: 'Customer', relationName: '下单', targetObject: 'Order',
    matchMode: 'same_row',
  }] });

  assert.equal(result.nodeCount, 6);
  assert.equal(result.relationshipCount, 2);
  assert.deepEqual(result.entities[0].rows.map((row) => row.recordKey), [
    'node:Customer:00000001',
    'node:Customer:00000002',
    'node:Customer:00000004',
  ]);
  assert.deepEqual(result.entities[1].rows.map((row) => row.recordKey), [
    'node:Order:00000001',
    'node:Order:00000002',
    'node:Order:00000003',
  ]);
  assert.deepEqual(result.entities[1].rows[0].properties, {
    customer_id: 'C-1', order_id: 'O-1',
  });
  assert.deepEqual(result.entities[0].rows[2].properties, {
    customer_id: '', name: 'Bob',
  });
  assert.deepEqual(result.relations[0].pairs, [
    { sourceRecordKey: 'node:Customer:00000001', targetRecordKey: 'node:Order:00000001' },
    { sourceRecordKey: 'node:Customer:00000002', targetRecordKey: 'node:Order:00000002' },
  ]);
});

test('graph import mapping：历史字段关联缺省模式保持兼容且同行模式移除隐藏字段', () => {
  const legacy = {
    relations: [{
      sourceObject: 'Part', relationName: 'contains', targetObject: 'BOMItem',
      sourceJoinProperty: 'relation_code', targetJoinProperty: 'parent_code',
    }],
  };
  assert.deepEqual(normalizeGraphImportMapping(legacy as any), graphMapping);
  assert.deepEqual(normalizeGraphImportMapping({
    relations: [{
      ...legacy.relations[0], matchMode: 'same_row',
    }],
  } as any), {
    relations: [{
      sourceObject: 'Part', relationName: 'contains', targetObject: 'BOMItem', matchMode: 'same_row',
    }],
  });
});

test('graph import upload：临时文件按项目和用户隔离并可立即删除', async () => {
  const { saveGraphImportUpload, getGraphImportUpload, deleteGraphImportUpload } = await import('../src/graph-import/upload-store.js');
  const project = await createGraphImportProject('上传隔离测试');
  const upload = saveGraphImportUpload({
    projectId: project.id,
    owner: 'alice',
    filename: 'graph.xlsx',
    buffer: workbookBuffer({ Part: [['id'], ['P-1']] }),
    packageRevisionOid: 'revision-upload-isolation',
  });
  assert.equal(existsSync(upload.storedPath), true);
  assert.equal(getGraphImportUpload({ uploadId: upload.id, projectId: project.id, owner: 'alice' }).id, upload.id);
  assert.throws(() => getGraphImportUpload({
    uploadId: upload.id, projectId: project.id, owner: 'bob',
  }), /不存在或已过期/);
  deleteGraphImportUpload({ uploadId: upload.id, projectId: project.id, owner: 'alice' });
  assert.equal(existsSync(upload.storedPath), false);
});

test('graph import upload：过期文件读取时自动清理', async () => {
  const { saveGraphImportUpload, getGraphImportUpload } = await import('../src/graph-import/upload-store.js');
  const project = await createGraphImportProject('上传过期测试');
  const upload = saveGraphImportUpload({
    projectId: project.id,
    owner: 'alice',
    filename: 'expired.xlsx',
    buffer: workbookBuffer({ Part: [['id'], ['P-1']] }),
    packageRevisionOid: 'revision-upload-expired',
    ttlMs: -1,
  });
  assert.throws(() => getGraphImportUpload({
    uploadId: upload.id, projectId: project.id, owner: 'alice',
  }), /不存在或已过期/);
  assert.equal(existsSync(upload.storedPath), false);
});

test('graph import repository：历史只保存映射和统计，不存在密码字段', async () => {
  const { createGraphImportRun, finishGraphImportRun, listGraphImportRuns } = await import('../src/graph-import/repo.js');
  const { getDb } = await import('../src/db/index.js');
  const project = await createGraphImportProject('历史脱敏测试');
  const run = createGraphImportRun({
    projectId: project.id,
    filename: 'graph.xlsx',
    fileHash: 'sha256',
    packageVersion: '1.0.0',
    packageRevisionOid: 'revision-history',
    mapping: graphMapping,
  });
  finishGraphImportRun(run.id, {
    status: 'done', nodeCount: 3, relationshipCount: 1, skippedCount: 1,
  });
  const stored = listGraphImportRuns(project.id)[0];
  assert.equal(stored.status, 'done');
  assert.equal(stored.nodeCount, 3);
  assert.equal(stored.packageRevisionOid, 'revision-history');
  const columns = getDb().prepare('PRAGMA table_info(graph_import_runs)').all() as Array<{ name: string }>;
  assert.equal(columns.some((column) => /password|secret/i.test(column.name)), false);
  const raw = getDb().prepare('SELECT mapping_json FROM graph_import_runs WHERE id = ?')
    .get(run.id) as { mapping_json: string };
  assert.equal(/password|secret/i.test(raw.mapping_json), false);
});

test('graph import repository：运行任务可查询且服务重启时标记失败', async () => {
  const {
    createGraphImportRun,
    failRunningGraphImportRuns,
    getGraphImportRun,
    getRunningGraphImportRun,
  } = await import('../src/graph-import/repo.js');
  const project = await createGraphImportProject('运行恢复测试');
  const run = createGraphImportRun({
    projectId: project.id,
    filename: 'background.xlsx',
    fileHash: 'background-sha256',
    packageVersion: '1.0.0',
    packageRevisionOid: 'revision-running',
    mapping: graphMapping,
  });
  assert.equal(getRunningGraphImportRun(project.id)?.id, run.id);
  assert.equal(failRunningGraphImportRuns('服务重启，后台导入已中断'), 1);
  assert.equal(getRunningGraphImportRun(project.id), null);
  assert.equal(getGraphImportRun(run.id)?.status, 'failed');
  assert.equal(getGraphImportRun(run.id)?.error, '服务重启，后台导入已中断');
  assert.ok(getGraphImportRun(run.id)?.finishedAt);
});

test('graph import Neo4j：后台任务启动前可快速预检隔离数据', async () => {
  const driver = recordingDriver(() => ({
    records: [{ get: (key: string) => (key === 'nodeCount' ? 12 : 34) }],
  }));
  const result = await inspectGraphIsolation(neo4jConnection(), driver.factory);
  assert.deepEqual(result, { nodeCount: 12, relationshipCount: 34 });
  assert.deepEqual(driver.state.databases, ['neo4j']);
  assert.equal(driver.calls.length, 1);
  assert.match(driver.calls[0].query, /count\(node\).*count\(relationship\)/s);
  assert.deepEqual(driver.calls[0].params, {
    workspaceValue: 'workspace-1', ontologyValue: 'ontology-1',
  });
  assert.equal(driver.state.sessionClosed, true);
  assert.equal(driver.state.driverClosed, true);
});

test('graph import Neo4j：节点和关系同事务幂等写入并带双隔离属性', async () => {
  const driver = recordingDriver();
  const result = await writeGraphImport(importPlan(),
    neo4jConnection({ password: 'super-secret', database: 'knowledge' }),
    { userName: 'alice' }, driver.factory);
  assert.deepEqual(result, { nodeCount: 2, relationshipCount: 1 });
  assert.deepEqual(driver.state.databases, ['knowledge']);
  assert.equal(driver.state.transactionCount, 1);
  assert.equal(driver.calls.length, 4);
  for (const call of driver.calls) {
    assert.match(call.query, /`d2o_workspace_id`/);
    assert.match(call.query, /`d2o_ontology_id`/);
    assert.equal(call.params.workspaceValue, 'workspace-1');
    assert.equal(call.params.ontologyValue, 'ontology-1');
    assert.doesNotMatch(`${call.query}${JSON.stringify(call.params)}`, /super-secret/);
  }
  assert.match(driver.calls[1].query, /MERGE \(node:`设备零件`/);
  assert.match(driver.calls[1].query, /`_d2o_record_key`: row\.recordKey/);
  assert.match(driver.calls[1].query, /node\.`d2o_user_id` = \$userName/);
  assert.equal(driver.calls[1].params.userName, 'alice');
  assert.deepEqual((driver.calls[1].params.rows as any[])[0], {
    recordKey: 'node:Part:00000001',
    properties: { part_id: 'P-001', relation_code: 'R-1', name: '机翼' },
  });
  assert.match(driver.calls[3].query, /MERGE \(source\)-\[relationship:`contains`/);
  assert.match(driver.calls[3].query, /`_d2o_record_key`: row\.sourceRecordKey/);
  assert.match(driver.calls[3].query, /`_d2o_record_key`: row\.targetRecordKey/);
  assert.equal(driver.state.sessionClosed, true);
  assert.equal(driver.state.driverClosed, true);
});

test('graph import Neo4j：校验、统计和写入包含未参与关系的实体', async () => {
  const schema = {
    ...graphSchema,
    objects: [...graphSchema.objects, {
      key: 'Standalone', title: '独立实体', label: 'Standalone', defaultSheet: 'Standalone',
      propertyMappings: [
        { property: 'standalone_id', sourceColumn: 'standalone_id' },
        { property: 'name', sourceColumn: 'name' },
      ],
    }],
  };
  const plan = validateGraphImportMapping(schema, parseExcel(workbookBuffer({
    Part: [['part_id', 'relation_code', 'name'], ['P-001', 'R-1', '机翼']],
    BOMItem: [['item_id', 'parent_code', 'name'], ['I-001', 'R-1', '机翼 BOM 行']],
    Standalone: [
      ['standalone_id', 'name'],
      ['S-001', '独立实例一'],
      ['S-002', '独立实例二'],
    ],
  })), graphMapping);

  assert.equal(plan.nodeCount, 4);
  assert.deepEqual(plan.entities.map((entity) => entity.objectKey), ['Part', 'BOMItem', 'Standalone']);

  const driver = recordingDriver();
  const result = await writeGraphImport(
    plan,
    neo4jConnection(),
    { userName: 'alice' },
    driver.factory,
  );
  assert.deepEqual(result, { nodeCount: 4, relationshipCount: 1 });
  const standaloneWrite = driver.calls.find((call) => /MERGE \(node:`Standalone`/.test(call.query));
  assert.ok(standaloneWrite);
  assert.deepEqual(standaloneWrite.params.rows, [
    {
      recordKey: 'node:Standalone:00000001',
      properties: { standalone_id: 'S-001', name: '独立实例一' },
    },
    {
      recordKey: 'node:Standalone:00000002',
      properties: { standalone_id: 'S-002', name: '独立实例二' },
    },
  ]);
});

test('graph import Neo4j：动态标识符非法时拒绝连接', async () => {
  let connected = false;
  await assert.rejects(() => writeGraphImport({
    nodeCount: 0,
    relationshipCount: 0,
    skippedBlankJoinCount: 0,
    skippedUnmatchedJoinCount: 0,
    warnings: [],
    entities: [],
    relations: [],
  }, neo4jConnection({ workspaceProperty: 'x`) DETACH DELETE n //' }), { userName: 'alice' }, () => {
    connected = true;
    throw new Error('不应连接');
  }), /Workspace 隔离字段.*格式非法/);
  assert.equal(connected, false);

  await assert.rejects(() => writeGraphImport({
    nodeCount: 0,
    relationshipCount: 0,
    skippedBlankJoinCount: 0,
    skippedUnmatchedJoinCount: 0,
    warnings: [],
    entities: [],
    relations: [],
  }, neo4jConnection({ workspaceProperty: '_d2o_record_key' }), { userName: 'alice' }, () => {
    connected = true;
    throw new Error('不应连接');
  }), /系统保留字段/);
  assert.equal(connected, false);
});

test('graph import Neo4j：事务失败时仍关闭 session 和 driver', async () => {
  const driver = recordingDriver(() => { throw new Error('write failed'); });
  await assert.rejects(() => writeGraphImport(importPlan(), neo4jConnection(),
    { userName: 'alice' }, driver.factory), /write failed/);
  assert.equal(driver.state.sessionClosed, true);
  assert.equal(driver.state.driverClosed, true);
});

test('graph import Neo4j：取消信号会关闭会话并中断当前事务', async () => {
  const controller = new AbortController();
  let notifyWriteStarted = () => {};
  const writeStarted = new Promise<void>((resolve) => { notifyWriteStarted = resolve; });
  let rejectActiveRun: ((error: Error) => void) | undefined;
  const state = { sessionClosed: false, driverClosed: false };
  let callCount = 0;
  const factory = async () => ({
    session() {
      return {
        async executeWrite(work: (transaction: any) => Promise<unknown>) {
          return work({
            async run() {
              callCount += 1;
              if (callCount === 1) {
                return { records: [{ get: () => 0 }] };
              }
              notifyWriteStarted();
              return new Promise((_resolve, reject) => { rejectActiveRun = reject; });
            },
          });
        },
        async close() {
          state.sessionClosed = true;
          rejectActiveRun?.(new Error('Neo4j 会话已关闭'));
        },
      };
    },
    async close() {
      state.driverClosed = true;
      rejectActiveRun?.(new Error('Neo4j Driver 已关闭'));
    },
  });

  const writing = writeGraphImport(importPlan(), neo4jConnection(), {
    userName: 'alice',
    signal: controller.signal,
  }, factory);
  await writeStarted;
  controller.abort(new Error('用户取消图数据导入'));

  await assert.rejects(writing, /已关闭|用户取消/);
  assert.equal(state.sessionClosed, true);
  assert.equal(state.driverClosed, true);
});

test('graph import Neo4j：已有双隔离数据时中止并要求显式确认', async () => {
  const driver = recordingDriver(() => ({
    records: [{ get: (key: string) => (key === 'nodeCount' ? 2 : 1) }],
  }));
  await assert.rejects(() => writeGraphImport(importPlan(), neo4jConnection(),
    { userName: 'alice' }, driver.factory), (error: any) => {
    assert.equal(error instanceof GraphIsolationDataExistsError, true);
    assert.equal(error.code, 'GRAPH_ISOLATION_DATA_EXISTS');
    assert.deepEqual(error.existing, { nodeCount: 2, relationshipCount: 1 });
    return true;
  });
  assert.equal(driver.calls.length, 1);
});

test('graph import Neo4j：确认覆盖后只清理完全匹配的双隔离数据', async () => {
  let callCount = 0;
  const driver = recordingDriver(() => {
    callCount += 1;
    return callCount === 1
      ? { records: [{ get: (key: string) => (key === 'nodeCount' ? 2 : 1) }] }
      : {};
  });
  await writeGraphImport(importPlan(), neo4jConnection(),
    { replaceExisting: true, userName: 'alice' }, driver.factory);
  assert.equal(driver.state.transactionCount, 1);
  assert.equal(driver.calls.length, 6);
  assert.match(driver.calls[1].query, /MATCH \(\)-\[relationship\]->\(\)/);
  assert.match(driver.calls[1].query, /DELETE relationship/);
  assert.match(driver.calls[2].query, /MATCH \(node\)/);
  assert.match(driver.calls[2].query, /DELETE node/);
  assert.doesNotMatch(driver.calls[2].query, /DETACH/);
  for (const call of driver.calls.slice(0, 3)) {
    assert.match(call.query, /`d2o_workspace_id`/);
    assert.match(call.query, /`d2o_ontology_id`/);
    assert.equal(call.params.workspaceValue, 'workspace-1');
    assert.equal(call.params.ontologyValue, 'ontology-1');
  }
});

test('graph import service：预览、校验、执行和审计形成脱敏闭环', async () => {
  const { uploadGraphImportExcel, validateGraphImportUpload, executeGraphImport, getGraphImportHistory } =
    await import('../src/graph-import/service.js');
  const { getGraphImportUpload } = await import('../src/graph-import/upload-store.js');
  const project = await createGraphImportProject('图导入服务测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'graph.xlsx', buffer: importBuffer(),
  });
  assert.deepEqual(preview.sheets.map((sheet) => sheet.name), ['Part', 'BOMItem']);
  assert.deepEqual(preview.sheets[0].columnTypes, {
    part_id: 'string', relation_code: 'string', name: 'string',
  });
  assert.equal('storedPath' in preview, false);
  const validated = await validateGraphImportUpload({
    projectId: project.id, owner: 'alice', uploadId: preview.id, mapping: graphMapping,
  });
  assert.deepEqual(validated.counts, {
    nodeCount: 4,
    relationshipCount: 1,
    skippedCount: 2,
    skippedBlankCount: 1,
    skippedUnmatchedCount: 1,
  });
  assert.doesNotMatch(validated.warnings.join('；'), /NOT-FOUND/);

  let receivedPassword = '';
  const scheduled: Array<() => Promise<void>> = [];
  const dependencies = {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
    schedule: (task: () => Promise<void>) => { scheduled.push(task); },
    writer: async (plan: any, connection: any, options: any) => {
      receivedPassword = connection.password;
      assert.deepEqual({
        workspaceProperty: connection.workspaceProperty,
        workspaceValue: connection.workspaceValue,
        ontologyProperty: connection.ontologyProperty,
        ontologyValue: connection.ontologyValue,
      }, {
        workspaceProperty: 'd2o_workspace_id',
        workspaceValue: 'workspace-001',
        ontologyProperty: 'd2o_ontology_id',
        ontologyValue: 'ontology-001',
      });
      assert.equal(options.signal instanceof AbortSignal, true);
      assert.deepEqual(
        { replaceExisting: options.replaceExisting, userName: options.userName },
        { replaceExisting: false, userName: 'alice' },
      );
      return { nodeCount: plan.nodeCount, relationshipCount: plan.relationshipCount };
    },
  };
  const started = await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'one-time-secret',
  }, dependencies);
  assert.equal(started.run.status, 'running');
  assert.equal(started.run.packageRevisionOid, preview.packageRevisionOid);
  assert.equal(receivedPassword, '');
  assert.doesNotMatch(JSON.stringify(getGraphImportHistory(project.id)), /one-time-secret/);
  await assert.rejects(() => executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'one-time-secret',
  }, dependencies), /已有图数据导入任务正在后台执行/);
  await scheduled[0]();
  assert.equal(receivedPassword, 'one-time-secret');
  assert.throws(() => getGraphImportUpload({
    uploadId: preview.id, projectId: project.id, owner: 'alice',
  }), /不存在或已过期/);
  const history = getGraphImportHistory(project.id);
  assert.equal(history[0].status, 'done');
  assert.equal(history[0].skippedCount, 2);
  assert.doesNotMatch(JSON.stringify(history), /one-time-secret/);
});

test('graph import service：知识包修订变化后拒绝旧上传的校验和执行', async () => {
  const {
    executeGraphImport,
    getGraphImportHistory,
    GraphImportPackageStaleError,
    uploadGraphImportExcel,
    validateGraphImportUpload,
  } = await import('../src/graph-import/service.js');
  const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');
  const project = await createGraphImportProject('图导入修订过期测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'stale.xlsx', buffer: importBuffer(),
  });
  const packages = getPackageArtifactStore();
  const current = (await packages.current(project.id))!;
  const advanced = await packages.saveFiles(project.id, {
    expectedRevision: current.revision.oid,
    files: { 'package/extensions/revision-marker.txt': '修订已更新\n' },
    action: {
      cause: 'candidate_accepted', summary: '接受候选修订', actorKind: 'user', actorName: 'alice',
    },
  });
  assert.notEqual(advanced.revision.oid, preview.packageRevisionOid);

  await assert.rejects(() => validateGraphImportUpload({
    projectId: project.id, owner: 'alice', uploadId: preview.id, mapping: graphMapping,
  }), (error: unknown) => error instanceof GraphImportPackageStaleError
    && error.uploadRevision === preview.packageRevisionOid
    && error.currentRevision === advanced.revision.oid);
  await assert.rejects(() => executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'one-time-secret',
  }, {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
  }), (error: unknown) => error instanceof GraphImportPackageStaleError);
  assert.equal(getGraphImportHistory(project.id).length, 0);
});

test('graph import service：后台任务排队期间知识包变化时不执行旧 schema 写入', async () => {
  const { executeGraphImport, getGraphImportHistory, uploadGraphImportExcel } =
    await import('../src/graph-import/service.js');
  const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');
  const project = await createGraphImportProject('图导入排队修订过期测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'queued-stale.xlsx', buffer: importBuffer(),
  });
  const scheduled: Array<() => Promise<void>> = [];
  let writerCalled = false;
  const started = await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'one-time-secret',
  }, {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
    schedule: (task) => { scheduled.push(task); },
    writer: async () => {
      writerCalled = true;
      return { nodeCount: 0, relationshipCount: 0 };
    },
  });
  const packages = getPackageArtifactStore();
  const current = (await packages.current(project.id))!;
  await packages.saveFiles(project.id, {
    expectedRevision: current.revision.oid,
    files: { 'package/extensions/queued-revision-marker.txt': '修订已更新\n' },
    action: {
      cause: 'candidate_accepted', summary: '排队期间接受候选修订', actorKind: 'user', actorName: 'alice',
    },
  });

  await scheduled[0]();

  assert.equal(writerCalled, false);
  const run = getGraphImportHistory(project.id).find((item) => item.id === started.run.id)!;
  assert.equal(run.status, 'failed');
  assert.match(run.error ?? '', /知识包已更新/u);
});

test('graph import service：审计只保存规范化 mapping 字段', async () => {
  const { uploadGraphImportExcel, executeGraphImport, getGraphImportHistory } =
    await import('../src/graph-import/service.js');
  const project = await createGraphImportProject('图导入 mapping 脱敏测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'mapping.xlsx', buffer: importBuffer(),
  });
  const scheduled: Array<() => Promise<void>> = [];
  const injectedMapping = {
    password: 'root-mapping-secret',
    relations: graphMapping.relations.map((relation) => ({
      ...relation,
      secret: 'relation-mapping-secret',
    })),
  } as any;
  await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: injectedMapping,
    password: 'execution-secret',
  }, {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
    schedule: (task) => { scheduled.push(task); },
    writer: async (plan) => ({
      nodeCount: plan.nodeCount,
      relationshipCount: plan.relationshipCount,
    }),
  });

  const running = getGraphImportHistory(project.id)[0];
  assert.deepEqual(running.mapping, graphMapping);
  assert.doesNotMatch(JSON.stringify(running), /root-mapping-secret|relation-mapping-secret|execution-secret/);
  await scheduled[0]();
});

test('graph import service：同步隔离预检错误不泄露一次性密码', async () => {
  const { uploadGraphImportExcel, executeGraphImport } = await import('../src/graph-import/service.js');
  const project = await createGraphImportProject('图导入预检脱敏测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'inspect.xlsx', buffer: importBuffer(),
  });
  const password = 'preflight-password-secret';
  await assert.rejects(executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password,
  }, {
    inspectIsolation: async () => { throw new Error(`认证失败 ${password}`); },
  }), (error: unknown) => {
    assert.match(String(error), /\[REDACTED\]/);
    assert.doesNotMatch(String(error), new RegExp(password));
    return true;
  });
});

test('graph import service：失败审计脱敏并保留临时文件供重试', async () => {
  const { uploadGraphImportExcel, executeGraphImport, getGraphImportHistory } = await import('../src/graph-import/service.js');
  const { getGraphImportUpload } = await import('../src/graph-import/upload-store.js');
  const project = await createGraphImportProject('图导入失败测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'retry.xlsx', buffer: importBuffer(),
  });
  const scheduled: Array<() => Promise<void>> = [];
  const started = await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'leaked-password',
  }, {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
    schedule: (task) => { scheduled.push(task); },
    writer: async () => { throw new Error('连接失败 leaked-password'); },
  });
  assert.equal(started.run.status, 'running');
  await scheduled[0]();
  assert.equal(getGraphImportUpload({
    uploadId: preview.id, projectId: project.id, owner: 'alice',
  }).id, preview.id);
  const failed = getGraphImportHistory(project.id)[0];
  assert.equal(failed.status, 'failed');
  assert.equal(failed.error, '连接失败 [REDACTED]');
  assert.doesNotMatch(JSON.stringify(failed), /leaked-password/);
});

test('graph import service：取消后台写入时等待 writer 完成中断清理', async () => {
  const {
    cancelGraphImportRun,
    cancelGraphImportUpload,
    executeGraphImport,
    getGraphImportHistory,
    uploadGraphImportExcel,
  } = await import('../src/graph-import/service.js');
  const { getGraphImportUpload } = await import('../src/graph-import/upload-store.js');
  const project = await createGraphImportProject('图导入取消测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'cancel.xlsx', buffer: importBuffer(),
  });
  const scheduled: Array<() => Promise<void>> = [];
  let notifyAbort = () => {};
  const abortObserved = new Promise<void>((resolve) => { notifyAbort = resolve; });
  let notifyWriterStarted = () => {};
  const writerStarted = new Promise<void>((resolve) => { notifyWriterStarted = resolve; });
  let releaseCleanup = () => {};
  const cleanupGate = new Promise<void>((resolve) => { releaseCleanup = resolve; });
  let cleanupFinished = false;
  const started = await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'cancel-secret',
  }, {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
    schedule: (task) => { scheduled.push(task); },
    writer: async (_plan, _connection, options) => new Promise((_resolve, reject) => {
      notifyWriterStarted();
      options.signal?.addEventListener('abort', async () => {
        notifyAbort();
        await cleanupGate;
        cleanupFinished = true;
        reject(options.signal?.reason);
      }, { once: true });
    }),
  });

  const background = scheduled[0]();
  await writerStarted;
  let cancellationFinished = false;
  const cancellation = cancelGraphImportRun(project.id, started.run.id)
    .finally(() => { cancellationFinished = true; });
  await abortObserved;
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(cancellationFinished, false);
  assert.equal(getGraphImportHistory(project.id)[0].status, 'running');

  releaseCleanup();
  const cancelled = await cancellation;
  await background;
  assert.equal(cleanupFinished, true);
  assert.equal(cancelled.status, 'cancelled');
  assert.equal(getGraphImportHistory(project.id)[0].status, 'cancelled');
  assert.equal(getGraphImportUpload({
    uploadId: preview.id, projectId: project.id, owner: 'alice',
  }).id, preview.id);
  await cancelGraphImportUpload({ projectId: project.id, owner: 'alice', uploadId: preview.id });
});

test('graph import service：任务尚未开始时取消不会再调用 writer', async () => {
  const {
    cancelGraphImportRun,
    executeGraphImport,
    getGraphImportHistory,
    uploadGraphImportExcel,
  } = await import('../src/graph-import/service.js');
  const project = await createGraphImportProject('图导入排队取消测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'queued.xlsx', buffer: importBuffer(),
  });
  const scheduled: Array<() => Promise<void>> = [];
  let writerCalls = 0;
  const started = await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'queued-secret',
  }, {
    inspectIsolation: async () => ({ nodeCount: 0, relationshipCount: 0 }),
    schedule: (task) => { scheduled.push(task); },
    writer: async () => {
      writerCalls += 1;
      return { nodeCount: 0, relationshipCount: 0 };
    },
  });

  const cancelled = await cancelGraphImportRun(project.id, started.run.id);
  await scheduled[0]();
  assert.equal(cancelled.status, 'cancelled');
  assert.equal(getGraphImportHistory(project.id)[0].status, 'cancelled');
  assert.equal(writerCalls, 0);
});

test('graph import service：冲突仅提示，确认后才记录替换导入', async () => {
  const { uploadGraphImportExcel, executeGraphImport, getGraphImportHistory } = await import('../src/graph-import/service.js');
  const { getGraphImportUpload } = await import('../src/graph-import/upload-store.js');
  const project = await createGraphImportProject('图导入替换测试');
  const preview = await uploadGraphImportExcel({
    projectId: project.id, owner: 'alice', filename: 'replace.xlsx', buffer: importBuffer(),
  });
  const historyBefore = getGraphImportHistory(project.id).length;
  let scheduledCount = 0;
  await assert.rejects(() => executeGraphImport({
    projectId: project.id, owner: 'alice', uploadId: preview.id,
    mapping: graphMapping, password: 'secret',
  }, {
    inspectIsolation: async () => ({ nodeCount: 2, relationshipCount: 1 }),
    schedule: () => { scheduledCount += 1; },
    writer: async () => { throw new Error('预检冲突时不应启动 writer'); },
  }), (error: any) => error instanceof GraphIsolationDataExistsError);
  assert.equal(scheduledCount, 0);
  assert.equal(getGraphImportHistory(project.id).length, historyBefore);
  assert.equal(getGraphImportUpload({
    uploadId: preview.id, projectId: project.id, owner: 'alice',
  }).id, preview.id);

  const scheduled: Array<() => Promise<void>> = [];
  const result = await executeGraphImport({
    projectId: project.id,
    owner: 'alice',
    uploadId: preview.id,
    mapping: graphMapping,
    password: 'secret',
    replaceExisting: true,
  }, {
    inspectIsolation: async () => { throw new Error('已确认替换时不需要重复预检'); },
    schedule: (task) => { scheduled.push(task); },
    writer: async (plan, _connection, options) => {
      assert.equal(options.signal instanceof AbortSignal, true);
      assert.deepEqual(
        { replaceExisting: options.replaceExisting, userName: options.userName },
        { replaceExisting: true, userName: 'alice' },
      );
      return { nodeCount: plan.nodeCount, relationshipCount: plan.relationshipCount };
    },
  });
  assert.equal(result.run.status, 'running');
  assert.equal(getGraphImportHistory(project.id).length, historyBefore + 1);
  await scheduled[0]();
  assert.equal(getGraphImportHistory(project.id)[0].status, 'done');
});

test('graph import form：固定知识包 Sheet 且关联字段只使用实体属性', async () => {
  const {
    GRAPH_ISOLATION_EXISTS_PROMPT, addRelationMapping, graphImportMappingPayload,
    graphRelationLabel, hasRunningGraphImport, isGraphIsolationConflict, joinPropertyOptions,
    objectImportIssue, updateRelationMapping,
  } = await import('../frontend/src/graph-import-form.js');
  const preview = {
    schema: graphSchema,
    sheets: [
      { name: 'Part', columns: ['part_id', 'relation_code', 'name', 'table_only'], rowCount: 1, sampleRows: [] },
      { name: 'BOMItem', columns: ['item_id', 'parent_code', 'name'], rowCount: 1, sampleRows: [] },
    ],
  };
  const draft = { sourceObject: 'Part', relationName: 'contains', targetObject: 'BOMItem' };
  const added = addRelationMapping([], draft, preview);
  assert.deepEqual(added, [{
    ...draft, matchMode: 'field_match', sourceJoinProperty: 'part_id', targetJoinProperty: 'item_id',
  }]);
  assert.throws(() => addRelationMapping(added, draft, preview), /不能重复添加/);
  assert.deepEqual(joinPropertyOptions(preview, 'Part').map((option) => option.value), [
    'part_id', 'relation_code', 'name',
  ]);
  const changed = updateRelationMapping(added, 0, 'sourceJoinProperty', 'relation_code');
  assert.deepEqual(graphImportMappingPayload(changed), { relations: changed });
  assert.deepEqual(graphImportMappingPayload([]), { relations: [] });
  const unavailable = {
    key: 'Unavailable', title: '不可导入实体', label: '不可导入实体', defaultSheet: 'MissingSheet',
    propertyMappings: [{ property: 'id', sourceColumn: 'id' }],
  };
  const unavailablePreview = {
    ...preview,
    schema: { ...preview.schema, objects: [...preview.schema.objects, unavailable] },
  };
  assert.match(objectImportIssue(unavailablePreview, unavailable), /MissingSheet/);
  assert.equal(GRAPH_ISOLATION_EXISTS_PROMPT, '当前您所选的隔离字段值在图数据库中已存在数据，是否要清空旧数据，并写入新数据？');
  assert.equal(isGraphIsolationConflict({ status: 409, data: { code: 'GRAPH_ISOLATION_DATA_EXISTS' } }), true);
  assert.equal(hasRunningGraphImport([{ status: 'done' }, { status: 'running' }]), true);

  const sameSheetPreview = {
    schema: {
      objects: [
        {
          key: 'Customer', title: '客户', defaultSheet: 'Wide',
          propertyMappings: [{ property: 'customer_id', sourceColumn: 'customer_id' }],
        },
        {
          key: 'Order', title: '订单', defaultSheet: 'Wide',
          propertyMappings: [
            { property: 'customer_id', sourceColumn: 'customer_id' },
            { property: 'order_id', sourceColumn: 'order_id' },
          ],
        },
      ],
      relations: [],
    },
    sheets: [{
      name: 'Wide', columns: ['customer_id', 'order_id'], rowCount: 2, sampleRows: [],
    }],
  };
  const sameSheetDraft = {
    sourceObject: 'Customer', relationName: '下单', targetObject: 'Order',
  };
  const sameSheetAdded = addRelationMapping([], sameSheetDraft, sameSheetPreview);
  assert.deepEqual(sameSheetAdded, [{ ...sameSheetDraft, matchMode: 'same_row' }]);
  const sameRow = sameSheetAdded;
  assert.deepEqual(sameRow, [{ ...sameSheetDraft, matchMode: 'same_row' }]);
  assert.equal(graphRelationLabel(sameRow[0], sameSheetPreview), '客户 -[下单]-> 订单（同 Sheet 同行）');
  assert.deepEqual(graphImportMappingPayload(sameRow), { relations: sameRow });

  const fieldMatched = updateRelationMapping(sameRow, 0, 'matchMode', 'field_match', sameSheetPreview);
  assert.deepEqual(fieldMatched, [{
    ...sameSheetDraft,
    matchMode: 'field_match',
    sourceJoinProperty: 'customer_id',
    targetJoinProperty: 'customer_id',
  }]);
  assert.deepEqual(
    updateRelationMapping(fieldMatched, 0, 'matchMode', 'same_row', sameSheetPreview),
    sameRow,
  );
});

test('graph import form：同行模式显示关联方式并隐藏关联字段', async () => {
  const { RelationCard } = await import('../frontend/src/GraphImportPanel.jsx');
  const preview = {
    schema: {
      objects: [
        {
          key: 'Customer', title: '客户', defaultSheet: 'Wide',
          propertyMappings: [{ property: 'customer_id', sourceColumn: 'customer_id' }],
        },
        {
          key: 'Order', title: '订单', defaultSheet: 'Wide',
          propertyMappings: [{ property: 'order_id', sourceColumn: 'order_id' }],
        },
      ],
    },
    sheets: [{ name: 'Wide', columns: ['customer_id', 'order_id'] }],
  };
  const card = RelationCard({
    index: 0,
    mapping: {
      sourceObject: 'Customer', relationName: '下单', targetObject: 'Order', matchMode: 'same_row',
    },
    preview,
    disabled: false,
    onChange: () => {},
    onRemove: () => {},
  });
  const elementText = (value: any): string => {
    if (value == null || typeof value === 'boolean') return '';
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    if (Array.isArray(value)) return value.map(elementText).join('');
    return elementText(value.props?.children);
  };
  const classNames = (value: any): string[] => {
    if (value == null || typeof value !== 'object') return [];
    if (Array.isArray(value)) return value.flatMap(classNames);
    return [value.props?.className, ...classNames(value.props?.children)].filter(Boolean);
  };
  const text = elementText(card);

  assert.match(text, /客户 -\[下单\]-> 订单（同 Sheet 同行）/);
  assert.match(text, /关联方式/);
  assert.match(text, /同一数据行/);
  assert.match(text, /按字段值匹配/);
  assert.match(text, /来源于同表的两个实体关联方式可以选择同一数据行，或者自由选择某一关联字段关联。/);
  assert.doesNotMatch(text, /关系关联属性/);
  assert.ok(classNames(card).includes('gi-same-row'));

  const fieldCard = RelationCard({
    index: 0,
    mapping: {
      sourceObject: 'Customer', relationName: '下单', targetObject: 'Order',
      matchMode: 'field_match', sourceJoinProperty: 'customer_id', targetJoinProperty: 'order_id',
    },
    preview,
    disabled: false,
    onChange: () => {},
    onRemove: () => {},
  });
  assert.ok(classNames(fieldCard).includes('gi-sides'));
  assert.equal(classNames(fieldCard).includes('gi-same-row'), false);
});

test('graph import execution：数据源上下文变化后忽略延迟冲突', async () => {
  const graphImportForm = await import('../frontend/src/graph-import-form.js');
  const { graphImportContextKey, startGraphImportExecution } = graphImportForm;
  const originalDataSource = {
    updatedAt: '2026-07-15T10:00:00.000Z',
    provider: 'neo4j',
    config: { uri: 'bolt://old:7687', database: 'neo4j', workspace_value: 'workspace-a' },
  };
  const changedDataSource = {
    ...originalDataSource,
    updatedAt: '2026-07-15T10:01:00.000Z',
    config: { ...originalDataSource.config, uri: 'bolt://new:7687' },
  };
  const originalKey = graphImportContextKey('project-1', originalDataSource);
  const changedKey = graphImportContextKey('project-1', changedDataSource);
  let currentKey = originalKey;
  let rejectAttempt: (error: Error) => void = () => {};
  const request = {
    projectId: 'project-1', uploadId: 'upload-1', payload: { relations: [] },
    password: 'one-time-password', contextKey: originalKey,
  };
  const delayed = startGraphImportExecution({
    execute: (...args) => new Promise((_resolve, reject) => {
      assert.equal(args[4], false);
      rejectAttempt = reject;
    }),
    request,
    isCurrent: (contextKey) => contextKey === currentKey,
  });
  currentKey = changedKey;
  rejectAttempt(Object.assign(new Error('exists'), {
    status: 409, data: { code: 'GRAPH_ISOLATION_DATA_EXISTS' },
  }));
  assert.deepEqual(await delayed, { kind: 'stale' });
  const current = await startGraphImportExecution({
    execute: async () => { throw Object.assign(new Error('exists'), {
      status: 409, data: { code: 'GRAPH_ISOLATION_DATA_EXISTS' },
    }); },
    request: { ...request, contextKey: changedKey },
    isCurrent: (contextKey) => contextKey === currentKey,
  });
  assert.equal(current.kind, 'conflict');
  assert.equal(current.request.contextKey, changedKey);

  const run = { id: 'run-1', status: 'running' };
  const started = await startGraphImportExecution({
    execute: async () => ({ run }),
    request: { ...request, contextKey: changedKey },
    isCurrent: (contextKey) => contextKey === currentKey,
  });
  assert.deepEqual(started, { kind: 'started', run });
  assert.equal(typeof graphImportForm.activeGraphImportRun, 'function');
  assert.equal(graphImportForm.activeGraphImportRun([run], run.id), run);
  assert.equal(graphImportForm.activeGraphImportRun([run], 'missing'), null);
});

test('graph import context：旧上下文的延迟成功和失败响应均不落状态', async () => {
  const graphImportForm = await import('../frontend/src/graph-import-form.js');
  assert.equal(typeof graphImportForm.runGraphImportContextRequest, 'function');
  const runRequest = graphImportForm.runGraphImportContextRequest;
  let currentKey = 'context-a';
  let resolveRequest: (value: any) => void = () => {};
  const cleaned: string[] = [];
  const delayedSuccess = runRequest({
    contextKey: 'context-a',
    isCurrent: (contextKey: string) => contextKey === currentKey,
    request: () => new Promise((resolve) => { resolveRequest = resolve; }),
    onStale: async (value: any) => { cleaned.push(value.upload.id); },
  });
  currentKey = 'context-b';
  resolveRequest({ upload: { id: 'upload-old' } });
  assert.deepEqual(await delayedSuccess, { kind: 'stale' });
  assert.deepEqual(cleaned, ['upload-old']);

  let rejectRequest: (error: Error) => void = () => {};
  currentKey = 'context-c';
  const delayedFailure = runRequest({
    contextKey: 'context-c',
    isCurrent: (contextKey: string) => contextKey === currentKey,
    request: () => new Promise((_resolve, reject) => { rejectRequest = reject; }),
  });
  currentKey = 'context-d';
  rejectRequest(new Error('旧上下文错误'));
  assert.deepEqual(await delayedFailure, { kind: 'stale' });

  const current = await runRequest({
    contextKey: 'context-d',
    isCurrent: (contextKey: string) => contextKey === currentKey,
    request: async () => ({ ok: true }),
  });
  assert.deepEqual(current, { kind: 'current', value: { ok: true } });
});

test('graph import conflict modal：居中展示并区分取消、确认与忙碌态', async () => {
  const { GraphIsolationConflictModal } = await import('../frontend/src/GraphImportPanel.jsx');
  let cancelCount = 0;
  let confirmCount = 0;
  const overlay = GraphIsolationConflictModal({
    busy: false,
    onCancel: () => { cancelCount += 1; },
    onConfirm: () => { confirmCount += 1; },
  });
  const dialog = overlay.props.children;
  const [title, message, footer] = dialog.props.children;
  const [cancelButton, confirmButton] = footer.props.children;
  assert.equal(overlay.props.className, 'modal-ov');
  assert.equal(dialog.props.role, 'dialog');
  assert.equal(dialog.props['aria-modal'], 'true');
  assert.equal(title.props.children, '确认覆盖旧数据');
  assert.match(message.props.children, /是否要清空旧数据/);
  assert.equal(confirmButton.props.autoFocus, true);
  const backdrop = {};
  overlay.props.onClick({ target: backdrop, currentTarget: backdrop });
  overlay.props.onKeyDown({ key: 'Escape', preventDefault: () => {} });
  cancelButton.props.onClick();
  confirmButton.props.onClick();
  assert.equal(cancelCount, 3);
  assert.equal(confirmCount, 1);

  let firstFocusCount = 0;
  let lastFocusCount = 0;
  const firstButton = { focus: () => { firstFocusCount += 1; } };
  const lastButton = { focus: () => { lastFocusCount += 1; } };
  const focusContainer = {
    ownerDocument: { activeElement: lastButton },
    querySelectorAll: () => [firstButton, lastButton],
  };
  overlay.props.onKeyDown({
    key: 'Tab', shiftKey: false, currentTarget: focusContainer, preventDefault: () => {},
  });
  focusContainer.ownerDocument.activeElement = firstButton;
  overlay.props.onKeyDown({
    key: 'Tab', shiftKey: true, currentTarget: focusContainer, preventDefault: () => {},
  });
  assert.equal(firstFocusCount, 1);
  assert.equal(lastFocusCount, 1);

  const busyOverlay = GraphIsolationConflictModal({
    busy: true,
    onCancel: () => { cancelCount += 1; },
    onConfirm: () => { confirmCount += 1; },
  });
  const busyButtons = busyOverlay.props.children.props.children[2].props.children;
  busyButtons[0].props.onClick();
  busyButtons[1].props.onClick();
  assert.equal(busyButtons[0].props.disabled, true);
  assert.equal(busyButtons[1].props.disabled, true);
  assert.equal(busyButtons[1].props.children, '正在清空并写入…');
  assert.equal(cancelCount, 3);
  assert.equal(confirmCount, 1);
});
