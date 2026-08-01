import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

// 独立 DATA_DIR：这些用例会写入已完成任务作为实测样本，
// 跟别的用例共用库会污染它们对 eta 的精确断言。
process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'ai-construction-eta-'));

const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');

test('运行中的任务已耗时要从预计剩余里扣掉，否则数字纹丝不动', () => {
  const project = createProject({ owner: 'alice', name: '剩余时长随进度收缩' });
  const graph = new AiConstructionTaskGraph(getDb());
  const before = graph.snapshot(project.id)!;
  const task = before.tasks[0];

  const started = graph.apply(project.id, {
    type: 'start_task', expectedRevision: before.revision, taskId: task.id,
  });
  const fresh = started.progress.eta.maxMinutes!;

  // 把该任务的 updated_at 往前挪，模拟已经跑了一段时间
  getDb().prepare('UPDATE ai_construction_tasks SET updated_at = ? WHERE id = ?')
    .run(new Date(Date.now() - 4 * 60_000).toISOString(), task.id);
  const later = graph.snapshot(project.id)!.progress.eta.maxMinutes!;

  assert.ok(later < fresh, `跑了 4 分钟后剩余应变少：${fresh} -> ${later}`);
  assert.ok(later >= 1, '不能因为超时就显示成 0');
});

test('别的项目跑完的同类任务会喂给本项目的估算', () => {
  const graph = new AiConstructionTaskGraph(getDb());
  // 甲项目先把 modeling_brief 跑完，留下一条实测样本
  const done = createProject({ owner: 'alice', name: '甲：已跑完建模目标' });
  const doneSnapshot = graph.snapshot(done.id)!;
  const doneTask = doneSnapshot.tasks[0];
  assert.equal(doneTask.kind, 'modeling_brief');
  getDb().prepare(`UPDATE ai_construction_tasks SET status = 'completed', created_at = ?, updated_at = ?
    WHERE id = ?`).run(
    new Date(Date.now() - 40 * 60_000).toISOString(),
    new Date(Date.now() - 10 * 60_000).toISOString(),
    doneTask.id,
  );

  // 乙项目还没跑过 modeling_brief，但应当用得上甲项目的样本
  const fresh = createProject({ owner: 'alice', name: '乙：全新项目' });
  const progress = graph.snapshot(fresh.id)!.progress;
  assert.notEqual(progress.eta.confidence, 'low', '有同类实测样本就不该还是 low');
  // 甲项目实测 30 分钟，静态表是 [5, 15]，估算应当被真实数据拉上去
  assert.ok((progress.eta.maxMinutes ?? 0) > 15,
    `应参考实测 30 分钟而不是静态上限 15：${progress.eta.maxMinutes}`);
});
