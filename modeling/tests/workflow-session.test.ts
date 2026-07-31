import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildProjectWorkflow,
  type ProjectWorkflowStateInput,
} from '../src/authoring/workflow-session.js';
import type { AuthoringSnapshot } from '../src/authoring/types.js';
import type { DocumentRecord, PackageSpec, Project } from '../src/types.js';

const project: Project = {
  id: 'pj_workflow', owner: 'alice', kind: 'modeling', name: '流程状态', domain: '测试',
  background: '', sensitivityLevel: 'standard',
  workflowProfile: {
    enabledSteps: [], requiredSteps: ['scenario', 'data_source'],
    releaseRequested: true, externalResearch: false,
  },
  stage: 'project_setup',
  createdAt: '2026-07-31T00:00:00.000Z', updatedAt: '2026-07-31T00:00:00.000Z',
};

const document: DocumentRecord = {
  id: 'doc_workflow', projectId: project.id, filename: '资料.md', kind: 'markdown',
  parseStatus: 'parsed', textContent: '资料', createdAt: '2026-07-31T00:00:00.000Z',
};

const authoring: AuthoringSnapshot = {
  projectId: project.id, activeRuns: [], pendingInteractions: [], lastEventSeq: 0,
};

const revision = '1'.repeat(40);
const spec: PackageSpec = {
  id: 'workflow-package', name: '流程包', description: '流程包', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
};
const currentPackage = {
  revision: { oid: revision }, packageVersion: spec.version, spec,
};

function graph(overrides: Record<string, unknown> = {}) {
  return {
    projectId: project.id, revision: 1, status: 'active', source: 'new',
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

function input(overrides: Partial<ProjectWorkflowStateInput> = {}): ProjectWorkflowStateInput {
  return {
    project, documents: [document], nodes: [], currentPackage: null, packageCheck: null,
    dataSource: null, testcases: [], rounds: [], latestResults: [], diagnoses: [], suggestions: [],
    authoring, progress: [], aiConstruction: graph(), releaseAvailable: false, releaseReady: false,
    ...overrides,
  };
}

function workflow(overrides: Partial<ProjectWorkflowStateInput> = {}) {
  return buildProjectWorkflow(input(overrides));
}

function steps(overrides: Partial<ProjectWorkflowStateInput> = {}) {
  return Object.fromEntries(workflow(overrides).steps.map((step) => [step.id, step])) as any;
}

test('会话原生返回九步流程且完全忽略旧 wf_nodes', () => {
  const withoutNodes = workflow();
  const withConflictingNodes = workflow({
    nodes: [
      { id: 'old-analyze', type: 'analyze', status: 'done' },
      { id: 'old-interview', type: 'interview', status: 'failed' },
      { id: 'old-generate', type: 'generate', status: 'stale' },
      { id: 'old-review', type: 'review', status: 'waiting_user' },
      { id: 'old-evaluate', type: 'evaluate', status: 'done' },
      { id: 'old-optimize', type: 'optimize', status: 'failed' },
    ] as any,
  });

  assert.deepEqual(withoutNodes.steps.map((step) => step.id), [
    'project_setup', 'ingest', 'ai_construction', 'scenario', 'data_source',
    'evaluate', 'diagnose', 'optimize', 'release',
  ]);
  assert.deepEqual(withConflictingNodes.steps, withoutNodes.steps);
  for (const legacy of ['research', 'extract', 'interview', 'flowchart', 'generate', 'validate']) {
    assert.equal(withoutNodes.steps.some((step) => step.id === legacy), false);
  }
});

test('旧步骤进度不会泄漏到九步会话或改变状态', () => {
  const baseline = workflow();
  const withLegacyProgress = workflow({
    progress: [{
      projectId: project.id, stepId: 'flowchart', status: 'completed', output: {},
      updatedAt: '2026-07-31T00:01:00.000Z',
    }] as any,
  });

  assert.deepEqual(withLegacyProgress.steps, baseline.steps);
  assert.deepEqual(withLegacyProgress.progress, []);
});

test('资料和任务图分别提供摄取与 AI 构建状态', () => {
  const noGraph = steps({ aiConstruction: null });
  assert.equal(noGraph.ingest.status, 'completed');
  assert.equal(noGraph.ai_construction.status, 'ready');

  const imported = steps({
    project: { ...project, kind: 'package' },
    documents: [],
    aiConstruction: graph({ source: 'package_import' }),
  });
  assert.equal(imported.ingest.status, 'skipped');
  assert.equal(imported.ai_construction.status, 'ready');

  const waiting = steps({
    aiConstruction: graph({
      tasks: [{ id: 'task-current', kind: 'process_interview', status: 'waiting_user' }],
      progress: { currentTaskId: 'task-current', waitingFor: 'user_input' },
    }),
  });
  assert.equal(waiting.ai_construction.status, 'waiting_answer');
});

test('AI 构建完成后场景和数据源并行开放，二者完成后开放评测', () => {
  const completedGraph = graph({
    status: 'completed', tasks: [], progress: { waitingFor: 'completed' },
  });
  const parallel = steps({ aiConstruction: completedGraph, currentPackage });
  assert.equal(parallel.scenario.status, 'ready');
  assert.equal(parallel.data_source.status, 'ready');
  assert.equal(parallel.evaluate.status, 'not_started');

  const ready = steps({
    aiConstruction: completedGraph,
    currentPackage,
    dataSource: { mode: 'none' } as any,
    testcases: [{ id: 'tc_1' }],
    progress: [{
      projectId: project.id, stepId: 'scenario', status: 'completed', output: {}, revision,
      updatedAt: '2026-07-31T00:02:00.000Z',
    }],
  });
  assert.equal(ready.scenario.status, 'completed');
  assert.equal(ready.data_source.status, 'skipped');
  assert.equal(ready.evaluate.status, 'ready');
});

test('场景进度绑定 Git 修订，旧修订不能继续评测', () => {
  const stale = steps({
    aiConstruction: graph({ status: 'completed', tasks: [], progress: { waitingFor: 'completed' } }),
    currentPackage,
    dataSource: { mode: 'none' } as any,
    testcases: [{ id: 'tc_1' }],
    progress: [{
      projectId: project.id, stepId: 'scenario', status: 'completed', output: {},
      revision: 'old-revision', updatedAt: '2026-07-31T00:02:00.000Z',
    }],
  });

  assert.equal(stale.scenario.status, 'stale');
  assert.equal(stale.evaluate.status, 'not_started');
});

test('评测结果、诊断和建议直接驱动诊断优化阶段', () => {
  const failed = steps({
    currentPackage,
    rounds: [{ id: 'round-1', status: 'done' }],
    latestResults: [{ verdict: 'fail' }],
  });
  assert.equal(failed.evaluate.status, 'completed');
  assert.equal(failed.diagnose.status, 'ready');
  assert.equal(failed.optimize.status, 'not_started');

  const diagnosed = steps({
    currentPackage,
    rounds: [{ id: 'round-1', status: 'done' }],
    latestResults: [{ verdict: 'fail' }],
    diagnoses: [{ basedRevisionOid: revision, packageOptimizable: true, blocking: false }] as any,
  });
  assert.equal(diagnosed.diagnose.status, 'completed');
  assert.equal(diagnosed.optimize.status, 'ready');

  const pendingCandidate = steps({
    currentPackage,
    rounds: [{ id: 'round-1', status: 'done' }],
    latestResults: [{ verdict: 'fail' }],
    diagnoses: [{ basedRevisionOid: revision, packageOptimizable: true, blocking: false }] as any,
    suggestions: [{ status: 'candidate' }],
  });
  assert.equal(pendingCandidate.optimize.status, 'waiting_review');

  const rejected = steps({
    currentPackage,
    suggestions: [{ status: 'rejected' }, { status: 'rejected' }],
  });
  assert.equal(rejected.optimize.status, 'skipped');
});

test('旧修订诊断保持过时且不能开放优化', () => {
  const stale = steps({
    currentPackage,
    rounds: [{ id: 'round-1', status: 'done' }],
    latestResults: [{ verdict: 'fail' }],
    diagnoses: [{ basedRevisionOid: 'old-revision', packageOptimizable: true, blocking: false }] as any,
  });

  assert.equal(stale.diagnose.status, 'stale');
  assert.equal(stale.optimize.status, 'not_started');
});

test('只有知识包对话变更计划进入优化复核', () => {
  const withInteraction = (purpose: string) => steps({
    currentPackage,
    authoring: {
      ...authoring,
      pendingInteractions: [{
        id: `interaction-${purpose}`,
        type: 'change_plan',
        payload: { purpose },
      }] as any,
    },
  });

  assert.equal(withInteraction('package_chat').optimize.status, 'waiting_review');
  assert.equal(withInteraction('script_generation').optimize.status, 'not_started');
});

test('评测后的修缮任务归入评测阶段', () => {
  const repairing = steps({
    currentPackage,
    rounds: [{ id: 'round-1', status: 'done' }],
    latestResults: [{ verdict: 'fail' }],
    aiConstruction: graph({
      tasks: [{ id: 'task-current', kind: 'evaluation_repair', status: 'running' }],
      progress: { currentTaskId: 'task-current', waitingFor: 'task_execution' },
    }),
  });

  assert.equal(repairing.ai_construction.status, 'completed');
  assert.equal(repairing.evaluate.status, 'running');
});

test('当前 Git 包检查直接决定发布阶段的可处理状态', () => {
  const clean = steps({
    currentPackage,
    packageCheck: { version: spec.version, revision, findings: [] },
    releaseAvailable: true,
  });
  assert.equal(clean.release.status, 'ready');

  const errors = steps({
    currentPackage,
    packageCheck: { version: spec.version, revision, findings: [{ severity: '错误' }] },
    releaseAvailable: true,
  });
  assert.equal(errors.release.status, 'waiting_review');

  const stale = steps({
    currentPackage,
    packageCheck: { version: spec.version, revision: 'old-revision', findings: [] },
    releaseAvailable: true,
  });
  assert.equal(stale.release.status, 'stale');
});
