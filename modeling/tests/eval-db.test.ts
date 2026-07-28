import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as XLSX from 'xlsx';
import { parseExcel } from '../src/documents/parser.js';
import {
  importEvaluationDatabaseExcel,
  importEvaluationDatabaseTables,
  type EvaluationDatabaseClient,
} from '../src/evaluation/eval-db.js';
import { importEvaluationDatabaseExcelInWorker } from '../src/evaluation/eval-db-import-worker-client.js';
import {
  evaluationDatabaseUrlSummary,
  redactEvaluationDatabaseError,
  resolveEvaluationDatabaseTarget,
  selectEvaluationDatabaseUrlOverride,
  type EvaluationDatabaseTarget,
} from '../src/evaluation/evaluation-database-target.js';
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

function databaseTarget(overrides: Partial<EvaluationDatabaseTarget> = {}): EvaluationDatabaseTarget {
  return {
    connectionUrl: 'mysql://tester:db-secret@mysql.local:3306/',
    database: 'evaluation',
    ...overrides,
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

test('评测库目标：使用 EVAL_DB_URL 服务器并以 Scene 作为数据库名', () => {
  assert.deepEqual(resolveEvaluationDatabaseTarget(
    ' scene_a ',
    ' mysql://user:p%40ss@mysql.local:3307/ignored_database?charset=utf8mb4 ',
  ), {
    connectionUrl: 'mysql://user:p%40ss@mysql.local:3307/?charset=utf8mb4',
    database: 'scene_a',
  });
  assert.throws(() => resolveEvaluationDatabaseTarget('', 'mysql://root:secret@localhost:3306'),
    /Scene 不能为空/);
  assert.throws(() => resolveEvaluationDatabaseTarget('invalid-scene', 'mysql://root:secret@localhost:3306'),
    /Scene 格式非法/);
  assert.throws(() => resolveEvaluationDatabaseTarget('scene_a', ''), /缺少 EVAL_DB_URL/);
  assert.throws(() => resolveEvaluationDatabaseTarget('scene_a', 'mariadb://localhost:3306'),
    /仅支持 mysql/);
});

test('评测库目标：只向前端返回脱敏 URL，并要求明确选择默认或自定义连接', () => {
  assert.deepEqual(evaluationDatabaseUrlSummary(
    'mysql://user:p%40ss@mysql.local:3307/default_db?token=query-secret&charset=utf8mb4',
  ), {
    configured: true,
    databaseUrlPreview: 'mysql://user:******@mysql.local:3307/default_db?token=******&charset=utf8mb4',
  });
  assert.deepEqual(evaluationDatabaseUrlSummary(''), {
    configured: false,
    databaseUrlPreview: '',
  });
  assert.equal(selectEvaluationDatabaseUrlOverride('default', ''), undefined);
  assert.equal(
    selectEvaluationDatabaseUrlOverride('custom', ' mysql://custom:secret@db.local:3306 '),
    'mysql://custom:secret@db.local:3306',
  );
  assert.throws(() => selectEvaluationDatabaseUrlOverride('', ''), /确认.*EVAL_DB_URL/);
  assert.throws(() => selectEvaluationDatabaseUrlOverride('custom', ''), /自定义.*URL.*不能为空/);
});

test('评测库目标：错误信息隐藏密码和敏感查询参数', () => {
  const target = resolveEvaluationDatabaseTarget(
    'scene_a',
    'mysql://user:p%40ss@mysql.local:3306/?token=query-secret&sslKey=client-key',
  );
  const message = redactEvaluationDatabaseError(
    new Error(`连接失败 p%40ss p@ss query-secret client-key ${target.connectionUrl}`),
    target,
  );
  assert.doesNotMatch(message, /p%40ss|p@ss|query-secret|client-key|mysql:\/\//);
  assert.match(message, /\[REDACTED\]/);
});

test('评测库导入：MySQL 使用临时表原子替换目标表', async () => {
  const recorder = recordingClient((sql) => (
    sql.startsWith('SELECT 1 FROM INFORMATION_SCHEMA.TABLES') ? [{ exists: true }] : []
  ));
  const result = await importEvaluationDatabaseTables(
    databaseTarget(),
    [table],
    { connect: async () => recorder.client },
  );
  assert.deepEqual(result, {
    database: 'evaluation',
    tableCount: 1,
    rowCount: 2,
    tables: [{ name: 'orders', rowCount: 2 }],
  });
  assert.equal(recorder.isClosed(), true);
  const sql = recorder.calls.map((call) => call.sql).join('\n');
  assert.doesNotMatch(sql, /DROP TABLE IF EXISTS `orders`/);
  assert.match(sql, /CREATE TABLE `__mv_stg_[^`]+`.*`id` DECIMAL\(65, 30\).*`active` BOOLEAN.*`name` TEXT/);
  assert.match(sql, /INSERT INTO `__mv_stg_[^`]+`.*VALUES \(\?, \?, \?\),\s*\(\?, \?, \?\)/);
  assert.match(sql, /`orders` TO `__mv_bak_[^`]+`/);
  assert.match(sql, /`__mv_stg_[^`]+` TO `orders`/);
  const insert = recorder.calls.find((call) => call.sql.startsWith('INSERT INTO'));
  assert.deepEqual(insert?.values, ['1', true, '测试', '2', false, '示例']);
});

test('评测库导入：写入失败时不删除或替换原表', async () => {
  const recorder = recordingClient((sql) => {
    if (sql.startsWith('INSERT INTO')) throw new Error('batch failed');
    if (sql.startsWith('SELECT 1 FROM INFORMATION_SCHEMA.TABLES')) return [{ exists: true }];
    return [];
  });
  await assert.rejects(importEvaluationDatabaseTables(
    databaseTarget(),
    [table],
    { connect: async () => recorder.client },
  ), /batch failed/);
  const sql = recorder.calls.map((call) => call.sql).join('\n');
  assert.doesNotMatch(sql, /DROP TABLE IF EXISTS `orders`/);
  assert.doesNotMatch(sql, /RENAME TABLE/);
  assert.match(sql, /DROP TABLE IF EXISTS `__mv_stg_/);
  assert.equal(recorder.isClosed(), true);
});

test('评测库导入：拒绝非 Excel 文件且不建立数据库连接', async () => {
  let connected = false;
  await assert.rejects(importEvaluationDatabaseExcel({
    filename: 'data.csv',
    buffer: Buffer.from('id\n1'),
    target: databaseTarget(),
  }, {
    connect: async () => {
      connected = true;
      return recordingClient().client;
    },
  }), /仅支持 .xlsx 或 .xls/);
  assert.equal(connected, false);
});

test('评测库导入：生产 Worker 隔离执行并隐藏 EVAL_DB_URL 密钥', async () => {
  await assert.rejects(importEvaluationDatabaseExcelInWorker({
    filename: 'data.csv',
    buffer: Buffer.from('db-secret'),
    target: databaseTarget(),
  }), (error: unknown) => {
    assert.match(String(error), /仅支持 .xlsx 或 .xls/);
    assert.doesNotMatch(String(error), /db-secret/);
    assert.doesNotMatch(String(error), /mysql:\/\//);
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
    target: databaseTarget(),
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
    databaseTarget(),
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

test('项目评测库：通过 EVAL_DB_URL 幂等创建固定 Scene 数据库', async () => {
  assert.equal(projectEvaluationDatabaseName('pj_a-b'), 'mv_shadow_pj_a_b');
  let exists = false;
  let closeCount = 0;
  const calls: { sql: string; values?: unknown[] }[] = [];
  const targets: EvaluationDatabaseTarget[] = [];
  const dependencies = {
    databaseUrl: 'mysql://admin:secret@mysql.local:3306/ignored',
    connectAdmin: async (target: EvaluationDatabaseTarget) => {
      targets.push(target);
      return {
        query: async (sql: string, values?: unknown[]) => {
          calls.push({ sql, values });
          if (sql.startsWith('SELECT')) return exists ? [{ exists: true }] : [];
          exists = true;
          return [];
        },
        close: async () => { closeCount += 1; },
      };
    },
  };

  const created = await createProjectEvaluationDatabase({ projectId: 'pj_a-b' }, dependencies);
  const reused = await createProjectEvaluationDatabase({ projectId: 'pj_a-b' }, dependencies);

  assert.deepEqual(created, { database: 'mv_shadow_pj_a_b', created: true });
  assert.deepEqual(reused, { database: 'mv_shadow_pj_a_b', created: false });
  assert.equal(closeCount, 2);
  assert.deepEqual(targets[0], {
    connectionUrl: 'mysql://admin:secret@mysql.local:3306/',
    database: 'mv_shadow_pj_a_b',
  });
  assert.equal(calls.filter((call) => call.sql.startsWith('CREATE DATABASE')).length, 1);
  assert.match(calls[1].sql, /CREATE DATABASE IF NOT EXISTS `mv_shadow_pj_a_b`/);
  assert.deepEqual(calls[0].values, ['mv_shadow_pj_a_b']);
});

test('项目评测库：创建失败时关闭连接且不泄露 EVAL_DB_URL', async () => {
  let closed = false;
  await assert.rejects(createProjectEvaluationDatabase({ projectId: 'pj_secret' }, {
    databaseUrl: 'mysql://admin:db-secret@mysql.local:3306',
    connectAdmin: async () => ({
      query: async () => { throw new Error('permission denied db-secret'); },
      close: async () => { closed = true; },
    }),
  }), (error: unknown) => {
    assert.match(String(error), /\[REDACTED\]/);
    assert.doesNotMatch(String(error), /db-secret/);
    assert.doesNotMatch(String(error), /mysql:\/\//);
    return true;
  });
  assert.equal(closed, true);
});
