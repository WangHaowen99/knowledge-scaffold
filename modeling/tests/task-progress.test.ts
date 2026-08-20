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
const { STALL_THRESHOLD_MS, lastProgressAt, stalledForMs } =
  await import('../frontend/src/taskStall.js');
const { jobEtaFromProgress, jobHeartbeatStale, startJobHeartbeat } = await import('../src/authoring/job-observability.js');

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

test('刚启动的操作不会因为任务上有旧消息而被误报成停滞', () => {
  // 界面上同时出现「正在处理 · 1 秒」和「已经 43 分钟没有新进展」，就是这条没做对
  const messages = [{ taskId: 't1', createdAt: ago(43 * 60_000) }];
  assert.equal(
    stalledForMs({ messages, taskId: 't1', startedAt: ago(1_000), now: NOW }),
    null,
    '当前操作一秒前才启动，不该判成停滞',
  );
});

test('当前操作确实跑了很久且无进展时仍然提示', () => {
  const messages = [{ taskId: 't1', createdAt: ago(60 * 60_000) }];
  assert.ok(stalledForMs({
    messages, taskId: 't1', startedAt: ago(STALL_THRESHOLD_MS + 60_000), now: NOW,
  }), '这才是真停滞');
});

test('统一 Job 心跳超过阈值才标记为可能中断', () => {
  const now = Date.parse('2026-08-03T10:00:00.000Z');
  assert.equal(jobHeartbeatStale('2026-08-03T09:59:30.000Z', now), false);
  assert.equal(jobHeartbeatStale('2026-08-03T09:57:00.000Z', now), true);
  assert.equal(jobHeartbeatStale(undefined, now), true);
});

test('统一 Job 定时心跳可以持续刷新并在任务收尾时停止', async () => {
  let heartbeatCount = 0;
  const stop = startJobHeartbeat({
    heartbeat: () => { heartbeatCount += 1; },
  } as any, 'project-heartbeat', 'run-heartbeat', {}, 5);

  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.ok(heartbeatCount >= 2);
  stop();
  const stoppedAt = heartbeatCount;
  await new Promise((resolve) => setTimeout(resolve, 18));
  assert.equal(heartbeatCount, stoppedAt);
});

test('统一 Job 根据实际进度和耗时动态收窄剩余范围', () => {
  assert.deepEqual(jobEtaFromProgress(50, 600), {
    minMinutes: 8, maxMinutes: 14, confidence: 'medium',
  });
  assert.deepEqual(jobEtaFromProgress(0, 60), {
    minMinutes: null, maxMinutes: null, confidence: 'low',
  });
});
