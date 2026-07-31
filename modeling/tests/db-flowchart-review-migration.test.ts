import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';

import { ensureFlowchartReviewInteraction } from '../src/db/index.js';

test('旧数据库原地扩展流程图确认待办并保留既有记录与外键', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`PRAGMA foreign_keys = ON;
    CREATE TABLE projects (id TEXT PRIMARY KEY);
    CREATE TABLE authoring_runs (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE
    );
    CREATE TABLE authoring_interactions (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      run_id TEXT NOT NULL REFERENCES authoring_runs(id) ON DELETE CASCADE,
      type TEXT NOT NULL CHECK (type IN (
        'question_batch', 'change_plan', 'decision', 'artifact_review',
        'finding_review', 'knowledge_conflict', 'scenario_review'
      )),
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'cancelled')),
      base_revision TEXT,
      payload_json TEXT NOT NULL,
      response_id TEXT,
      response_json TEXT,
      created_at TEXT NOT NULL,
      responded_at TEXT
    );
    CREATE INDEX idx_authoring_interactions_pending
      ON authoring_interactions(project_id, status, created_at);
    CREATE INDEX idx_authoring_interactions_run
      ON authoring_interactions(run_id, created_at);
    CREATE UNIQUE INDEX idx_authoring_interactions_response
      ON authoring_interactions(project_id, response_id) WHERE response_id IS NOT NULL;
    CREATE TABLE project_events (
      id TEXT PRIMARY KEY,
      interaction_id TEXT REFERENCES authoring_interactions(id) ON DELETE SET NULL
    );
    INSERT INTO projects VALUES ('p1');
    INSERT INTO authoring_runs VALUES ('r1', 'p1');
    INSERT INTO authoring_interactions (
      id, project_id, run_id, type, payload_json, created_at
    ) VALUES ('i1', 'p1', 'r1', 'scenario_review', '{}', '2026-07-23T00:00:00.000Z');
    INSERT INTO project_events VALUES ('e1', 'i1');`);

  ensureFlowchartReviewInteraction(db);

  assert.equal((db.prepare('SELECT type FROM authoring_interactions WHERE id = ?').get('i1') as any).type, 'scenario_review');
  db.prepare(`INSERT INTO authoring_interactions (
    id, project_id, run_id, type, payload_json, created_at
  ) VALUES (?, ?, ?, ?, ?, ?)`).run(
    'i2', 'p1', 'r1', 'flowchart_review', '{}', '2026-07-23T00:01:00.000Z',
  );
  assert.equal((db.prepare('SELECT interaction_id FROM project_events WHERE id = ?').get('e1') as any).interaction_id, 'i1');
  assert.deepEqual(db.prepare('PRAGMA foreign_key_check').all(), []);
});
