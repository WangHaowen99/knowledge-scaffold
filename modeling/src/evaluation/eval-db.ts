import { randomUUID } from 'node:crypto';
import { extname } from 'node:path';
import { parseExcel } from '../documents/parser.js';
import type { DataDictField, DataDictTable } from '../types.js';
import type {
  EvaluationDatabaseConfig,
  EvaluationDatabaseEngine,
} from './data-source.js';

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const INSERT_BATCH_SIZE = 200;

export interface EvaluationDatabaseClient {
  query(sql: string, values?: unknown[]): Promise<unknown>;
  close(): Promise<void>;
}

export type EvaluationDatabaseConnector = (
  config: EvaluationDatabaseConfig,
) => Promise<EvaluationDatabaseClient>;

export interface EvaluationDatabaseDependencies {
  connect?: EvaluationDatabaseConnector;
}

export interface EvaluationDatabaseImportResult {
  engine: EvaluationDatabaseEngine;
  database: string;
  tableCount: number;
  rowCount: number;
  tables: { name: string; rowCount: number }[];
}

export function validateEvaluationDatabaseExcelUpload(
  filename: string,
  buffer: Buffer,
): void {
  if (!['.xlsx', '.xls'].includes(extname(filename).toLowerCase())) {
    throw new Error('仅支持 .xlsx 或 .xls 文件');
  }
  if (buffer.byteLength > MAX_FILE_SIZE) throw new Error('Excel 文件不能超过 50MB');
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function safeError(error: unknown, password: string): string {
  return password
    ? errorMessage(error).split(password).join('[REDACTED]')
    : errorMessage(error);
}

async function defaultConnector(
  config: EvaluationDatabaseConfig,
): Promise<EvaluationDatabaseClient> {
  if (config.engine === 'mysql') {
    const mysql = await import('mysql2/promise');
    const connection = await mysql.createConnection({
      host: config.host,
      port: config.port,
      user: config.username,
      password: config.password,
      database: config.database,
      connectTimeout: 10_000,
    });
    return {
      query: async (sql, values) => (await connection.query(sql, values))[0],
      close: async () => { await connection.end(); },
    };
  }

  const { Client } = await import('pg');
  const client = new Client({
    host: config.host,
    port: config.port,
    user: config.username,
    password: config.password,
    database: config.database,
    connectionTimeoutMillis: 10_000,
  });
  await client.connect();
  return {
    query: async (sql, values) => (await client.query(sql, values)).rows,
    close: async () => { await client.end(); },
  };
}

export async function testEvaluationDatabaseConnection(
  config: EvaluationDatabaseConfig,
  dependencies: EvaluationDatabaseDependencies = {},
): Promise<void> {
  let client: EvaluationDatabaseClient | undefined;
  try {
    client = await (dependencies.connect ?? defaultConnector)(config);
    await client.query('SELECT 1 AS ok');
  } catch (error) {
    throw new Error(`评测库连接测试失败：${safeError(error, config.password)}`);
  } finally {
    await client?.close().catch(() => undefined);
  }
}

function quoteIdentifier(engine: EvaluationDatabaseEngine, value: string): string {
  return engine === 'mysql'
    ? `\`${value.replace(/`/g, '``')}\``
    : `"${value.replace(/"/g, '""')}"`;
}

function columnType(engine: EvaluationDatabaseEngine, field: DataDictField): string {
  if (field.inferredType === 'number') return engine === 'mysql' ? 'DECIMAL(65, 30)' : 'NUMERIC';
  if (field.inferredType === 'boolean') return 'BOOLEAN';
  if (field.inferredType === 'date') return engine === 'mysql' ? 'DATETIME' : 'TIMESTAMP';
  return 'TEXT';
}

function validateTable(table: DataDictTable): void {
  if (!table.fields.length) throw new Error(`Sheet「${table.name}」没有可导入字段`);
  const names = table.fields.map((field) => field.name.trim());
  if (names.some((name) => !name)) throw new Error(`Sheet「${table.name}」存在空字段名`);
  const normalized = names.map((name) => name.toLocaleLowerCase());
  if (new Set(normalized).size !== normalized.length) {
    throw new Error(`Sheet「${table.name}」存在重复字段名`);
  }
}

const BOOLEAN_VALUES: Record<string, boolean> = {
  '1': true,
  '0': false,
  true: true,
  false: false,
  yes: true,
  no: false,
  y: true,
  n: false,
  '是': true,
  '否': false,
};

function coerceValue(value: unknown, field: DataDictField): unknown {
  const content = String(value ?? '').trim();
  if (!content) return null;
  if (field.inferredType === 'number') {
    if (!/^-?\d+(?:\.\d+)?$/.test(content)) {
      throw new Error(`字段「${field.name}」包含非数值内容：${content}`);
    }
    return content;
  }
  if (field.inferredType === 'boolean') {
    const boolean = BOOLEAN_VALUES[content.toLocaleLowerCase()];
    if (boolean === undefined) {
      throw new Error(`字段「${field.name}」包含非法布尔值：${content}`);
    }
    return boolean;
  }
  return content;
}

function insertSql(
  engine: EvaluationDatabaseEngine,
  table: DataDictTable,
  rowCount: number,
  targetName = table.name,
): string {
  let parameter = 0;
  const rows = Array.from({ length: rowCount }, () => {
    const placeholders = table.fields.map(() => {
      parameter += 1;
      return engine === 'mysql' ? '?' : `$${parameter}`;
    });
    return `(${placeholders.join(', ')})`;
  });
  const columns = table.fields.map((field) => quoteIdentifier(engine, field.name)).join(', ');
  return `INSERT INTO ${quoteIdentifier(engine, targetName)} (${columns}) VALUES ${rows.join(', ')}`;
}

async function createAndPopulateTable(
  client: EvaluationDatabaseClient,
  engine: EvaluationDatabaseEngine,
  table: DataDictTable,
  targetName: string,
): Promise<void> {
  validateTable(table);
  const tableName = quoteIdentifier(engine, targetName);
  const columns = table.fields.map((field) => (
    `${quoteIdentifier(engine, field.name)} ${columnType(engine, field)}`
  ));
  await client.query(`CREATE TABLE ${tableName} (${columns.join(', ')})`);
  const rows = table.rows ?? [];
  for (let start = 0; start < rows.length; start += INSERT_BATCH_SIZE) {
    const batch = rows.slice(start, start + INSERT_BATCH_SIZE);
    const values = batch.flatMap((row) => table.fields.map(
      (field, column) => coerceValue(row[column], field),
    ));
    await client.query(insertSql(engine, table, batch.length, targetName), values);
  }
}

function temporaryTableName(kind: 'stg' | 'bak', token: string, index: number): string {
  return `__mv_${kind}_${token}_${index}`;
}

function hasRows(result: unknown): boolean {
  return Array.isArray(result) && result.length > 0;
}

async function cleanupTables(
  client: EvaluationDatabaseClient,
  engine: EvaluationDatabaseEngine,
  names: string[],
): Promise<void> {
  for (const name of names) {
    await client.query(`DROP TABLE IF EXISTS ${quoteIdentifier(engine, name)}`).catch(() => undefined);
  }
}

async function replaceMysqlTablesAtomically(
  client: EvaluationDatabaseClient,
  database: string,
  tables: DataDictTable[],
): Promise<void> {
  const token = randomUUID().replace(/-/g, '').slice(0, 12);
  const swaps = tables.map((table, index) => ({
    table,
    staging: temporaryTableName('stg', token, index),
    backup: temporaryTableName('bak', token, index),
    existing: false,
  }));
  let swapped = false;
  try {
    for (const entry of swaps) {
      await cleanupTables(client, 'mysql', [entry.staging, entry.backup]);
      await createAndPopulateTable(client, 'mysql', entry.table, entry.staging);
    }
    for (const entry of swaps) {
      entry.existing = hasRows(await client.query(
        `SELECT 1 FROM INFORMATION_SCHEMA.TABLES
          WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? LIMIT 1`,
        [database, entry.table.name],
      ));
    }
    const renames = swaps.flatMap((entry) => [
      ...(entry.existing ? [
        `${quoteIdentifier('mysql', entry.table.name)} TO ${quoteIdentifier('mysql', entry.backup)}`,
      ] : []),
      `${quoteIdentifier('mysql', entry.staging)} TO ${quoteIdentifier('mysql', entry.table.name)}`,
    ]);
    await client.query(`RENAME TABLE ${renames.join(', ')}`);
    swapped = true;
  } finally {
    if (swapped) {
      await cleanupTables(client, 'mysql', swaps.filter((entry) => entry.existing)
        .map((entry) => entry.backup));
    } else {
      await cleanupTables(client, 'mysql', swaps.flatMap((entry) => [entry.staging, entry.backup]));
    }
  }
}

async function replacePostgresqlTablesInTransaction(
  client: EvaluationDatabaseClient,
  tables: DataDictTable[],
): Promise<void> {
  await client.query('BEGIN');
  try {
    for (const table of tables) {
      await client.query(`DROP TABLE IF EXISTS ${quoteIdentifier('postgresql', table.name)}`);
      await createAndPopulateTable(client, 'postgresql', table, table.name);
    }
    await client.query('COMMIT');
  } catch (error) {
    await client.query('ROLLBACK').catch(() => undefined);
    throw error;
  }
}

export async function importEvaluationDatabaseTables(
  config: EvaluationDatabaseConfig,
  tables: DataDictTable[],
  dependencies: EvaluationDatabaseDependencies = {},
): Promise<EvaluationDatabaseImportResult> {
  const withRows = tables.filter((table) => table.rows?.length);
  if (!withRows.length) throw new Error('Excel 中没有可导入的数据行');
  let client: EvaluationDatabaseClient | undefined;
  try {
    client = await (dependencies.connect ?? defaultConnector)(config);
    if (config.engine === 'mysql') {
      await replaceMysqlTablesAtomically(client, config.database, withRows);
    } else {
      await replacePostgresqlTablesInTransaction(client, withRows);
    }
    return {
      engine: config.engine,
      database: config.database,
      tableCount: withRows.length,
      rowCount: withRows.reduce((total, table) => total + (table.rows?.length ?? 0), 0),
      tables: withRows.map((table) => ({ name: table.name, rowCount: table.rows?.length ?? 0 })),
    };
  } catch (error) {
    throw new Error(`Excel 导入评测库失败：${safeError(error, config.password)}`);
  } finally {
    await client?.close().catch(() => undefined);
  }
}

export async function importEvaluationDatabaseExcel(
  input: { filename: string; buffer: Buffer; config: EvaluationDatabaseConfig },
  dependencies: EvaluationDatabaseDependencies = {},
): Promise<EvaluationDatabaseImportResult> {
  validateEvaluationDatabaseExcelUpload(input.filename, input.buffer);
  const tables = parseExcel(input.buffer, { maxRows: null });
  return importEvaluationDatabaseTables(input.config, tables, dependencies);
}
