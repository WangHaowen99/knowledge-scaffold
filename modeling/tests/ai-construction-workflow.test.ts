import { test } from 'node:test';
import assert from 'node:assert/strict';

import { deriveAiConstructionPhaseStatuses } from '../src/authoring/ai-construction-workflow.js';

function graph(overrides: Record<string, unknown> = {}) {
  return {
    projectId: 'pj_workflow', revision: 1, status: 'active', source: 'new',
    createdAt: '2026-07-31T00:00:00.000Z', updatedAt: '2026-07-31T00:00:00.000Z',
    milestones: [], confirmations: [],
    tasks: [{
      id: 'task-current', kind: 'material_analysis', milestoneId: 'materials_ready', ordinal: 1,
      title: '分析资料', goal: '分析资料', status: 'pending', required: true, dependsOn: [],
      createdAt: '2026-07-31T00:00:00.000Z', updatedAt: '2026-07-31T00:00:00.000Z',
    }],
    progress: {
      confirmedMilestones: 0, totalMilestones: 1, completedTasks: 0, totalTasks: 1,
      percent: 0, currentTaskId: 'task-current', waitingFor: 'task_execution',
      eta: {
        minMinutes: 1, maxMinutes: 2, confidence: 'low', basis: 'planned_tasks', note: '待执行',
      },
    },
    ...overrides,
  } as any;
}

test('任务图直接给出 AI 辅助构建阶段状态', () => {
  assert.deepEqual(deriveAiConstructionPhaseStatuses(null), { aiConstruction: 'not_started' });
  assert.equal(deriveAiConstructionPhaseStatuses(graph()).aiConstruction, 'ready');
  assert.equal(deriveAiConstructionPhaseStatuses(graph({
    tasks: [{ id: 'task-current', kind: 'material_analysis', status: 'waiting_user' }],
    progress: { currentTaskId: 'task-current', waitingFor: 'user_input' },
  })).aiConstruction, 'waiting_answer');
  assert.equal(deriveAiConstructionPhaseStatuses(graph({
    progress: { waitingFor: 'milestone_confirmation' }, tasks: [],
  })).aiConstruction, 'waiting_review');
  assert.equal(deriveAiConstructionPhaseStatuses(graph({
    status: 'completed', progress: { waitingFor: 'completed' }, tasks: [],
  })).aiConstruction, 'completed');
});

test('失败、过时和被替代的任务图不会伪装成可继续', () => {
  assert.equal(deriveAiConstructionPhaseStatuses(graph({
    tasks: [{ id: 'task-current', kind: 'material_analysis', status: 'failed' }],
    progress: { currentTaskId: 'task-current', waitingFor: 'task_failed' },
  })).aiConstruction, 'failed');
  assert.equal(deriveAiConstructionPhaseStatuses(graph({
    tasks: [{ id: 'task-current', kind: 'material_analysis', status: 'stale' }],
  })).aiConstruction, 'stale');
  assert.equal(deriveAiConstructionPhaseStatuses(graph({ status: 'superseded' })).aiConstruction, 'stale');
});

test('评测开始后的修缮任务归入评测而不重新点亮建模', () => {
  const statuses = deriveAiConstructionPhaseStatuses(graph({
    tasks: [{ id: 'task-current', kind: 'evaluation_repair', status: 'running' }],
    progress: { currentTaskId: 'task-current', waitingFor: 'task_execution' },
  }), true);

  assert.deepEqual(statuses, { aiConstruction: 'completed', evaluationRepair: 'running' });
});

test('评测前的知识包校验仍属于 AI 辅助构建', () => {
  const statuses = deriveAiConstructionPhaseStatuses(graph({
    tasks: [{ id: 'task-current', kind: 'package_validation', status: 'running' }],
    progress: { currentTaskId: 'task-current', waitingFor: 'task_execution' },
  }), false);

  assert.deepEqual(statuses, { aiConstruction: 'running' });
});
