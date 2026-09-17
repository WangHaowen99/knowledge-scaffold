import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import { ensureAgentChangePlanPausedStatus } from '../src/db/index.js';

test('旧 agent_change_plans 表迁移后接受 paused 且保留原行', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`
    CREATE TABLE projects(id TEXT PRIMARY KEY);
    INSERT INTO projects(id) VALUES ('project-demo');
    CREATE TABLE agent_change_plans (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      run_id TEXT NOT NULL,
      goal TEXT NOT NULL,
      mode TEXT NOT NULL CHECK (mode IN ('expert_edit', 'generation')),
      status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'failed', 'cancelled')),
      approval_status TEXT NOT NULL DEFAULT 'approved'
        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
      approved_by TEXT,
      approved_at TEXT,
      version INTEGER NOT NULL DEFAULT 1,
      expected_revision TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    INSERT INTO agent_change_plans VALUES (
      'plan-demo', 'project-demo', 'run-demo', '合成建模计划', 'generation',
      'active', 'approved', 'operator', '2026-01-01', 2, 'revision-demo',
      '2026-01-01', '2026-01-01'
    );
  `);

  ensureAgentChangePlanPausedStatus(db);
  db.prepare(`UPDATE agent_change_plans SET status = 'paused' WHERE id = ?`).run('plan-demo');
  const row = db.prepare(`SELECT status, version FROM agent_change_plans WHERE id = ?`)
    .get('plan-demo') as { status: string; version: number };
  assert.equal(row.status, 'paused');
  assert.equal(row.version, 2);
  db.close();
});
