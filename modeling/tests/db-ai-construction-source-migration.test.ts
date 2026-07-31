import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import { migrateLegacyAiConstructionGraphSources } from '../src/db/index.js';

function legacyDatabase(): DatabaseSync {
  const db = new DatabaseSync(':memory:');
  db.exec(`
    CREATE TABLE ai_construction_graphs (
      project_id TEXT PRIMARY KEY,
      revision INTEGER NOT NULL,
      status TEXT NOT NULL,
      source TEXT NOT NULL CHECK (source IN ('new', 'legacy_projection', 'package_import')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE ai_construction_milestones (
      project_id TEXT NOT NULL,
      milestone_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      title TEXT NOT NULL,
      status TEXT NOT NULL,
      confirmed_revision_oid TEXT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (project_id, milestone_id)
    );
    CREATE TABLE ai_construction_tasks (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      task_kind TEXT NOT NULL,
      milestone_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      title TEXT NOT NULL,
      goal TEXT NOT NULL,
      status TEXT NOT NULL,
      required INTEGER NOT NULL,
      reason TEXT,
      input_revision_oid TEXT,
      output_revision_oid TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    INSERT INTO ai_construction_graphs
      (project_id, revision, status, source, created_at, updated_at)
    VALUES
      ('pj_modeling', 7, 'active', 'legacy_projection', 'created-modeling', 'updated-modeling'),
      ('pj_package', 3, 'active', 'package_import', 'created-package', 'updated-package');
    INSERT INTO ai_construction_milestones
      (project_id, milestone_id, ordinal, title, status, confirmed_revision_oid, updated_at)
    VALUES
      ('pj_modeling', 'materials_ready', 1, '资料就绪', 'confirmed', 'oid-materials', 'milestone-time');
    INSERT INTO ai_construction_tasks
      (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required,
       reason, input_revision_oid, output_revision_oid, created_at, updated_at)
    VALUES
      ('task_existing', 'pj_modeling', 'package_baseline_review', 'materials_ready', 1,
       '检查历史项目基线', '保留已有任务', 'completed', 1,
       '历史任务', 'oid-input', 'oid-output', 'task-created', 'task-updated');
  `);
  return db;
}

test('启动迁移只统一旧任务图来源并完整保留既有图数据', () => {
  const db = legacyDatabase();
  const graphBefore = db.prepare(`SELECT * FROM ai_construction_graphs
    WHERE project_id = 'pj_modeling'`).get() as Record<string, unknown>;
  const milestoneBefore = db.prepare(`SELECT * FROM ai_construction_milestones
    WHERE project_id = 'pj_modeling'`).get();
  const taskBefore = db.prepare(`SELECT * FROM ai_construction_tasks
    WHERE project_id = 'pj_modeling'`).get();

  assert.equal(migrateLegacyAiConstructionGraphSources(db), 1);

  const graphAfter = db.prepare(`SELECT * FROM ai_construction_graphs
    WHERE project_id = 'pj_modeling'`).get() as Record<string, unknown>;
  assert.deepEqual({ ...graphAfter }, { ...graphBefore, source: 'new' });
  assert.deepEqual(db.prepare(`SELECT * FROM ai_construction_milestones
    WHERE project_id = 'pj_modeling'`).get(), milestoneBefore);
  assert.deepEqual(db.prepare(`SELECT * FROM ai_construction_tasks
    WHERE project_id = 'pj_modeling'`).get(), taskBefore);
  assert.equal((db.prepare(`SELECT source FROM ai_construction_graphs
    WHERE project_id = 'pj_package'`).get() as { source: string }).source, 'package_import');
  assert.equal(migrateLegacyAiConstructionGraphSources(db), 0, '重复启动迁移应保持幂等');

  db.close();
});
