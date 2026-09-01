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

function ensureDocumentPurposeAllowsTerm(db: DatabaseSync): void {
  const row = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'documents'`).get() as { sql?: string } | undefined;
  if (!row?.sql || row.sql.includes("'term'")) return;

  db.exec('PRAGMA foreign_keys = OFF');
  try {
    db.exec(`BEGIN IMMEDIATE;
      CREATE TABLE documents_next (
        id            TEXT PRIMARY KEY,
        project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        source_key    TEXT,
        revision_no   INTEGER NOT NULL DEFAULT 1,
        filename      TEXT NOT NULL,
        kind          TEXT NOT NULL,
        purpose       TEXT NOT NULL DEFAULT 'auto'
                      CHECK (purpose IN ('auto', 'goal', 'schema', 'evidence', 'term')),
        reading_mode  TEXT NOT NULL DEFAULT 'on_demand'
                      CHECK (reading_mode IN ('on_demand', 'full')),
        directive_revision INTEGER NOT NULL DEFAULT 0,
        parse_status  TEXT NOT NULL DEFAULT 'pending',
        parse_error   TEXT,
        text_content  TEXT,
        tables_json   TEXT,
        raw_content   BLOB,
        content_hash  TEXT NOT NULL DEFAULT '',
        semantic_hash TEXT NOT NULL DEFAULT '',
        lifecycle_status TEXT NOT NULL DEFAULT 'active'
                      CHECK (lifecycle_status IN ('active', 'superseded', 'deleted')),
        supersedes_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
        created_at    TEXT NOT NULL,
        retired_at    TEXT
      );
      INSERT INTO documents_next (
        id, project_id, source_key, revision_no, filename, kind, purpose, reading_mode,
        directive_revision, parse_status, parse_error, text_content, tables_json, raw_content,
        content_hash, semantic_hash, lifecycle_status, supersedes_id, created_at, retired_at
      )
      SELECT
        id, project_id, source_key, revision_no, filename, kind, purpose, reading_mode,
        directive_revision, parse_status, parse_error, text_content, tables_json, raw_content,
        content_hash, semantic_hash, lifecycle_status, supersedes_id, created_at, retired_at
      FROM documents;
      DROP TABLE documents;
      ALTER TABLE documents_next RENAME TO documents;
      CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
      CREATE INDEX IF NOT EXISTS idx_documents_source_history
        ON documents(project_id, source_key, revision_no);
      CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_source
        ON documents(project_id, source_key) WHERE lifecycle_status = 'active';
      COMMIT;`);
  } catch (error) {
    try { db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
    throw error;
  } finally {
    db.exec('PRAGMA foreign_keys = ON');
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
  ensureColumn(db, 'documents', 'raw_content', 'BLOB');
  ensureColumn(db, 'documents', 'purpose', "TEXT NOT NULL DEFAULT 'auto'");
  ensureColumn(db, 'documents', 'reading_mode', "TEXT NOT NULL DEFAULT 'on_demand'");
  ensureColumn(db, 'documents', 'directive_revision', 'INTEGER NOT NULL DEFAULT 0');
  ensureDocumentPurposeAllowsTerm(db);
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
 * 让 task_kind 的 CHECK 取值与代码里的全量清单对齐。
 *
 * 原先每新增一种就复制一遍整段重建逻辑（evaluation_repair 一次、
 * material_change_review 一次），第三种再复制就该收口了。这里按清单比对：
 * 少任何一种就重建，全都在就直接返回。
 */
export const ALL_TASK_KINDS = [
  'material_analysis', 'package_baseline_review', 'modeling_brief',
  'process_interview', 'process_diagram', 'process_review',
  'operation_extraction', 'knowledge_extraction', 'script_decision',
  'script_generation', 'package_validation', 'package_repair',
  'evaluation_repair', 'completion_review', 'material_change_review',
  'incremental_intake',
] as const;

export function ensureTaskKindCoverage(db: DatabaseSync): void {
  const row = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'ai_construction_tasks'`).get() as { sql?: string } | undefined;
  if (!row?.sql) return;
  if (ALL_TASK_KINDS.every((kind) => row.sql!.includes(`'${kind}'`))) return;

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
                        ${ALL_TASK_KINDS.map((kind) => `'${kind}'`).join(', ')}
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

/** 扩展 Core -> Agent outbox 的封闭事件类型，同时保留旧库中的未投递事件。 */
export function ensureAgentProjectEventKinds(db: DatabaseSync): void {
  const row = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'agent_project_event_outbox'`).get() as { sql?: string } | undefined;
  if (!row?.sql || row.sql.includes("'evaluation_terminal'")) return;

  db.exec('PRAGMA foreign_keys = OFF');
  try {
    db.exec(`BEGIN IMMEDIATE;
      CREATE TABLE agent_project_event_outbox_next (
        id            TEXT PRIMARY KEY,
        project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        event_type    TEXT NOT NULL CHECK (event_type IN ('material_parse', 'studio_review', 'fusion_review', 'package_import', 'evaluation_terminal')),
        payload_json  TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        delivered_at TEXT
      );
      INSERT INTO agent_project_event_outbox_next (
        id, project_id, event_type, payload_json, created_at, delivered_at
      ) SELECT id, project_id, event_type, payload_json, created_at, delivered_at
        FROM agent_project_event_outbox;
      DROP TABLE agent_project_event_outbox;
      ALTER TABLE agent_project_event_outbox_next RENAME TO agent_project_event_outbox;
      CREATE INDEX idx_agent_project_event_outbox_pending
        ON agent_project_event_outbox(project_id, delivered_at, created_at);
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
  ensureAgentProjectEventKinds(db);
  ensureFlowchartReviewInteraction(db);
  ensureEvaluationRepairTaskKind(db);
  ensureTaskKindCoverage(db);
  // CREATE TABLE IF NOT EXISTS 不会给旧数据库补列；显式补齐向后兼容字段。
  ensureColumn(db, 'projects', 'kind', "TEXT NOT NULL DEFAULT 'modeling'");
  ensureColumn(db, 'projects', 'runtime', "TEXT NOT NULL DEFAULT 'legacy'");
  ensureColumn(db, 'projects', 'sensitivity_level', "TEXT NOT NULL DEFAULT 'standard'");
  ensureColumn(db, 'projects', 'workflow_profile_json', `TEXT NOT NULL DEFAULT '{"enabledSteps":[],"requiredSteps":["scenario","data_source"],"releaseRequested":true,"externalResearch":false}'`);
  // 旧库的用户 LLM 配置表可能没有上下文窗口列；保留 NULL 以继续回退部署默认值。
  ensureColumn(db, 'user_llm_settings', 'context_window', 'INTEGER');
  // 平台默认与用户快照表在旧版本中也可能缺少独立上下文窗口。
  ensureColumn(db, 'llm_default_settings', 'context_window', 'INTEGER NOT NULL DEFAULT 1000000');
  ensureColumn(db, 'user_llm_default_snapshots', 'context_window', 'INTEGER NOT NULL DEFAULT 1000000');
  ensureColumn(db, 'package_versions', 'revision_oid', 'TEXT');
  // 旧库已有 inflight_operations 时 CREATE TABLE IF NOT EXISTS 不会补列
  ensureColumn(db, 'inflight_operations', 'cancel_requested', 'INTEGER NOT NULL DEFAULT 0');
  ensureColumn(db, 'agent_candidate_workspaces', 'credential_hash', 'TEXT');
  ensureColumn(db, 'agent_candidate_workspaces', 'credential_expires_at', 'TEXT');
  ensureColumn(db, 'agent_candidate_workspaces', 'package_data_source_type', "TEXT NOT NULL DEFAULT 'neo4j'");
  ensureColumn(db, 'agent_change_plans', 'approval_status', "TEXT NOT NULL DEFAULT 'approved'");
  ensureColumn(db, 'agent_change_plans', 'approved_by', 'TEXT');
  ensureColumn(db, 'agent_change_plans', 'approved_at', 'TEXT');
  ensureColumn(db, 'package_drafts', 'base_revision_oid', 'TEXT');
  ensureColumn(db, 'package_checks', 'artifact_revision_oid', 'TEXT');
  ensureColumn(db, 'generation_batch_checkpoints', 'dependency_hash', "TEXT NOT NULL DEFAULT ''");
  ensureColumn(db, 'generation_batch_checkpoints', 'invalidated_at', 'TEXT');
  ensureColumn(db, 'generation_batch_checkpoints', 'invalidated_reason', 'TEXT');
  ensureColumn(db, 'generation_batch_checkpoints', 'invalidated_by_rollback_id', 'TEXT');
  ensureColumn(db, 'package_repair_attempts', 'execution_status', "TEXT NOT NULL DEFAULT 'pending'");
  ensureColumn(db, 'package_repair_attempts', 'quality_outcome', "TEXT NOT NULL DEFAULT 'unknown_legacy'");
  ensureColumn(db, 'package_repair_attempts', 'target_fingerprints_json', "TEXT NOT NULL DEFAULT '[]'");
  ensureColumn(db, 'package_repair_attempts', 'resolved_fingerprints_json', "TEXT NOT NULL DEFAULT '[]'");
  ensureColumn(db, 'package_repair_attempts', 'introduced_fingerprints_json', "TEXT NOT NULL DEFAULT '[]'");
  ensureColumn(db, 'package_repair_attempts', 'before_blocking_count', 'INTEGER NOT NULL DEFAULT 0');
  ensureColumn(db, 'package_repair_attempts', 'after_blocking_count', 'INTEGER');
  ensureColumn(db, 'package_repair_attempts', 'candidate_revision_oid', 'TEXT');
  ensureColumn(db, 'package_repair_attempts', 'quality_gate_json', "TEXT NOT NULL DEFAULT '{}'");
  ensureColumn(db, 'package_repair_attempts', 'retry_count', 'INTEGER NOT NULL DEFAULT 0');
  ensureColumn(db, 'package_repair_attempts', 'next_action_json', 'TEXT');
  db.exec(`UPDATE package_repair_attempts SET execution_status = CASE status
      WHEN 'running' THEN 'running'
      WHEN 'waiting_user' THEN 'waiting_user'
      WHEN 'completed' THEN 'finished'
      WHEN 'stopped' THEN 'finished'
      ELSE 'failed'
    END
    WHERE execution_status = 'pending'`);
  db.exec(`UPDATE package_repair_attempts SET quality_outcome = CASE status
      WHEN 'stopped' THEN 'rejected'
      WHEN 'failed' THEN 'unresolved'
      ELSE quality_outcome
    END
    WHERE quality_outcome = 'unknown_legacy' AND status <> 'completed'`);
  ensureColumn(db, 'ai_construction_confirmations', 'invalidated_at', 'TEXT');
  ensureColumn(db, 'ai_construction_confirmations', 'invalidated_by_rollback_id', 'TEXT');
  ensureColumn(db, 'ai_construction_milestone_rollbacks', 'current_package_revision_oid', 'TEXT NOT NULL DEFAULT \'\'');
  ensureColumn(db, 'graph_import_uploads', 'package_revision_oid', 'TEXT');
  ensureColumn(db, 'graph_import_runs', 'package_revision_oid', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'artifact_revision_oid', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'phase', "TEXT NOT NULL DEFAULT 'workspace'");
  ensureColumn(db, 'eval_rounds', 'phase_started_at', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'current_item_started_at', 'TEXT');
  ensureColumn(db, 'eval_rounds', 'evaluation_scope_json', `TEXT NOT NULL DEFAULT '{"kind":"current"}'`);
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
