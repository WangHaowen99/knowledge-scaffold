import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

import { recoverInterruptedAuthoringWork } from '../src/authoring/restart-recovery.js';

const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');
const NOW = '2026-07-23T08:00:00.000Z';

function createDb(): DatabaseSync {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  db.prepare(`INSERT INTO projects
    (id, owner, name, created_at, updated_at) VALUES ('pj_restart', 'tester', '重启恢复', ?, ?)`)
    .run(NOW, NOW);
  db.prepare(`INSERT INTO ai_construction_graphs
    (project_id, revision, status, source, created_at, updated_at)
    VALUES ('pj_restart', 1, 'active', 'new', ?, ?)`)
    .run(NOW, NOW);
  db.prepare(`INSERT INTO ai_construction_milestones
    (project_id, milestone_id, ordinal, title, status, updated_at)
    VALUES ('pj_restart', 'materials_ready', 1, '建模资料已理解', 'active', ?)`)
    .run(NOW);
  return db;
}

test('服务重启会结束运行中任务，并保留等待用户的任务和交互', () => {
  const db = createDb();
  const insertRun = db.prepare(`INSERT INTO authoring_runs
    (id, project_id, intent, status, state_json, created_at, updated_at)
    VALUES (?, 'pj_restart', ?, ?, '{}', ?, ?)`);
  insertRun.run('run_queued', '排队任务', 'queued', NOW, NOW);
  insertRun.run('run_running', '运行任务', 'running', NOW, NOW);
  insertRun.run('run_waiting', '等待用户', 'waiting', NOW, NOW);

  db.prepare(`INSERT INTO authoring_interactions
    (id, project_id, run_id, type, status, payload_json, created_at)
    VALUES ('interaction_waiting', 'pj_restart', 'run_waiting', 'decision', 'pending', '{}', ?)`)
    .run(NOW);

  const insertTask = db.prepare(`INSERT INTO ai_construction_tasks
    (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required, created_at, updated_at)
    VALUES (?, 'pj_restart', 'material_analysis', 'materials_ready', ?, ?, '', ?, 1, ?, ?)`);
  insertTask.run('task_running', 1, '运行任务', 'running', NOW, NOW);
  insertTask.run('task_waiting', 2, '等待回答', 'waiting_user', NOW, NOW);
  insertTask.run('task_confirming', 3, '等待确认', 'waiting_confirmation', NOW, NOW);

  db.prepare(`INSERT INTO ai_construction_messages
    (id, project_id, task_id, message_key, seq, role, blocks_json, created_at)
    VALUES ('message_analysis', 'pj_restart', 'task_running', 'analysis.progress.test', 1, 'assistant', ?, ?)`)
    .run(JSON.stringify([
      { type: 'status', title: '正在分析第 2/4 批资料', summary: '对象与操作候选' },
      { type: 'text', text: '当前正在分析资料。' },
    ]), NOW);

  db.prepare(`INSERT INTO wf_nodes
    (id, project_id, type, status, ordinal, title, output_json, created_at, updated_at)
    VALUES ('node_running', 'pj_restart', 'generate', 'running', 1, '知识包生成', '{"kept":true}', ?, ?)`)
    .run(NOW, NOW);
  db.prepare(`INSERT INTO project_operation_leases
    (project_id, operation, owner, token, acquired_at, updated_at, expires_at)
    VALUES ('pj_restart', '执行知识生成', 'tester:req-old', 'lease_old', ?, ?, '2026-07-23T09:00:00.000Z')`)
    .run(NOW, NOW);

  const result = recoverInterruptedAuthoringWork(db, {
    now: '2026-07-23T08:05:00.000Z',
    reason: '服务重启，任务已中断，请重新执行当前步骤',
  });

  assert.deepEqual(result, {
    authoringRuns: 2,
    constructionTasks: 1,
    workflowNodes: 1,
    projectLeases: 1,
  });
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM project_operation_leases').get().count, 0);

  const runs = db.prepare(`SELECT id, status, error, finished_at FROM authoring_runs ORDER BY id`).all() as any[];
  assert.deepEqual(runs.map(({ id, status }) => ({ id, status })), [
    { id: 'run_queued', status: 'failed' },
    { id: 'run_running', status: 'failed' },
    { id: 'run_waiting', status: 'waiting' },
  ]);
  assert.equal(runs[0]?.error, '服务重启，任务已中断，请重新执行当前步骤');
  assert.equal(runs[1]?.finished_at, '2026-07-23T08:05:00.000Z');
  const interaction = db.prepare("SELECT status FROM authoring_interactions WHERE id = 'interaction_waiting'").get() as any;
  assert.equal(interaction.status, 'pending');

  const tasks = db.prepare(`SELECT id, status, reason FROM ai_construction_tasks ORDER BY ordinal`).all() as any[];
  assert.deepEqual(tasks.map(({ id, status }) => ({ id, status })), [
    { id: 'task_running', status: 'failed' },
    { id: 'task_waiting', status: 'waiting_user' },
    { id: 'task_confirming', status: 'waiting_confirmation' },
  ]);
  assert.equal(tasks[0]?.reason, '服务重启，任务已中断，请重新执行当前步骤');

  const message = db.prepare(`SELECT blocks_json FROM ai_construction_messages
    WHERE id = 'message_analysis'`).get() as any;
  assert.deepEqual(JSON.parse(message.blocks_json), [
    {
      type: 'status',
      title: '建模资料分析已停止',
      summary: '当前进度已保留，可以直接重试',
      milestoneId: 'materials_ready',
      taskId: 'task_running',
      progress: 0,
      eta: { minMinutes: null, maxMinutes: null, confidence: 'low' },
    },
    { type: 'text', text: '服务重启，任务已中断，请重新执行当前步骤' },
    {
      type: 'next_actions',
      title: '可以这样继续',
      actions: [
        { id: 'analyze_materials', label: '重新分析资料', style: 'primary' },
        { id: 'upload_materials', label: '补充建模资料', style: 'secondary' },
      ],
    },
  ]);

  const node = db.prepare(`SELECT status, reason, output_json FROM wf_nodes WHERE id = 'node_running'`).get() as any;
  assert.equal(node.status, 'failed');
  assert.equal(node.reason, '服务重启，任务已中断，请重新执行当前步骤');
  assert.deepEqual(JSON.parse(node.output_json), { kept: true });

});

test('恢复操作可重复执行且不会改写已结束状态', () => {
  const db = createDb();
  db.prepare(`INSERT INTO authoring_runs
    (id, project_id, intent, status, state_json, error, created_at, updated_at, finished_at)
    VALUES ('run_done', 'pj_restart', '已完成', 'completed', '{}', NULL, ?, ?, ?)`)
    .run(NOW, NOW, NOW);

  const first = recoverInterruptedAuthoringWork(db, { now: NOW });
  const second = recoverInterruptedAuthoringWork(db, { now: NOW });

  assert.deepEqual(first, {
    authoringRuns: 0,
    constructionTasks: 0,
    workflowNodes: 0,
    projectLeases: 0,
  });
  assert.deepEqual(second, first);
  assert.equal((db.prepare(`SELECT status FROM authoring_runs WHERE id = 'run_done'`).get() as any).status, 'completed');
});

test('服务启动会修正此前已中断但仍显示进行中的资料分析消息', () => {
  const db = createDb();
  db.prepare(`INSERT INTO ai_construction_tasks
    (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required, reason, created_at, updated_at)
    VALUES ('task_interrupted', 'pj_restart', 'material_analysis', 'materials_ready', 1,
      '资料分析', '', 'failed', 1, '服务重启，任务已中断，请重新执行当前步骤', ?, ?)`)
    .run(NOW, NOW);
  db.prepare(`INSERT INTO ai_construction_messages
    (id, project_id, task_id, message_key, seq, role, blocks_json, created_at)
    VALUES ('message_stale', 'pj_restart', 'task_interrupted', 'analysis.progress.old', 1,
      'assistant', ?, ?)`)
    .run(JSON.stringify([
      { type: 'status', title: '正在分析第 3/4 批资料', summary: '正在整理候选' },
      { type: 'text', text: '当前正在分析资料。' },
    ]), NOW);

  const result = recoverInterruptedAuthoringWork(db, { now: NOW });

  assert.equal(result.constructionTasks, 0);
  const blocks = JSON.parse((db.prepare(`SELECT blocks_json FROM ai_construction_messages
    WHERE id = 'message_stale'`).get() as any).blocks_json);
  assert.equal(blocks[0].title, '建模资料分析已停止');
  assert.equal(blocks[1].text, '服务重启，任务已中断，请重新执行当前步骤');
  assert.deepEqual(blocks[2].actions.map((action: any) => action.id), [
    'analyze_materials', 'upload_materials',
  ]);
});
