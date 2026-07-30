import { DatabaseSync } from 'node:sqlite';
import { readFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { config } from '../config.js';

// 用 Node 内置 node:sqlite（零原生编译）。API 近似 better-sqlite3：
// exec / prepare().run|get|all。
const here = dirname(fileURLToPath(import.meta.url));

let _db: DatabaseSync | null = null;

function hasTable(db: DatabaseSync, table: string): boolean {
  return Boolean(db.prepare(`SELECT 1 FROM sqlite_schema
    WHERE type = 'table' AND name = ?`).get(table));
}

function ensureColumn(db: DatabaseSync, table: string, column: string, definition: string): void {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
  if (!columns.some((item) => item.name === column)) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
  }
}

/**
 * 资料修订索引依赖新列，因此旧库必须在执行完整 schema 前先补列。
 */
export function ensureDocumentRevisionColumns(db: DatabaseSync): void {
  if (!hasTable(db, 'documents')) return;
  ensureColumn(db, 'documents', 'source_key', 'TEXT');
  ensureColumn(db, 'documents', 'revision_no', 'INTEGER NOT NULL DEFAULT 1');
  ensureColumn(db, 'documents', 'content_hash', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'documents', 'semantic_hash', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'documents', 'lifecycle_status', "TEXT NOT NULL DEFAULT 'active'");
  ensureColumn(db, 'documents', 'supersedes_id', 'TEXT');
  ensureColumn(db, 'documents', 'retired_at', 'TEXT');
  db.exec("UPDATE documents SET source_key = id WHERE source_key IS NULL OR source_key = ''");
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
  ensureDocumentRevisionColumns(db);
  db.exec(schema);
  // CREATE TABLE IF NOT EXISTS 不会给旧数据库补列；显式补齐向后兼容字段。
  ensureColumn(db, 'projects', 'kind', "TEXT NOT NULL DEFAULT 'modeling'");
  ensureColumn(db, 'package_versions', 'revision_oid', 'TEXT');
  ensureColumn(db, 'package_checks', 'artifact_revision_oid', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'artifact_revision_oid', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'phase', "TEXT NOT NULL DEFAULT 'workspace'");
  ensureColumn(db, 'eval_rounds', 'phase_started_at', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'current_item_started_at', 'TEXT');
  ensureColumn(db, 'eval_result_traces', 'tool_failures_json', "TEXT NOT NULL DEFAULT '[]'");
  ensureColumn(db, 'eval_result_traces', 'trace_status', "TEXT NOT NULL DEFAULT 'complete'");
  ensureColumn(db, 'eval_result_traces', 'trace_error', 'TEXT');
  ensureColumn(db, 'eval_results', 'original_verdict', 'TEXT');
  ensureColumn(db, 'eval_results', 'override_reason', 'TEXT');
  ensureColumn(db, 'eval_results', 'override_actor', 'TEXT');
  ensureColumn(db, 'eval_results', 'overridden_at', 'TEXT');
  ensureColumn(db, 'opt_suggestions', 'diagnosis_ids_json', "TEXT NOT NULL DEFAULT '[]'");
  ensureColumn(db, 'opt_suggestions', 'evidence_json', "TEXT NOT NULL DEFAULT '[]'");
  ensureColumn(db, 'opt_suggestions', 'generalization_plan', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'opt_suggestions', 'validation_plan', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'opt_suggestions', 'based_revision_oid', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'opt_suggestions', 'candidate_revision_id', 'TEXT');
  ensureColumn(db, 'opt_suggestions', 'candidate_feedback', 'TEXT');
  ensureColumn(db, 'opt_suggestions', 'applied_revision_oid', 'TEXT');
  ensureColumn(db, 'failure_diagnoses', 'based_revision_oid', "TEXT NOT NULL DEFAULT ''");
  ensureDocumentRevisionColumns(db);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_documents_source_history
    ON documents(project_id, source_key, revision_no)`);
  db.exec(`CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_source
    ON documents(project_id, source_key) WHERE lifecycle_status = 'active'`);
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
  db.exec(`UPDATE eval_rounds
    SET phase = status
    WHERE status IN ('done', 'failed') AND phase = 'workspace' AND phase_started_at IS NULL`);
  db.exec('UPDATE eval_rounds SET phase_started_at = created_at WHERE phase_started_at IS NULL');
  _db = db;
  return db;
}

export function nowIso(): string {
  return new Date().toISOString();
}
