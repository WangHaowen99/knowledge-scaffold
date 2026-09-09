import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-performance-stats-'));

const [{ createProject }, { getDb }, { appendTraceEvent }, { getPerformanceRun, listPerformanceRuns }] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/workflow/trace.js'),
  import('../src/workflow/performance-stats.js'),
]);

test('modeling performance summary aggregates trace and generation units', () => {
  const project = createProject({ owner: 'alice', name: '性能合成项目', runtime: 'agent' });
  const db = getDb();
  const now = new Date();
  const started = new Date(now.getTime() - 1000).toISOString();
  const finished = now.toISOString();
  db.prepare(`INSERT INTO wf_nodes (id, project_id, type, status, ordinal, title, created_at, updated_at)
    VALUES (?, ?, 'generate', 'done', 1, '生成', ?, ?)`).run('run-performance-1', project.id, started, finished);
  db.prepare(`INSERT INTO authoring_runs
    (id, project_id, intent, status, state_json, created_at, updated_at)
    VALUES (?, ?, '生成知识包', 'completed', '{}', ?, ?)`)
    .run('run-performance-1', project.id, started, finished);
  appendTraceEvent({ projectId: project.id, nodeId: 'run-performance-1', phase: 'generate', kind: 'tool_started', status: 'running', title: '读取表', details: { toolName: 'read_table_rows', cacheHits: 1, inputTokens: 10 } });
  appendTraceEvent({ projectId: project.id, nodeId: 'run-performance-1', phase: 'generate', kind: 'tool_completed', status: 'done', title: '读取表完成', details: { toolName: 'read_table_rows', outputTokens: 5 } });
  db.prepare(`INSERT INTO generation_batch_checkpoints
    (project_id, run_id, plan_hash, phase, batch_kind, batch_index, dependency_hash, status,
     output_json, issues_json, attempt_count, unit_id, target_ids_json, created_at, updated_at)
    VALUES (?, ?, 'plan', 'object:0', 'object', 1, '', 'completed', '{}', '[]', 2, 'unit-object-0', '["Customer"]', ?, ?)`)
    .run(project.id, 'run-performance-1', started, finished);

  const summary = getPerformanceRun(project.id, 'run-performance-1');
  assert.ok(summary);
  assert.equal(summary.eventCount, 2);
  assert.equal(summary.inputTokens, 10);
  assert.equal(summary.outputTokens, 5);
  assert.equal(summary.cacheHits, 1);
  assert.equal(summary.retryCount, 1);
  assert.equal(summary.units[0].unitId, 'unit-object-0');
  assert.equal(summary.units[0].retryCount, 1);
  assert.equal(listPerformanceRuns(project.id, 5).length, 1);
});
