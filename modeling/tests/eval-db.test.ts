import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as XLSX from 'xlsx';
import { parseExcel } from '../src/documents/parser.js';
import {
  importEvaluationDatabaseExcel,
  importEvaluationDatabaseTables,
  testEvaluationDatabaseConnection,
  type EvaluationDatabaseClient,
} from '../src/evaluation/eval-db.js';
import { importEvaluationDatabaseExcelInWorker } from '../src/evaluation/eval-db-import-worker-client.js';
import type {
  EvaluationDatabaseConfig,
  EvaluationDatabaseEngine,
} from '../src/evaluation/data-source.js';
import {
  createProjectEvaluationDatabase,
  projectEvaluationDatabaseName,
} from '../src/evaluation/project-evaluation-database.js';
import type { DataDictTable } from '../src/types.js';

const table: DataDictTable = {
  name: 'orders',
  fields: [
    { name: 'id', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 },
    { name: 'active', inferredType: 'boolean', sampleValues: ['是'], distinctRatio: 1 },
    { name: 'name', inferredType: 'string', sampleValues: ['测试'], distinctRatio: 1 },
  ],
  rowCount: 2,
  rows: [['1', '是', '测试'], ['2', '否', '示例']],
};

function databaseConfig(engine: EvaluationDatabaseEngine): EvaluationDatabaseConfig {
  return {
    engine,
    host: `${engine}.local`,
    port: engine === 'mysql' ? 3306 : 5432,
    database: 'evaluation',
    username: 'tester',
    password: 'db-secret',
  };
}

function recordingClient(onQuery?: (sql: string, values?: unknown[]) => unknown | Promise<unknown>) {
  const calls: { sql: string; values?: unknown[] }[] = [];
  let closed = false;
  const client: EvaluationDatabaseClient = {
    query: async (sql, values) => {
      calls.push({ sql, values });
      return onQuery?.(sql, values);
    },
    close: async () => { closed = true; },
  };
  return { client, calls, isClosed: () => closed };
}

test('评测库导入：MySQL 与 PostgreSQL 使用各自标识符和参数语法', async () => {
  for (const engine of ['mysql', 'postgresql'] as const) {
    const recorder = recordingClient((sql) => (
      engine === 'mysql' && sql.startsWith('SELECT 1 FROM INFORMATION_SCHEMA.TABLES')
        ? [{ exists: true }]
        : []
    ));
    const result = await importEvaluationDatabaseTables(
      databaseConfig(engine),
      [table],
      { connect: async () => recorder.client },
    );
    assert.deepEqual(result, {
      engine,
      database: 'evaluation',
      tableCount: 1,
      rowCount: 2,
      tables: [{ name: 'orders', rowCount: 2 }],
    });
    assert.equal(recorder.isClosed(), true);
    const create = recorder.calls.find((call) => call.sql.startsWith('CREATE TABLE'))?.sql ?? '';
    const insert = recorder.calls.find((call) => call.sql.startsWith('INSERT INTO'));
    if (engine === 'mysql') {
      assert.doesNotMatch(recorder.calls.map((call) => call.sql).join('\n'), /DROP TABLE IF EXISTS `orders`/);
      assert.match(create, /`__mv_stg_[^`]+`.*`id` DECIMAL\(65, 30\).*`active` BOOLEAN.*`name` TEXT/);
      assert.match(insert?.sql ?? '', /VALUES \(\?, \?, \?\),\s*\(\?, \?, \?\)/);
      const rename = recorder.calls.find((call) => call.sql.startsWith('RENAME TABLE'))?.sql ?? '';
      assert.match(rename, /`orders` TO `__mv_bak_[^`]+`/);
      assert.match(rename, /`__mv_stg_[^`]+` TO `orders`/);
    } else {
      assert.equal(recorder.calls[0].sql, 'BEGIN');
      assert.equal(recorder.calls.at(-1)?.sql, 'COMMIT');
      assert.match(create, /"orders".*"id" NUMERIC.*"active" BOOLEAN.*"name" TEXT/);
      assert.match(insert?.sql ?? '', /VALUES \(\$1, \$2, \$3\),\s*\(\$4, \$5, \$6\)/);
    }
    assert.deepEqual(insert?.values, ['1', true, '测试', '2', false, '示例']);
  }
});

test('评测库导入：MySQL 写入失败时不删除或替换原表', async () => {
  const recorder = recordingClient((sql) => {
    if (sql.startsWith('INSERT INTO')) throw new Error('batch failed');
    if (sql.startsWith('SELECT 1 FROM INFORMATION_SCHEMA.TABLES')) return [{ exists: true }];
    return [];
  });
  await assert.rejects(importEvaluationDatabaseTables(
    databaseConfig('mysql'),
    [table],
    { connect: async () => recorder.client },
  ), /batch failed/);
  const sql = recorder.calls.map((call) => call.sql).join('\n');
  assert.doesNotMatch(sql, /DROP TABLE IF EXISTS `orders`/);
  assert.doesNotMatch(sql, /RENAME TABLE/);
  assert.match(sql, /DROP TABLE IF EXISTS `__mv_stg_/);
  assert.equal(recorder.isClosed(), true);
});

test('评测库连接：执行探针、关闭连接且错误不泄露密码', async () => {
  const recorder = recordingClient();
  await testEvaluationDatabaseConnection(databaseConfig('mysql'), {
    connect: async () => recorder.client,
  });
  assert.deepEqual(recorder.calls, [{ sql: 'SELECT 1 AS ok', values: undefined }]);
  assert.equal(recorder.isClosed(), true);

  await assert.rejects(testEvaluationDatabaseConnection(databaseConfig('postgresql'), {
    connect: async () => { throw new Error('认证失败 db-secret'); },
  }), (error: unknown) => {
    assert.match(String(error), /\[REDACTED\]/);
    assert.doesNotMatch(String(error), /db-secret/);
    return true;
  });
});

test('评测库导入：拒绝非 Excel 文件且不建立数据库连接', async () => {
  let connected = false;
  await assert.rejects(importEvaluationDatabaseExcel({
    filename: 'data.csv',
    buffer: Buffer.from('id\n1'),
    config: databaseConfig('mysql'),
  }, {
    connect: async () => {
      connected = true;
      return recordingClient().client;
    },
  }), /仅支持 .xlsx 或 .xls/);
  assert.equal(connected, false);
});

test('评测库导入：生产 Worker 隔离执行并返回脱敏错误', async () => {
  await assert.rejects(importEvaluationDatabaseExcelInWorker({
    filename: 'data.csv',
    buffer: Buffer.from('db-secret'),
    config: databaseConfig('mysql'),
  }), (error: unknown) => {
    assert.match(String(error), /仅支持 .xlsx 或 .xls/);
    assert.doesNotMatch(String(error), /db-secret/);
    return true;
  });
});

test('评测库导入：Excel 超过既有行数阈值时仍全量分批写入', async () => {
  const rowCount = 50_001;
  const worksheet = XLSX.utils.aoa_to_sheet([
    ['id', 'name'],
    ...Array.from({ length: rowCount }, (_, index) => [index + 1, `row-${index + 1}`]),
  ]);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'records');
  const buffer = Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));
  const recorder = recordingClient();

  const result = await importEvaluationDatabaseExcel({
    filename: 'records.xlsx',
    buffer,
    config: databaseConfig('postgresql'),
  }, { connect: async () => recorder.client });

  assert.equal(result.rowCount, rowCount);
  assert.deepEqual(result.tables, [{ name: 'records', rowCount }]);
  const inserts = recorder.calls.filter((call) => call.sql.startsWith('INSERT INTO'));
  assert.equal(inserts.length, Math.ceil(rowCount / 200));
  assert.equal(inserts.reduce((total, call) => total + (call.values?.length ?? 0), 0), rowCount * 2);
  assert.deepEqual(inserts.at(-1)?.values?.slice(-2), [String(rowCount), `row-${rowCount}`]);
});

test('评测库导入：完整列推断避免混合类型失败并保留超大整数', async () => {
  const worksheet = XLSX.utils.aoa_to_sheet([
    ['value', 'large_id'],
    ...Array.from({ length: 20 }, (_, index) => [String(index + 1), '9007199254740993']),
    ['ABC', '9007199254740995'],
  ]);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'mixed');
  const buffer = Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));
  const parsed = parseExcel(buffer, { maxRows: null })[0];

  assert.deepEqual(parsed.fields.map((field) => field.inferredType), ['string', 'string']);
  const recorder = recordingClient();
  await importEvaluationDatabaseTables(
    databaseConfig('postgresql'),
    [parsed],
    { connect: async () => recorder.client },
  );
  const inserted = recorder.calls.find((call) => call.sql.startsWith('INSERT INTO'));
  assert.equal(inserted?.values?.at(-1), '9007199254740995');
});

test('评测库导入：超出 MySQL DECIMAL 容量的数值按文本原样保存', () => {
  const tooManyIntegerDigits = `${'9'.repeat(36)}.1`;
  const tooManyFractionDigits = `0.${'1'.repeat(31)}`;
  const worksheet = XLSX.utils.aoa_to_sheet([
    ['large_decimal', 'precise_decimal', 'negative_code'],
    [tooManyIntegerDigits, tooManyFractionDigits, '-0012'],
  ]);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'precision');
  const buffer = Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));

  const parsed = parseExcel(buffer, { maxRows: null })[0];
  assert.deepEqual(parsed.fields.map((field) => field.inferredType), [
    'string', 'string', 'string',
  ]);
  assert.deepEqual(parsed.rows?.[0], [tooManyIntegerDigits, tooManyFractionDigits, '-0012']);
});

test('项目评测库：固定名称并为 MySQL/PostgreSQL 幂等创建唯一数据库', async () => {
  assert.equal(projectEvaluationDatabaseName('pj_a-b'), 'mv_shadow_pj_a_b');
  for (const engine of ['mysql', 'postgresql'] as const) {
    let exists = false;
    let closeCount = 0;
    const calls: { sql: string; values?: unknown[] }[] = [];
    const dependencies = {
      connectAdmin: async () => ({
        query: async (sql: string, values?: unknown[]) => {
          calls.push({ sql, values });
          if (sql.startsWith('SELECT')) return exists ? [{ exists: true }] : [];
          exists = true;
          return [];
        },
        close: async () => { closeCount += 1; },
      }),
    };
    const config = {
      engine,
      host: `${engine}.local`,
      port: engine === 'mysql' ? 3306 : 5432,
      username: 'admin',
      password: 'secret',
      database: 'attempted_override',
    };

    const created = await createProjectEvaluationDatabase({
      projectId: 'pj_a-b',
      config,
    }, dependencies);
    const reused = await createProjectEvaluationDatabase({
      projectId: 'pj_a-b',
      config,
    }, dependencies);

    assert.deepEqual(created, { engine, database: 'mv_shadow_pj_a_b', created: true });
    assert.deepEqual(reused, { engine, database: 'mv_shadow_pj_a_b', created: false });
    assert.equal(closeCount, 2);
    assert.equal(calls.filter((call) => call.sql.startsWith('CREATE DATABASE')).length, 1);
    assert.doesNotMatch(calls.map((call) => call.sql).join('\n'), /attempted_override/);
    if (engine === 'mysql') {
      assert.match(calls[1].sql, /CREATE DATABASE IF NOT EXISTS `mv_shadow_pj_a_b`/);
      assert.deepEqual(calls[0].values, ['mv_shadow_pj_a_b']);
    } else {
      assert.match(calls[1].sql, /CREATE DATABASE "mv_shadow_pj_a_b"/);
      assert.deepEqual(calls[0].values, ['mv_shadow_pj_a_b']);
    }
  }
});

test('项目评测库：创建失败时关闭连接且不泄露密码', async () => {
  let closed = false;
  await assert.rejects(createProjectEvaluationDatabase({
    projectId: 'pj_secret',
    config: {
      engine: 'postgresql', host: 'postgres.local', port: 5432,
      username: 'admin', password: 'db-secret',
    },
  }, {
    connectAdmin: async () => ({
      query: async () => { throw new Error('permission denied db-secret'); },
      close: async () => { closed = true; },
    }),
  }), (error: unknown) => {
    assert.match(String(error), /\[REDACTED\]/);
    assert.doesNotMatch(String(error), /db-secret/);
    return true;
  });
  assert.equal(closed, true);
});
