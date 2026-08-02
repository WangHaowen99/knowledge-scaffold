import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'interrupted-task-recovery-'));

const {
  buildInterruptedTaskBlocks, interruptedTaskKinds, interruptedTaskPresentation,
} = await import('../src/authoring/interrupted-task-recovery.js');
const { ASSISTANT_ACTION_KINDS } = await import('../src/ai-construction/assistant-actions.js');
const { recoverInterruptedAuthoringWork } = await import('../src/authoring/restart-recovery.js');
const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');

test('每种中断任务的重试动作都在助手动作目录里登记过', () => {
  for (const kind of interruptedTaskKinds()) {
    for (const action of interruptedTaskPresentation(kind)!.actions) {
      assert.ok(ASSISTANT_ACTION_KINDS[action.id],
        `${kind} 的出口 ${action.id} 未登记，会变成点了没反应的按钮`);
    }
  }
});

test('耗时最长的几种任务都必须有出口——它们被打断的代价最大', () => {
  for (const kind of ['knowledge_extraction', 'script_generation', 'package_repair',
    'evaluation_repair', 'operation_extraction', 'package_validation']) {
    const presentation = interruptedTaskPresentation(kind);
    assert.ok(presentation, `${kind} 缺少中断出口`);
    assert.equal(presentation!.actions[0].style, 'primary', `${kind} 应有主操作`);
  }
});

test('每个出口都有主操作打头，不能只给「查看」类动作', () => {
  for (const kind of interruptedTaskKinds()) {
    const actions = interruptedTaskPresentation(kind)!.actions;
    assert.ok(actions.length > 0, `${kind} 出口为空`);
    assert.equal(actions[0].style, 'primary', `${kind} 的首个出口应为主操作`);
  }
});

test('未登记的任务种类不硬造消息', () => {
  assert.equal(interruptedTaskPresentation('modeling_brief'), null);
  assert.equal(buildInterruptedTaskBlocks({
    kind: 'script_decision', reason: 'x', milestoneId: 'm', taskId: 't',
  }), null);
});

test('消息里带上被中断的步骤名和可执行的下一步', () => {
  const blocks = buildInterruptedTaskBlocks({
    kind: 'knowledge_extraction',
    reason: '服务重启，任务已中断，请重新执行当前步骤',
    milestoneId: 'knowledge_confirmed',
    taskId: 'task_1',
    taskTitle: '从流程提取业务操作',
  })!;
  assert.equal(blocks[0].type, 'status');
  assert.match(String((blocks[0] as any).summary), /从流程提取业务操作/);
  const next = blocks.find((block) => block.type === 'next_actions') as any;
  assert.equal(next.actions[0].id, 'generate_knowledge_structure');
});

test('重启恢复给非资料分析的中断任务也写出可操作消息', () => {
  const project = createProject({ owner: 'alice', name: '重启后知识生成中断' });
  const db = getDb();
  const graph = new AiConstructionTaskGraph(db);
  const snapshot = graph.snapshot(project.id)!;
  // 造一个执行中的知识生成任务
  db.prepare(`UPDATE ai_construction_tasks
    SET status = 'running', task_kind = 'knowledge_extraction' WHERE id = ?`)
    .run(snapshot.tasks[0].id);

  const result = recoverInterruptedAuthoringWork(db);
  assert.ok(result.recoveryMessages >= 1, '应写出至少一条中断说明');

  const message = db.prepare(`SELECT blocks_json FROM ai_construction_messages
    WHERE project_id = ? AND message_key = ?`)
    .get(project.id, `restart.interrupted.${snapshot.tasks[0].id}`) as { blocks_json?: string } | undefined;
  assert.ok(message?.blocks_json, '中断消息没有落库');
  const blocks = JSON.parse(message!.blocks_json!);
  const next = blocks.find((block: any) => block.type === 'next_actions');
  assert.equal(next.actions[0].id, 'generate_knowledge_structure');

  // 任务本身被标为 failed，界面才知道它停了
  const task = db.prepare('SELECT status FROM ai_construction_tasks WHERE id = ?')
    .get(snapshot.tasks[0].id) as { status: string };
  assert.equal(task.status, 'failed');
});

test('重复重启不会攒出一串重复卡片', () => {
  const project = createProject({ owner: 'alice', name: '连续两次重启' });
  const db = getDb();
  const snapshot = new AiConstructionTaskGraph(db).snapshot(project.id)!;
  const taskId = snapshot.tasks[0].id;
  const setRunning = () => db.prepare(`UPDATE ai_construction_tasks
    SET status = 'running', task_kind = 'script_generation' WHERE id = ?`).run(taskId);

  setRunning();
  recoverInterruptedAuthoringWork(db);
  setRunning();
  recoverInterruptedAuthoringWork(db);

  const count = db.prepare(`SELECT COUNT(*) AS n FROM ai_construction_messages
    WHERE project_id = ? AND message_key = ?`)
    .get(project.id, `restart.interrupted.${taskId}`) as { n: number };
  assert.equal(count.n, 1);
});
