import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'increment-milestone-'));

const { AiConstructionTaskGraph, INCREMENT_MILESTONE_ID } =
  await import('../src/ai-construction/task-graph.js');
const { ALL_TASK_KINDS, getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');

test('没做过增量的项目不会凭空多出一个阶段', () => {
  const project = createProject({ owner: 'alice', name: '未做增量' });
  const snapshot = new AiConstructionTaskGraph(getDb()).snapshot(project.id)!;
  assert.equal(snapshot.milestones.length, 6);
  assert.equal(snapshot.milestones.some((item) => item.id === INCREMENT_MILESTONE_ID), false);
  // 阶段数被带偏会连累进度百分比，所以这条要盯住
  assert.equal(snapshot.progress.totalMilestones, 6);
});

test('首次进入增量时才创建该阶段，排在完成确认之后', () => {
  const project = createProject({ owner: 'alice', name: '首次增量' });
  const graph = new AiConstructionTaskGraph(getDb());
  graph.ensureIncrementMilestone(project.id);

  const snapshot = graph.snapshot(project.id)!;
  const increment = snapshot.milestones.find((item) => item.id === INCREMENT_MILESTONE_ID);
  assert.ok(increment, '增量阶段应已创建');
  assert.equal(increment!.status, 'pending');
  const completed = snapshot.milestones.find((item) => item.id === 'construction_completed')!;
  assert.ok(increment!.ordinal > completed.ordinal, '增量阶段要排在建模完成确认之后');
});

test('反复进入增量不会重复创建', () => {
  const project = createProject({ owner: 'alice', name: '多轮增量' });
  const graph = new AiConstructionTaskGraph(getDb());
  graph.ensureIncrementMilestone(project.id);
  graph.ensureIncrementMilestone(project.id);
  graph.ensureIncrementMilestone(project.id);

  const count = getDb().prepare(`SELECT COUNT(*) AS n FROM ai_construction_milestones
    WHERE project_id = ? AND milestone_id = ?`).get(project.id, INCREMENT_MILESTONE_ID) as { n: number };
  assert.equal(count.n, 1);
});

test('存量项目不用迁移也能进入增量', () => {
  // 模拟一个只有 6 个阶段的老项目：懒创建正是为了免掉这类迁移
  const project = createProject({ owner: 'alice', name: '存量项目' });
  const db = getDb();
  assert.equal((db.prepare(`SELECT COUNT(*) AS n FROM ai_construction_milestones
    WHERE project_id = ?`).get(project.id) as { n: number }).n, 6);
  assert.doesNotThrow(() => new AiConstructionTaskGraph(db).ensureIncrementMilestone(project.id));
});

test('任务种类清单与库里的 CHECK 取值一致', () => {
  const row = getDb().prepare(`SELECT sql FROM sqlite_master
    WHERE type = 'table' AND name = 'ai_construction_tasks'`).get() as { sql: string };
  for (const kind of ALL_TASK_KINDS) {
    assert.ok(row.sql.includes(`'${kind}'`), `库里缺少任务种类 ${kind}，写入时会被 CHECK 拒绝`);
  }
});
