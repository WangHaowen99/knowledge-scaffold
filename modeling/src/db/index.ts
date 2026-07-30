import { DatabaseSync } from 'node:sqlite';
import { readFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { config } from '../config.js';

// 用 Node 内置 node:sqlite（零原生编译）。API 近似 better-sqlite3：
// exec / prepare().run|get|all。
const here = dirname(fileURLToPath(import.meta.url));

let _db: DatabaseSync | null = null;

function ensureColumn(db: DatabaseSync, table: string, column: string, definition: string): void {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
  if (!columns.some((item) => item.name === column)) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
  }
}

export function getDb(): DatabaseSync {
  if (_db) return _db;
  mkdirSync(config.dataDir, { recursive: true });
  const db = new DatabaseSync(join(config.dataDir, 'modeling.db'));
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA busy_timeout = 5000');
  db.exec('PRAGMA foreign_keys = ON');
  // schema.sql 随源码走：dev 从 src/db 读，编译后 build 脚本拷到 dist/db。
  const schema = readFileSync(join(here, 'schema.sql'), 'utf8');
  db.exec(schema);
  // CREATE TABLE IF NOT EXISTS 不会给旧数据库补列；显式补齐向后兼容字段。
  ensureColumn(db, 'projects', 'kind', "TEXT NOT NULL DEFAULT 'modeling'");
  ensureColumn(db, 'package_versions', 'revision_oid', 'TEXT');
  ensureColumn(db, 'package_checks', 'artifact_revision_oid', 'TEXT');
  const competencyColumns = db.prepare('PRAGMA table_info(competency_questions)').all() as Array<{ name: string }>;
  if (!competencyColumns.some((column) => column.name === 'source')) {
    db.exec("ALTER TABLE competency_questions ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'");
  }
  const importColumns = db.prepare(
    'PRAGMA table_info(evaluation_database_import_runs)',
  ).all() as Array<{ name: string }>;
  if (importColumns.some((column) => column.name === 'engine')) {
    db.exec('ALTER TABLE evaluation_database_import_runs DROP COLUMN engine');
  }
  _db = db;
  return db;
}

export function nowIso(): string {
  return new Date().toISOString();
}
