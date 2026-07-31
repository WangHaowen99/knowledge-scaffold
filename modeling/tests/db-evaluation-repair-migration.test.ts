import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { ensureEvaluationRepairTaskKind } from '../src/db/index.js';

/** 复刻加入 evaluation_repair 之前的表结构（含被外键引用的表，重建后外键仍须成立） */
function createLegacyTasksTable(db: DatabaseSync): void {
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(`
    CREATE TABLE ai_construction_graphs (
      project_id TEXT PRIMARY KEY,
      revision   INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE ai_construction_milestones (
      project_id   TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
      milestone_id TEXT NOT NULL,
      PRIMARY KEY (project_id, milestone_id)
    );
    INSERT INTO ai_construction_graphs (project_id) VALUES ('pj-legacy');
    INSERT INTO ai_construction_milestones (project_id, milestone_id)
      VALUES ('pj-legacy', 'knowledge_confirmed'), ('pj-legacy', 'construction_completed');
    CREATE TABLE ai_construction_tasks (
      id            TEXT PRIMARY KEY,
      project_id    TEXT NOT NULL,
      task_kind     TEXT NOT NULL CHECK (task_kind IN (
                      'material_analysis', 'package_baseline_review', 'modeling_brief',
                      'process_interview', 'process_diagram', 'process_review',
                      'operation_extraction', 'knowledge_extraction', 'script_decision',
                      'script_generation', 'package_validation', 'package_repair',
                      'completion_review'
                    )),
      milestone_id  TEXT NOT NULL,
      ordinal       INTEGER NOT NULL,
      title         TEXT NOT NULL,
      goal          TEXT NOT NULL DEFAULT '',
      status        TEXT NOT NULL DEFAULT 'pending',
      required      INTEGER NOT NULL DEFAULT 1,
      reason        TEXT,
      input_revision_oid TEXT,
      output_revision_oid TEXT,
      created_at    TEXT NOT NULL,
      updated_at    TEXT NOT NULL,
      UNIQUE (project_id, ordinal)
    );
    CREATE INDEX idx_ai_construction_tasks_project
      ON ai_construction_tasks(project_id, ordinal);
    INSERT INTO ai_construction_tasks
      (id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
       required, reason, input_revision_oid, output_revision_oid, created_at, updated_at)
    VALUES
      ('t1', 'pj-legacy', 'package_repair', 'knowledge_confirmed', 1, '修复', '目标', 'waiting_user',
       0, '既有原因', 'rev-in', NULL, '2026-01-01T00:00:00.000Z', '2026-01-02T00:00:00.000Z'),
      ('t2', 'pj-legacy', 'completion_review', 'construction_completed', 2, '检查', '目标', 'completed',
       1, NULL, NULL, 'rev-out', '2026-01-01T00:00:00.000Z', '2026-01-03T00:00:00.000Z');
  `);
}

function openTempDb(): DatabaseSync {
  return new DatabaseSync(join(mkdtempSync(join(tmpdir(), 'eval-repair-migration-')), 'test.db'));
}

test('旧库原地升级后接受 evaluation_repair 且不丢任务数据', () => {
  const db = openTempDb();
  createLegacyTasksTable(db);
  assert.throws(() => db.prepare(`INSERT INTO ai_construction_tasks
    (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required,
     created_at, updated_at)
    VALUES ('t3', 'pj-legacy', 'evaluation_repair', 'knowledge_confirmed', 3, 'x', 'y', 'pending', 0,
     '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')`).run());

  ensureEvaluationRepairTaskKind(db);

  const rows = db.prepare('SELECT * FROM ai_construction_tasks ORDER BY ordinal').all() as any[];
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.id), ['t1', 't2']);
  assert.equal(rows[0].task_kind, 'package_repair');
  assert.equal(rows[0].reason, '既有原因');
  assert.equal(rows[0].input_revision_oid, 'rev-in');
  assert.equal(rows[0].required, 0);
  assert.equal(rows[1].output_revision_oid, 'rev-out');
  assert.equal(rows[1].updated_at, '2026-01-03T00:00:00.000Z');

  db.prepare(`INSERT INTO ai_construction_tasks
    (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required,
     created_at, updated_at)
    VALUES ('t3', 'pj-legacy', 'evaluation_repair', 'knowledge_confirmed', 3, 'x', 'y', 'pending', 0,
     '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')`).run();
  assert.equal(
    (db.prepare("SELECT COUNT(*) AS n FROM ai_construction_tasks WHERE task_kind = 'evaluation_repair'")
      .get() as any).n,
    1,
  );

  // 索引必须随重建恢复，否则任务列表查询会退化成全表扫描
  const indexes = db.prepare("PRAGMA index_list('ai_construction_tasks')").all() as any[];
  assert.ok(indexes.some((item) => item.name === 'idx_ai_construction_tasks_project'));

  // 外键要跟着重建后的表继续生效
  assert.throws(() => db.prepare(`INSERT INTO ai_construction_tasks
    (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required,
     created_at, updated_at)
    VALUES ('t4', 'pj-legacy', 'evaluation_repair', 'milestone-does-not-exist', 4, 'x', 'y', 'pending', 0,
     '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')`).run());
  db.close();
});

test('迁移可重入：已升级的库再执行是空操作', () => {
  const db = openTempDb();
  createLegacyTasksTable(db);
  ensureEvaluationRepairTaskKind(db);
  const before = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'ai_construction_tasks'`).get() as { sql: string };

  ensureEvaluationRepairTaskKind(db);

  const after = db.prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'ai_construction_tasks'`).get() as { sql: string };
  assert.equal(after.sql, before.sql);
  assert.equal((db.prepare('SELECT COUNT(*) AS n FROM ai_construction_tasks').get() as any).n, 2);
  db.close();
});
