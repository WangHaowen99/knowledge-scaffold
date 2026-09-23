import { randomUUID } from 'node:crypto';
import { extname } from 'node:path';
import XLSX from 'xlsx';
import { parseExcel } from '../documents/parser.js';
import type { DataDictField, DataDictTable } from '../types.js';
import type { ProjectDataSource } from './data-source-repo.js';
import {
  redactEvaluationDatabaseError,
  type EvaluationDatabaseTarget,
} from './evaluation-database-target.js';

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const INSERT_BATCH_SIZE = 200;

export interface EvaluationDatabaseExcelFileInput {
  filename: string;
  buffer: Buffer;
}

export interface EvaluationDatabaseClient {
  query(sql: string, values?: unknown[]): Promise<unknown>;
  close(): Promise<void>;
}

export type EvaluationDatabaseConnector = (
  target: EvaluationDatabaseTarget,
) => Promise<EvaluationDatabaseClient>;

export interface EvaluationDatabaseDependencies {
  connect?: EvaluationDatabaseConnector;
}

export interface EvaluationDatabaseImportResult {
  database: string;
  tableCount: number;
  rowCount: number;
  tables: { name: string; rowCount: number }[];
}

export interface EvaluationDatabaseProvisionResult {
  ok: boolean;
  schema?: string;
  detail: string;
}

export function assertEvaluationDatabaseReady(
  result: EvaluationDatabaseProvisionResult,
  dataSource: ProjectDataSource,
): void {
  const requiresMysqlMirror = dataSource.mode === 'connection'
    && ['nl2sql_http', 'nl2sql_template'].includes(dataSource.provider ?? '');
  if (requiresMysqlMirror && !result.ok) {
    throw new Error(`评测库准备失败，已停止本轮以避免使用旧数据：${result.detail}`);
  }
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

export function mergeEvaluationDatabaseExcelFiles(
  files: EvaluationDatabaseExcelFileInput[],
): EvaluationDatabaseExcelFileInput {
  if (!files.length) throw new Error('缺少 Excel 文件');
  if (files.length === 1) {
    validateEvaluationDatabaseExcelUpload(files[0].filename, files[0].buffer);
    return files[0];
  }
  const totalSize = files.reduce((sum, file) => sum + file.buffer.byteLength, 0);
  if (totalSize > MAX_FILE_SIZE) throw new Error('Excel 文件总大小不能超过 50MB');
  const workbook = XLSX.utils.book_new();
  const seenSheets = new Map<string, string>();
  for (const file of files) {
    validateEvaluationDatabaseExcelUpload(file.filename, file.buffer);
    const parsed = XLSX.read(file.buffer, { type: 'buffer', cellDates: false, cellNF: true, cellText: false });
    for (const sheetName of parsed.SheetNames) {
      const previous = seenSheets.get(sheetName);
      if (previous) {
        throw new Error(`多个 Excel 中存在重复 Sheet「${sheetName}」：${previous}、${file.filename}`);
      }
      seenSheets.set(sheetName, file.filename);
      XLSX.utils.book_append_sheet(workbook, parsed.Sheets[sheetName], sheetName);
    }
  }
  return {
    filename: files.map((file) => file.filename).join(' + '),
    buffer: Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' })),
  };
}

async function defaultConnector(
  target: EvaluationDatabaseTarget,
): Promise<EvaluationDatabaseClient> {
  const mysql = await import('mysql2/promise');
  const connection = await mysql.createConnection({
    uri: target.connectionUrl,
    database: target.database,
    connectTimeout: 10_000,
  });
  return {
    query: async (sql, values) => (await connection.query(sql, values))[0],
    close: async () => { await connection.end(); },
  };
}

function quoteIdentifier(value: string): string {
  return `\`${value.replace(/`/g, '``')}\``;
}

function columnType(field: DataDictField): string {
  if (field.inferredType === 'number') return 'DECIMAL(65, 30)';
  if (field.inferredType === 'boolean') return 'BOOLEAN';
  if (field.inferredType === 'date') return 'DATETIME';
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

// DATETIME 只收完整且日历合法的年月日（可带时分秒）。解析层的日期推断只看前缀
// （`^\d{4}[-/]\d{1,2}`），年月（2024-01）、带后缀文字（2024-01-05 上午）、非法日期
// （2024-02-30）或带时区的 ISO 串都会被判成日期列，原样写入会让 MySQL 严格模式拒绝整批。
const MYSQL_DATE_TIME = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?)?$/;

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

export function mysqlDateTimeValue(content: string): string | null {
  const match = MYSQL_DATE_TIME.exec(content.trim());
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const calendar = new Date(Date.UTC(year, month - 1, day));
  if (year < 1000 || calendar.getUTCFullYear() !== year
    || calendar.getUTCMonth() !== month - 1 || calendar.getUTCDate() !== day) return null;
  const date = `${pad(year, 4)}-${pad(month)}-${pad(day)}`;
  if (match[4] === undefined) return date;
  const [hour, minute, second] = [Number(match[4]), Number(match[5]), Number(match[6] ?? '0')];
  if (hour > 23 || minute > 59 || second > 59) return null;
  return `${date}T${pad(hour)}:${pad(minute)}:${pad(second)}`;
}

/** 推断为日期但有值不能无损写入 DATETIME 的列降级为 TEXT，原样保存而不是整批失败。 */
function withStorableColumnTypes(table: DataDictTable): DataDictTable {
  const fields = table.fields.map((field, column) => {
    if (field.inferredType !== 'date') return field;
    const storable = (table.rows ?? []).every((row) => {
      const content = String(row[column] ?? '').trim();
      return !content || mysqlDateTimeValue(content) !== null;
    });
    return storable ? field : { ...field, inferredType: 'string' as const };
  });
  return { ...table, fields };
}

function coerceValue(value: unknown, field: DataDictField): unknown {
  const content = String(value ?? '').trim();
  if (!content) return null;
  if (field.inferredType === 'date') {
    const dateTime = mysqlDateTimeValue(content);
    if (dateTime === null) throw new Error(`字段「${field.name}」包含非法日期：${content}`);
    return dateTime;
  }
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

export function validateEvaluationDatabaseExcelContents(
  filename: string,
  buffer: Buffer,
): void {
  validateEvaluationDatabaseExcelUpload(filename, buffer);
  const tables = parseExcel(buffer, { maxRows: null })
    .filter((table) => table.rows?.length)
    .map(withStorableColumnTypes);
  if (!tables.length) throw new Error('Excel 中没有可导入的数据行');
  for (const table of tables) {
    validateTable(table);
    for (const row of table.rows ?? []) {
      table.fields.forEach((field, column) => coerceValue(row[column], field));
    }
  }
}

export function validateEvaluationDatabaseExcelFilesContents(
  files: EvaluationDatabaseExcelFileInput[],
): EvaluationDatabaseExcelFileInput {
  const merged = mergeEvaluationDatabaseExcelFiles(files);
  validateEvaluationDatabaseExcelContents(merged.filename, merged.buffer);
  return merged;
}

function insertSql(table: DataDictTable, rowCount: number, targetName = table.name): string {
  const rows = Array.from({ length: rowCount }, () => (
    `(${table.fields.map(() => '?').join(', ')})`
  ));
  const columns = table.fields.map((field) => quoteIdentifier(field.name)).join(', ');
  return `INSERT INTO ${quoteIdentifier(targetName)} (${columns}) VALUES ${rows.join(', ')}`;
}

async function createAndPopulateTable(
  client: EvaluationDatabaseClient,
  table: DataDictTable,
  targetName: string,
): Promise<void> {
  validateTable(table);
  const columns = table.fields.map((field) => (
    `${quoteIdentifier(field.name)} ${columnType(field)}`
  ));
  await client.query(`CREATE TABLE ${quoteIdentifier(targetName)} (${columns.join(', ')})`);
  const rows = table.rows ?? [];
  for (let start = 0; start < rows.length; start += INSERT_BATCH_SIZE) {
    const batch = rows.slice(start, start + INSERT_BATCH_SIZE);
    const values = batch.flatMap((row) => table.fields.map(
      (field, column) => coerceValue(row[column], field),
    ));
    await client.query(insertSql(table, batch.length, targetName), values);
  }
}

function temporaryTableName(kind: 'stg' | 'bak', token: string, index: number): string {
  return `__mv_${kind}_${token}_${index}`;
}

function hasRows(result: unknown): boolean {
  return Array.isArray(result) && result.length > 0;
}

async function cleanupTables(client: EvaluationDatabaseClient, names: string[]): Promise<void> {
  for (const name of names) {
    await client.query(`DROP TABLE IF EXISTS ${quoteIdentifier(name)}`).catch(() => undefined);
  }
}

async function replaceTablesAtomically(
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
      await cleanupTables(client, [entry.staging, entry.backup]);
      await createAndPopulateTable(client, entry.table, entry.staging);
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
        `${quoteIdentifier(entry.table.name)} TO ${quoteIdentifier(entry.backup)}`,
      ] : []),
      `${quoteIdentifier(entry.staging)} TO ${quoteIdentifier(entry.table.name)}`,
    ]);
    await client.query(`RENAME TABLE ${renames.join(', ')}`);
    swapped = true;
  } finally {
    if (swapped) {
      await cleanupTables(client, swaps.filter((entry) => entry.existing)
        .map((entry) => entry.backup));
    } else {
      await cleanupTables(client, swaps.flatMap((entry) => [entry.staging, entry.backup]));
    }
  }
}

export async function importEvaluationDatabaseTables(
  target: EvaluationDatabaseTarget,
  tables: DataDictTable[],
  dependencies: EvaluationDatabaseDependencies = {},
): Promise<EvaluationDatabaseImportResult> {
  const withRows = tables.filter((table) => table.rows?.length).map(withStorableColumnTypes);
  if (!withRows.length) throw new Error('Excel 中没有可导入的数据行');
  let client: EvaluationDatabaseClient | undefined;
  try {
    client = await (dependencies.connect ?? defaultConnector)(target);
    await replaceTablesAtomically(client, target.database, withRows);
    return {
      database: target.database,
      tableCount: withRows.length,
      rowCount: withRows.reduce((total, table) => total + (table.rows?.length ?? 0), 0),
      tables: withRows.map((table) => ({ name: table.name, rowCount: table.rows?.length ?? 0 })),
    };
  } catch (error) {
    throw new Error(`Excel 导入评测库失败：${redactEvaluationDatabaseError(error, target)}`);
  } finally {
    await client?.close().catch(() => undefined);
  }
}

export async function importEvaluationDatabaseExcel(
  input: { filename: string; buffer: Buffer; target: EvaluationDatabaseTarget },
  dependencies: EvaluationDatabaseDependencies = {},
): Promise<EvaluationDatabaseImportResult> {
  validateEvaluationDatabaseExcelUpload(input.filename, input.buffer);
  const tables = parseExcel(input.buffer, { maxRows: null });
  return importEvaluationDatabaseTables(input.target, tables, dependencies);
}
