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
 * 资料修订列必须在执行完整 schema 之前补齐。schema 会立即创建依赖
 * source_key/lifecycle_status 的索引，旧库需要先完成列迁移才能启动。
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

export function ensureFlowchartReviewInteraction(db: DatabaseSync): void {
  const row = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'authoring_interactions'`).get() as { sql?: string } | undefined;
  if (!row?.sql || row.sql.includes("'flowchart_review'")) return;

  db.exec('PRAGMA foreign_keys = OFF');
  try {
    db.exec(`BEGIN IMMEDIATE;
      CREATE TABLE authoring_interactions_next (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        run_id          TEXT NOT NULL REFERENCES authoring_runs(id) ON DELETE CASCADE,
        type            TEXT NOT NULL CHECK (type IN (
                          'question_batch', 'change_plan', 'decision', 'artifact_review',
                          'finding_review', 'knowledge_conflict', 'flowchart_review', 'scenario_review'
                        )),
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'answered', 'cancelled')),
        base_revision   TEXT,
        payload_json    TEXT NOT NULL,
        response_id     TEXT,
        response_json   TEXT,
        created_at      TEXT NOT NULL,
        responded_at    TEXT
      );
      INSERT INTO authoring_interactions_next (
        id, project_id, run_id, type, status, base_revision, payload_json,
        response_id, response_json, created_at, responded_at
      )
      SELECT
        id, project_id, run_id, type, status, base_revision, payload_json,
        response_id, response_json, created_at, responded_at
      FROM authoring_interactions;
      DROP TABLE authoring_interactions;
      ALTER TABLE authoring_interactions_next RENAME TO authoring_interactions;
      CREATE INDEX idx_authoring_interactions_pending
        ON authoring_interactions(project_id, status, created_at);
      CREATE INDEX idx_authoring_interactions_run
        ON authoring_interactions(run_id, created_at);
      CREATE UNIQUE INDEX idx_authoring_interactions_response
        ON authoring_interactions(project_id, response_id)
        WHERE response_id IS NOT NULL;
      COMMIT;`);
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
    throw error;
  } finally {
    db.exec('PRAGMA foreign_keys = ON');
  }
}

export function ensureEvaluationRepairTaskKind(db: DatabaseSync): void {
  const row = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'ai_construction_tasks'`).get() as { sql?: string } | undefined;
  if (!row?.sql || row.sql.includes("'evaluation_repair'")) return;

  db.exec('PRAGMA foreign_keys = OFF');
  try {
    db.exec(`BEGIN IMMEDIATE;
      CREATE TABLE ai_construction_tasks_next (
        id            TEXT PRIMARY KEY,
        project_id    TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
        task_kind     TEXT NOT NULL CHECK (task_kind IN (
                        'material_analysis', 'package_baseline_review', 'modeling_brief',
                        'process_interview', 'process_diagram', 'process_review',
                        'operation_extraction', 'knowledge_extraction', 'script_decision',
                        'script_generation', 'package_validation', 'package_repair',
                        'evaluation_repair', 'completion_review'
                      )),
        milestone_id  TEXT NOT NULL,
        ordinal       INTEGER NOT NULL,
        title         TEXT NOT NULL,
        goal          TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN (
                        'pending', 'ready', 'running', 'waiting_user', 'waiting_confirmation',
                        'completed', 'failed', 'skipped', 'stale', 'cancelled'
                      )),
        required      INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
        reason        TEXT,
        input_revision_oid TEXT,
        output_revision_oid TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        FOREIGN KEY (project_id, milestone_id)
          REFERENCES ai_construction_milestones(project_id, milestone_id) ON DELETE CASCADE,
        UNIQUE (project_id, ordinal)
      );
      INSERT INTO ai_construction_tasks_next (
        id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
        required, reason, input_revision_oid, output_revision_oid, created_at, updated_at
      )
      SELECT
        id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
        required, reason, input_revision_oid, output_revision_oid, created_at, updated_at
      FROM ai_construction_tasks;
      DROP TABLE ai_construction_tasks;
      ALTER TABLE ai_construction_tasks_next RENAME TO ai_construction_tasks;
      CREATE INDEX idx_ai_construction_tasks_project
        ON ai_construction_tasks(project_id, ordinal);
      COMMIT;`);
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
    throw error;
  } finally {
    db.exec('PRAGMA foreign_keys = ON');
  }
}

/**
 * 旧版按读取时机投影的任务图已经包含真实进度，只迁移来源标记。
 * 不重建任务图，避免改写既有任务、里程碑、确认记录和 revision。
 */
export function migrateLegacyAiConstructionGraphSources(db: DatabaseSync): number {
  if (!hasTable(db, 'ai_construction_graphs')) return 0;
  const result = db.prepare(`UPDATE ai_construction_graphs
    SET source = 'new'
    WHERE source = 'legacy_projection'`).run();
  return Number(result.changes ?? 0);
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
  migrateLegacyAiConstructionGraphSources(db);
  ensureFlowchartReviewInteraction(db);
  ensureEvaluationRepairTaskKind(db);
  // CREATE TABLE IF NOT EXISTS 不会给旧数据库补列；显式补齐向后兼容字段。
  ensureColumn(db, 'projects', 'kind', "TEXT NOT NULL DEFAULT 'modeling'");
  ensureColumn(db, 'projects', 'sensitivity_level', "TEXT NOT NULL DEFAULT 'standard'");
  ensureColumn(db, 'projects', 'workflow_profile_json', `TEXT NOT NULL DEFAULT '{"enabledSteps":[],"requiredSteps":["scenario","data_source"],"releaseRequested":true,"externalResearch":false}'`);
  ensureColumn(db, 'package_versions', 'revision_oid', 'TEXT');
  ensureColumn(db, 'package_drafts', 'base_revision_oid', 'TEXT');
  ensureColumn(db, 'package_checks', 'artifact_revision_oid', 'TEXT');
  ensureColumn(db, 'graph_import_uploads', 'package_revision_oid', 'TEXT');
  ensureColumn(db, 'graph_import_runs', 'package_revision_oid', 'TEXT');
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
  ensureColumn(db, 'failure_diagnoses', 'resolution_status', "TEXT NOT NULL DEFAULT 'pending'");
  ensureColumn(db, 'failure_diagnoses', 'resolution_action', 'TEXT');
  ensureColumn(db, 'failure_diagnoses', 'resolution_note', 'TEXT');
  ensureColumn(db, 'failure_diagnoses', 'resolution_actor', 'TEXT');
  ensureColumn(db, 'failure_diagnoses', 'resolution_payload_json', "TEXT NOT NULL DEFAULT '{}'");
  ensureColumn(db, 'failure_diagnoses', 'resolved_at', 'TEXT');
  const importColumns = db.prepare(
    'PRAGMA table_info(evaluation_database_import_runs)',
  ).all() as Array<{ name: string }>;
  if (importColumns.some((column) => column.name === 'engine')) {
    db.exec('ALTER TABLE evaluation_database_import_runs DROP COLUMN engine');
  }
  db.exec(`INSERT OR IGNORE INTO package_check_revisions (
      project_id, artifact_revision_oid, version, findings_json, created_at
    )
    SELECT project_id, artifact_revision_oid, version, findings_json, created_at
    FROM package_checks
    WHERE artifact_revision_oid IS NOT NULL AND artifact_revision_oid <> ''`);
  ensureDocumentRevisionColumns(db);
  db.exec(`CREATE INDEX IF NOT EXISTS idx_documents_source_history
    ON documents(project_id, source_key, revision_no)`);
  db.exec(`CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_source
    ON documents(project_id, source_key) WHERE lifecycle_status = 'active'`);
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
