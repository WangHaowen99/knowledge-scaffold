import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'task-progress-'));

const { remainingMinutesFromBatches, reportLongTaskProgress } =
  await import('../src/ai-construction/task-progress.js');
const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');
const { STALL_THRESHOLD_MS, lastProgressAt, stallHint, stalledForMs } =
  await import('../frontend/src/taskStall.js');

test('按实测批次速度推算剩余，比静态区间准得多', () => {
  // 5 批里跑完 2 批用了 10 分钟 → 剩 3 批约 15 分钟
  assert.deepEqual(remainingMinutesFromBatches(2, 5, 10 * 60_000), { minMinutes: 12, maxMinutes: 21 });
});

test('样本不足或已跑完时不硬给估算', () => {
  assert.equal(remainingMinutesFromBatches(0, 5, 60_000), null, '一批都没跑完时没有速度可依据');
  assert.equal(remainingMinutesFromBatches(5, 5, 60_000), null, '跑完了不需要估剩余');
  assert.equal(remainingMinutesFromBatches(2, 5, 0), null);
});

test('进度写成可就地更新的消息，不会在时间线里堆几十条', () => {
  const project = createProject({ owner: 'alice', name: '进度就地更新' });
  const db = getDb();
  const snapshot = new AiConstructionTaskGraph(db).snapshot(project.id)!;
  const taskId = snapshot.tasks[0].id;
  db.prepare(`UPDATE ai_construction_tasks
    SET status = 'running', task_kind = 'knowledge_extraction' WHERE id = ?`).run(taskId);

  for (const done of [1, 2, 3]) {
    reportLongTaskProgress(project.id, {
      taskKinds: ['knowledge_extraction'],
      title: `正在生成第 ${done}/5 批业务对象`,
      produced: `已产出 ${done * 4} 个对象`,
      done, total: 5, elapsedMs: done * 60_000,
    });
  }

  const rows = db.prepare(
    'SELECT blocks_json FROM ai_construction_messages WHERE project_id = ? AND message_key = ?',
  ).all(project.id, `progress.task.${taskId}`) as Array<{ blocks_json: string }>;
  assert.equal(rows.length, 1, '三次上报应该更新同一条消息');
  // 留下的是最后一次的内容，且带上「已经产出什么」——用户真正关心的是这个
  assert.match(rows[0].blocks_json, /第 3\/5 批/);
  assert.match(rows[0].blocks_json, /已产出 12 个对象/);
});

test('没有正在执行的任务时不写垃圾消息', () => {
  const project = createProject({ owner: 'alice', name: '无运行任务' });
  reportLongTaskProgress(project.id, {
    taskKinds: ['knowledge_extraction'], title: 'x', done: 1, total: 5, elapsedMs: 1_000,
  });
  const count = getDb().prepare(
    "SELECT COUNT(*) AS n FROM ai_construction_messages WHERE project_id = ? AND message_key LIKE 'progress.task.%'",
  ).get(project.id) as { n: number };
  assert.equal(count.n, 0);
});

test('进度上报失败不能拖垮任务本身', () => {
  // 项目不存在：内部查询会失败，但不应抛出
  assert.doesNotThrow(() => reportLongTaskProgress('pj_missing', {
    taskKinds: ['knowledge_extraction'], title: 'x', done: 1, total: 2, elapsedMs: 1_000,
  }));
});

// ---- 停滞判定 ----

const NOW = Date.parse('2026-08-03T10:00:00.000Z');
const ago = (ms: number) => new Date(NOW - ms).toISOString();

test('长时间没有新进展才提示，正常推进不打扰', () => {
  const messages = [{ taskId: 't1', createdAt: ago(2 * 60_000) }];
  assert.equal(stalledForMs({ messages, taskId: 't1', now: NOW }), null);
  const stale = [{ taskId: 't1', createdAt: ago(STALL_THRESHOLD_MS + 60_000) }];
  assert.ok((stalledForMs({ messages: stale, taskId: 't1', now: NOW }) ?? 0) > STALL_THRESHOLD_MS);
});

test('还没有任何进度时按任务开始时间算', () => {
  assert.equal(stalledForMs({ messages: [], taskId: 't1', startedAt: ago(60_000), now: NOW }), null);
  assert.ok(stalledForMs({
    messages: [], taskId: 't1', startedAt: ago(STALL_THRESHOLD_MS + 1_000), now: NOW,
  }));
});

test('连开始时间都不知道就不提示，宁可不说也不要误报成卡住', () => {
  assert.equal(stalledForMs({ messages: [], taskId: 't1', now: NOW }), null);
  assert.equal(stalledForMs({ messages: [], now: NOW }), null);
  assert.equal(lastProgressAt({ messages: [{ taskId: 't1', createdAt: 'x' }], taskId: 't1' }), null);
});

test('完全没有进度上报的任务，按开始时间也能判出停滞', () => {
  // 脚本方案生成就是这种情况：整段只有开始和结束两个点
  assert.ok(stalledForMs({ messages: [], startedAt: ago(STALL_THRESHOLD_MS + 1_000), now: NOW }));
});

test('提示语说明可能仍在处理，并指向取消而不是判死刑', () => {
  const hint = stallHint(9 * 60_000);
  assert.match(hint, /9 分钟/);
  assert.match(hint, /仍在处理/);
  assert.match(hint, /取消/);
  assert.match(hint, /已完成的部分会保留/);
});
