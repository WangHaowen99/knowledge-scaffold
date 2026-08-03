import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'ai-construction-task-graph-'));

const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { getDb } = await import('../src/db/index.js');
const { createProject, getProject, listProjects } = await import('../src/projects/repo.js');

const createMaterialFirstProject = (name: string) => createProject({
  owner: 'alice', name, kind: 'package',
});

test('新建 modeling 项目先初始化建模目标任务再进入资料分析', () => {
  const project = createProject({ owner: 'alice', name: '对公营销知识包' });
  const first = new AiConstructionTaskGraph(getDb()).snapshot(project.id);

  assert.equal(first?.revision, 1);
  assert.equal(first?.status, 'active');
  assert.equal(first?.source, 'new');
  assert.deepEqual(first?.milestones.map((item) => [item.id, item.title, item.status]), [
    ['goal_confirmed', '建模目标确认', 'active'],
    ['materials_ready', '资料就绪', 'pending'],
    ['process_confirmed', '业务流程确认', 'pending'],
    ['knowledge_confirmed', '知识结构确认', 'pending'],
    ['deterministic_confirmed', '确定性能力确认', 'pending'],
    ['construction_completed', '建模完成确认', 'pending'],
  ]);
  assert.deepEqual(first?.tasks.map((item) => ({
    kind: item.kind,
    milestoneId: item.milestoneId,
    status: item.status,
    title: item.title,
  })), [{
    kind: 'modeling_brief',
    milestoneId: 'goal_confirmed',
    status: 'pending',
    title: '确认建模目标',
  }]);

  const restored = new AiConstructionTaskGraph(getDb()).snapshot(project.id);
  assert.deepEqual(restored, first);
});

test('新导入的知识包以现有包为基线初始化任务图', () => {
  const project = createProject({ owner: 'alice', name: '已有知识包', kind: 'package' });
  const snapshot = new AiConstructionTaskGraph(getDb()).snapshot(project.id);

  assert.equal(snapshot?.source, 'package_import');
  assert.deepEqual(snapshot?.tasks.map((item) => [item.kind, item.title]), [
    ['package_baseline_review', '检查导入知识包基线'],
  ]);
});

test('重复初始化返回同一任务图而不创建重复任务', () => {
  const project = createMaterialFirstProject('重试初始化');
  const graph = new AiConstructionTaskGraph(getDb());
  const before = graph.snapshot(project.id);
  const after = graph.initialize(project.id);

  assert.deepEqual(after, before);
  assert.equal(after.tasks.length, 1);
  assert.equal(after.revision, 1);
});

test('完成任务必须基于当前 revision 并持久化输出修订', () => {
  const project = createMaterialFirstProject('完成资料分析');
  const graph = new AiConstructionTaskGraph(getDb());
  const before = graph.snapshot(project.id)!;
  const taskId = before.tasks[0].id;

  const after = graph.apply(project.id, {
    type: 'complete_task',
    expectedRevision: before.revision,
    taskId,
    outputRevisionOid: 'git-revision-material-analysis',
  });

  assert.equal(after.revision, 2);
  assert.equal(after.tasks[0].status, 'completed');
  assert.equal(after.tasks[0].outputRevisionOid, 'git-revision-material-analysis');
  assert.deepEqual(new AiConstructionTaskGraph(getDb()).snapshot(project.id), after);

  assert.throws(() => graph.apply(project.id, {
    type: 'complete_task',
    expectedRevision: before.revision,
    taskId,
    outputRevisionOid: 'stale-write',
  }), (error: any) => error?.code === 'REVISION_CONFLICT');
});

test('长任务可以进入运行态，失败后保留并允许显式重试', () => {
  const project = createMaterialFirstProject('可重试知识生成');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const taskId = initial.tasks[0].id;

  const running = graph.apply(project.id, {
    type: 'start_task', expectedRevision: initial.revision, taskId,
  });
  assert.equal(running.tasks[0].status, 'running');
  assert.equal(running.progress.waitingFor, 'task_execution');

  const failed = graph.apply(project.id, {
    type: 'fail_task', expectedRevision: running.revision, taskId,
  });
  assert.equal(failed.tasks[0].status, 'failed');
  assert.equal(failed.progress.currentTaskId, taskId);
  assert.equal(failed.progress.waitingFor, 'task_failed');
  assert.equal(failed.progress.eta.minMinutes, null);
  assert.equal(failed.progress.eta.maxMinutes, null);
  assert.match(failed.progress.eta.note, /步骤已停止/);

  const retried = graph.apply(project.id, {
    type: 'start_task', expectedRevision: failed.revision, taskId,
  });
  assert.equal(retried.tasks[0].status, 'running');

  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: retried.revision, taskId,
    outputRevisionOid: 'retry-output',
  });
  assert.equal(completed.tasks[0].status, 'completed');
  assert.equal(completed.tasks[0].outputRevisionOid, 'retry-output');
});

test('等待用户确认契约的任务可以在回答后恢复执行', () => {
  const project = createMaterialFirstProject('契约确认后继续执行');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const taskId = initial.tasks[0].id;

  const waiting = graph.apply(project.id, {
    type: 'wait_for_user', expectedRevision: initial.revision, taskId,
  });
  assert.equal(waiting.tasks[0].status, 'waiting_user');

  const resumed = graph.apply(project.id, {
    type: 'start_task', expectedRevision: waiting.revision, taskId,
  });
  assert.equal(resumed.tasks[0].status, 'running');
  assert.equal(resumed.progress.waitingFor, 'task_execution');
});

test('确认阶段要求必选任务完成并绑定知识包修订和确认人', () => {
  const project = createMaterialFirstProject('确认资料就绪');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;

  assert.throws(() => graph.apply(project.id, {
    type: 'confirm_milestone',
    expectedRevision: initial.revision,
    milestoneId: 'materials_ready',
    packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
  }), (error: any) => error?.code === 'MILESTONE_NOT_READY');

  const completed = graph.apply(project.id, {
    type: 'complete_task',
    expectedRevision: initial.revision,
    taskId: initial.tasks[0].id,
    outputRevisionOid: 'draft-revision-1',
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone',
    expectedRevision: completed.revision,
    milestoneId: 'materials_ready',
    packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
    note: '资料范围和缺失项已确认',
  });

  assert.equal(confirmed.revision, 3);
  assert.deepEqual(confirmed.milestones.slice(0, 2).map((item) => [
    item.id, item.status, item.confirmedRevisionOid,
  ]), [
    ['materials_ready', 'confirmed', 'draft-revision-1'],
    ['goal_confirmed', 'active', undefined],
  ]);
  assert.deepEqual(confirmed.confirmations.map((item) => ({
    milestoneId: item.milestoneId,
    packageRevisionOid: item.packageRevisionOid,
    actor: item.actor,
    note: item.note,
  })), [{
    milestoneId: 'materials_ready',
    packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
    note: '资料范围和缺失项已确认',
  }]);
});

test('确定性编排器可以基于当前 revision 为活动阶段规划受控任务', () => {
  const project = createMaterialFirstProject('规划建模目标任务');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: initial.revision, taskId: initial.tasks[0].id,
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: completed.revision, milestoneId: 'materials_ready',
    packageRevisionOid: 'draft-revision', actor: { username: 'alice', role: 'project_owner' },
  });

  const planned = graph.apply(project.id, {
    type: 'plan_task',
    expectedRevision: confirmed.revision,
    task: {
      kind: 'modeling_brief',
      milestoneId: 'goal_confirmed',
      title: '确认建模目标',
      goal: '明确知识包要解决的问题、使用者、范围和成功标准。',
      required: true,
      reason: '资料范围确认后进入建模目标确认',
      dependsOn: [initial.tasks[0].id],
    },
  });

  assert.equal(planned.revision, confirmed.revision + 1);
  assert.deepEqual(planned.tasks.slice(-1).map((task) => ({
    kind: task.kind,
    milestoneId: task.milestoneId,
    status: task.status,
    dependsOn: task.dependsOn,
  })), [{
    kind: 'modeling_brief',
    milestoneId: 'goal_confirmed',
    status: 'pending',
    dependsOn: [initial.tasks[0].id],
  }]);
  assert.equal(planned.progress.waitingFor, 'task_execution');

  assert.throws(() => graph.apply(project.id, {
    type: 'plan_task',
    expectedRevision: confirmed.revision,
    task: {
      kind: 'modeling_brief', milestoneId: 'goal_confirmed', title: '重复任务',
      goal: '不应写入', required: true, dependsOn: [],
    },
  }), (error: any) => error?.code === 'REVISION_CONFLICT');
});

test('未确认阶段中的已完成任务可以显式退回用户补充', () => {
  const project = createMaterialFirstProject('重填建模目标');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const materials = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: initial.revision, taskId: initial.tasks[0].id,
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: materials.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'draft-revision',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const planned = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: confirmed.revision,
    task: {
      kind: 'modeling_brief', milestoneId: 'goal_confirmed', title: '确认建模目标',
      goal: '形成目标说明', required: true, dependsOn: [initial.tasks[0].id],
    },
  });
  const goalTask = planned.tasks.at(-1)!;
  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: planned.revision, taskId: goalTask.id,
  });

  const reopened = graph.apply(project.id, {
    type: 'reopen_task', expectedRevision: completed.revision, taskId: goalTask.id,
  });
  const reopenedTask = reopened.tasks.find((task) => task.id === goalTask.id)!;
  assert.equal(reopenedTask.status, 'waiting_user');
  assert.equal(reopenedTask.outputRevisionOid, undefined);
  assert.equal(reopened.progress.waitingFor, 'user_input');
});

test('任务图快照给出当前工作、阶段进度和诚实的区间 ETA', () => {
  const project = createMaterialFirstProject('进度可视化');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;

  assert.deepEqual(initial.progress, {
    confirmedMilestones: 0,
    totalMilestones: 6,
    completedTasks: 0,
    totalTasks: 1,
    percent: 0,
    currentMilestoneId: 'materials_ready',
    currentTaskId: initial.tasks[0].id,
    waitingFor: 'task_execution',
    eta: {
      minMinutes: 3,
      maxMinutes: 10,
      confidence: 'low',
      basis: 'planned_tasks',
      note: '只估算当前已安排的步骤；后面还会新增步骤，剩余时长会随之更新',
    },
  });

  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: 1, taskId: initial.tasks[0].id,
  });
  assert.equal(completed.progress.percent, 13);
  assert.equal(completed.progress.waitingFor, 'milestone_confirmation');
  assert.deepEqual(completed.progress.eta, {
    minMinutes: null,
    maxMinutes: null,
    confidence: 'low',
    basis: 'planned_tasks',
    note: '当前已安排的步骤都已完成，下一批步骤安排好后会重新估算',
  });

  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: 2, milestoneId: 'materials_ready',
    packageRevisionOid: 'draft-revision', actor: { username: 'alice', role: 'project_owner' },
  });
  assert.equal(confirmed.progress.percent, 17);
  assert.equal(confirmed.progress.currentMilestoneId, 'goal_confirmed');
  assert.equal(confirmed.progress.waitingFor, 'task_planning');
});

test('已有有效替代任务时旧 stale 任务不再占用当前任务和进度', () => {
  const project = createMaterialFirstProject('忽略已替代完成检查');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: initial.revision,
    taskId: initial.tasks[0].id, outputRevisionOid: 'revision-current',
  });
  getDb().prepare(`INSERT INTO ai_construction_tasks (
    id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required,
    reason, input_revision_oid, created_at, updated_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
    'task_stale_replaced', project.id, completed.tasks[0].kind, completed.tasks[0].milestoneId,
    999, '旧检查任务', '已由新任务替代', 'stale', 1, '历史任务',
    'revision-old', new Date().toISOString(), new Date().toISOString(),
  );

  const restored = graph.snapshot(project.id)!;
  assert.equal(restored.progress.currentTaskId, undefined);
  assert.equal(restored.progress.waitingFor, 'milestone_confirmation');
  assert.equal(restored.progress.completedTasks, 1);
  assert.equal(restored.progress.totalTasks, 1);
});

test('动态任务支持影响预览、暂缓、跳过和恢复并保留审计', () => {
  const project = createMaterialFirstProject('动态任务调整');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: initial.revision, taskId: initial.tasks[0].id,
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: completed.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'revision-materials',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const planned = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: confirmed.revision,
    task: {
      kind: 'package_repair', milestoneId: 'goal_confirmed', title: '补充非阻断说明',
      goal: '补充可选说明', required: false, dependsOn: [initial.tasks[0].id],
    },
  });
  const task = planned.tasks.at(-1)!;
  const preview = graph.previewAdjustment(project.id, task.id, 'skip');
  assert.equal(preview.allowed, true);
  assert.deepEqual(preview.affectedTasks.map((item) => item.id), [task.id]);
  assert.ok((preview.etaDelta?.maxMinutes ?? 0) < 0);

  const skipped = graph.adjust(project.id, {
    expectedRevision: planned.revision, taskId: task.id, action: 'skip',
    actor: 'alice', reason: '当前范围不需要补充这项说明',
  });
  assert.equal(skipped.graph.tasks.find((item) => item.id === task.id)?.status, 'skipped');
  assert.equal(skipped.adjustment.action, 'skip');

  const resumed = graph.adjust(project.id, {
    expectedRevision: skipped.graph.revision, taskId: task.id, action: 'resume',
    actor: 'alice', reason: '评审后决定恢复该任务',
  });
  assert.equal(resumed.graph.tasks.find((item) => item.id === task.id)?.status, 'pending');
  assert.equal(graph.listAdjustments(project.id).length, 2);
});

test('重做已确认任务重新激活上游阶段并使下游任务过时', () => {
  const project = createMaterialFirstProject('确认点回退任务图');
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const materialsDone = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: initial.revision, taskId: initial.tasks[0].id,
  });
  const materialsConfirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: materialsDone.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'revision-materials',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const goalPlanned = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: materialsConfirmed.revision,
    task: {
      kind: 'modeling_brief', milestoneId: 'goal_confirmed', title: '确认建模目标',
      goal: '形成建模目标', required: true, dependsOn: [initial.tasks[0].id],
    },
  });
  const goalTask = goalPlanned.tasks.at(-1)!;
  const goalDone = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: goalPlanned.revision, taskId: goalTask.id,
    outputRevisionOid: 'revision-goal',
  });
  const goalConfirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: goalDone.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: 'revision-goal',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const processPlanned = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: goalConfirmed.revision,
    task: {
      kind: 'process_interview', milestoneId: 'process_confirmed', title: '访谈业务流程',
      goal: '确认流程', required: true, dependsOn: [goalTask.id],
    },
  });
  const processTask = processPlanned.tasks.at(-1)!;

  const preview = graph.previewAdjustment(project.id, goalTask.id, 'redo');
  assert.equal(preview.allowed, true);
  assert.deepEqual(preview.affectedTasks.map((item) => item.id), [goalTask.id, processTask.id]);
  assert.deepEqual(preview.affectedMilestones, ['goal_confirmed', 'process_confirmed']);
  assert.equal(preview.restoreRevisionOid, 'revision-materials');

  const redone = graph.adjust(project.id, {
    expectedRevision: processPlanned.revision, taskId: goalTask.id, action: 'redo',
    actor: 'alice', reason: '目标范围发生变化，需要重新确认',
  }).graph;
  assert.equal(redone.milestones.find((item) => item.id === 'goal_confirmed')?.status, 'active');
  assert.equal(redone.milestones.find((item) => item.id === 'process_confirmed')?.status, 'stale');
  assert.equal(redone.tasks.find((item) => item.id === goalTask.id)?.status, 'pending');
  assert.equal(redone.tasks.find((item) => item.id === processTask.id)?.status, 'stale');
  assert.equal(redone.confirmations.length, 2, '历史确认记录必须保留');
});

test('项目查询保持纯读，不为存量项目隐式创建任务图', () => {
  const now = new Date().toISOString();
  getDb().prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES ('pj_legacy', 'alice', '历史项目', '', '', 'project_setup', ?, ?)`)
    .run(now, now);

  const project = getProject('pj_legacy');
  assert.equal(project?.id, 'pj_legacy');
  assert.deepEqual(listProjects('alice').some((item) => item.id === 'pj_legacy'), true);
  assert.equal(new AiConstructionTaskGraph(getDb()).snapshot('pj_legacy'), null);
});
