import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'ai-construction-task-editing-'));

const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { AiConstructionTaskEditor } = await import('../src/ai-construction/task-editing.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');

function activeGoalMilestone(name: string) {
  const project = createProject({ owner: 'alice', name });
  const graph = new AiConstructionTaskGraph(getDb());
  const initial = graph.snapshot(project.id)!;
  const completed = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: initial.revision, taskId: initial.tasks[0].id,
  });
  return { project, graph, snapshot: completed, initialGoalTaskId: initial.tasks[0].id };
}

function planGoalTask(
  graph: InstanceType<typeof AiConstructionTaskGraph>,
  projectId: string,
  revision: number,
  title: string,
  dependsOn: string[],
) {
  return graph.apply(projectId, {
    type: 'plan_task', expectedRevision: revision,
    task: {
      kind: 'modeling_brief', milestoneId: 'goal_confirmed', title,
      goal: `${title}的目标`, required: true, dependsOn,
    },
  });
}

test('用户可以在当前里程碑新增封闭类型任务并保留影响预览和审计', () => {
  const { project, snapshot, initialGoalTaskId } = activeGoalMilestone('新增动态任务');
  const editor = new AiConstructionTaskEditor(getDb());
  const request = {
    action: 'add' as const,
    milestoneId: 'goal_confirmed',
    task: {
      kind: 'modeling_brief' as const,
      title: '补充成功标准',
      goal: '确认可验证的成功标准和非目标。',
      required: false,
      dependsOn: [initialGoalTaskId],
    },
  };

  const preview = editor.preview(project.id, request);
  assert.equal(preview.allowed, true);
  assert.deepEqual(preview.affectedMilestones, ['goal_confirmed']);
  assert.ok((preview.etaDelta?.minMinutes ?? 0) > 0);

  const result = editor.apply(project.id, {
    expectedRevision: snapshot.revision,
    actor: 'alice',
    reason: '材料中成功标准仍有歧义，需要单独确认',
    edit: request,
  });
  const added = result.graph.tasks.find((task: any) => task.title === '补充成功标准');
  assert.equal(added?.kind, 'modeling_brief');
  assert.equal(added?.required, false);
  assert.deepEqual(added?.dependsOn, [initialGoalTaskId]);
  assert.equal(result.edit.action, 'add');
  assert.equal(editor.list(project.id).length, 1);
});

test('任务重排必须保持依赖拓扑顺序且不能移动固定里程碑', () => {
  const { project, graph, snapshot, initialGoalTaskId } = activeGoalMilestone('重排动态任务');
  const first = planGoalTask(graph, project.id, snapshot.revision, '先确认目标', [initialGoalTaskId]);
  const firstTask = first.tasks.at(-1)!;
  const second = planGoalTask(graph, project.id, first.revision, '再确认边界', [firstTask.id]);
  const secondTask = second.tasks.at(-1)!;
  const editor = new AiConstructionTaskEditor(getDb());

  const invalid = editor.preview(project.id, {
    action: 'reorder', taskId: secondTask.id, beforeTaskId: firstTask.id,
  });
  assert.equal(invalid.allowed, false);
  assert.match(invalid.reason ?? '', /依赖/);

  const valid = editor.preview(project.id, {
    action: 'reorder', taskId: firstTask.id,
  });
  assert.equal(valid.allowed, false, '依赖它的任务存在时不能把它移动到最后');

  assert.deepEqual(new AiConstructionTaskGraph(getDb()).snapshot(project.id)?.milestones.map((item: any) => item.id), [
    'goal_confirmed', 'materials_ready', 'process_confirmed',
    'knowledge_confirmed', 'deterministic_confirmed', 'construction_completed',
  ]);
});

test('拆分任务会串联新任务并把下游依赖迁移到最后一项', () => {
  const { project, graph, snapshot, initialGoalTaskId } = activeGoalMilestone('拆分动态任务');
  const planned = planGoalTask(graph, project.id, snapshot.revision, '确认完整目标', [initialGoalTaskId]);
  const source = planned.tasks.at(-1)!;
  const downstream = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: planned.revision,
    task: {
      kind: 'modeling_brief', milestoneId: 'goal_confirmed', title: '复核目标',
      goal: '复核目标完整性', required: true, dependsOn: [source.id],
    },
  });
  const downstreamTask = downstream.tasks.at(-1)!;
  const editor = new AiConstructionTaskEditor(getDb());

  const result = editor.apply(project.id, {
    expectedRevision: downstream.revision,
    actor: 'alice', reason: '目标和成功标准需要分别确认',
    edit: {
      action: 'split', taskId: source.id,
      parts: [
        { kind: 'modeling_brief', title: '确认目标范围', goal: '确认目标、用户和范围' },
        { kind: 'modeling_brief', title: '确认成功标准', goal: '确认成功标准、约束和非目标' },
      ],
    },
  });
  const sourceAfter = result.graph.tasks.find((task: any) => task.id === source.id)!;
  const firstPart = result.graph.tasks.find((task: any) => task.title === '确认目标范围')!;
  const secondPart = result.graph.tasks.find((task: any) => task.title === '确认成功标准')!;
  const downstreamAfter = result.graph.tasks.find((task: any) => task.id === downstreamTask.id)!;
  assert.equal(sourceAfter.status, 'cancelled');
  assert.deepEqual(firstPart.dependsOn, [initialGoalTaskId]);
  assert.deepEqual(secondPart.dependsOn, [firstPart.id]);
  assert.deepEqual(downstreamAfter.dependsOn, [secondPart.id]);
  assert.equal(result.edit.impact.createdTaskIds.length, 2);
});

test('合并同类任务会保留外部依赖并重新连接下游任务', () => {
  const { project, graph, snapshot, initialGoalTaskId } = activeGoalMilestone('合并动态任务');
  const first = planGoalTask(graph, project.id, snapshot.revision, '确认目标用户', [initialGoalTaskId]);
  const firstTask = first.tasks.at(-1)!;
  const second = planGoalTask(graph, project.id, first.revision, '确认使用场景', [initialGoalTaskId]);
  const secondTask = second.tasks.at(-1)!;
  const downstream = planGoalTask(graph, project.id, second.revision, '确认完整目标', [firstTask.id, secondTask.id]);
  const downstreamTask = downstream.tasks.at(-1)!;
  const editor = new AiConstructionTaskEditor(getDb());

  const result = editor.apply(project.id, {
    expectedRevision: downstream.revision,
    actor: 'alice', reason: '两项访谈问题高度重叠',
    edit: {
      action: 'merge', taskIds: [firstTask.id, secondTask.id],
      task: { kind: 'modeling_brief', title: '确认用户与场景', goal: '一起确认目标用户和使用场景' },
    },
  });
  const merged = result.graph.tasks.find((task: any) => task.title === '确认用户与场景')!;
  const downstreamAfter = result.graph.tasks.find((task: any) => task.id === downstreamTask.id)!;
  assert.deepEqual(merged.dependsOn, [initialGoalTaskId]);
  assert.deepEqual(downstreamAfter.dependsOn, [merged.id]);
  assert.equal(result.graph.tasks.find((task: any) => task.id === firstTask.id)?.status, 'cancelled');
  assert.equal(result.graph.tasks.find((task: any) => task.id === secondTask.id)?.status, 'cancelled');
  assert.equal(merged.required, true);
});
