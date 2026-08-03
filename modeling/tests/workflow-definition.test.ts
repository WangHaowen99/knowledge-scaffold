import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  CORPORATE_MARKETING_PROFILE,
  WORKFLOW_STEPS,
  deriveWorkflowSteps,
} from '../src/authoring/workflow-definition.js';
import type {
  WorkflowFacts,
  WorkflowStepId,
} from '../src/authoring/workflow-definition.js';

function byId(facts: WorkflowFacts = {}) {
  return Object.fromEntries(deriveWorkflowSteps(facts).map((step) => [step.id, step])) as
    Record<WorkflowStepId, ReturnType<typeof deriveWorkflowSteps>[number]>;
}

test('唯一工作流只定义八个用户步骤', () => {
  assert.deepEqual(WORKFLOW_STEPS.map((step) => step.id), [
    'project_setup', 'ai_construction', 'scenario', 'data_source',
    'evaluate', 'diagnose', 'optimize', 'release',
  ]);
  assert.deepEqual(WORKFLOW_STEPS.map((step) => step.name), [
    '项目创建', 'AI辅助构建', '场景推演', '数据源绑定',
    '评测', '失败诊断', '优化更新', '发布交付',
  ]);
  assert.deepEqual(WORKFLOW_STEPS.map((step) => step.group), [
    'prepare', 'model', 'verify', 'verify', 'verify', 'improve', 'improve', 'deliver',
  ]);
  assert.equal(WORKFLOW_STEPS.find((step) => step.id === 'diagnose')?.linear, false);
  assert.equal(WORKFLOW_STEPS.find((step) => step.id === 'optimize')?.linear, false);
  assert.equal(WORKFLOW_STEPS.filter((step) => step.linear !== false).length, 6);
});

test('场景与数据源在建模完成后并行开放，评测等待二者结束', () => {
  const parallel = byId({
    releaseRequested: true,
    requiredSteps: ['data_source'],
    steps: {
      project_setup: { status: 'completed' },
      ingest: { status: 'completed' },
      ai_construction: { status: 'completed' },
      scenario: { available: true },
      data_source: { available: true },
      evaluate: { available: true },
    },
  });

  assert.equal(parallel.scenario.status, 'ready');
  assert.equal(parallel.data_source.status, 'ready');
  assert.equal(parallel.evaluate.status, 'not_started');

  const validationReady = byId({
    releaseRequested: true,
    requiredSteps: ['data_source'],
    steps: {
      project_setup: { status: 'completed' },
      ingest: { status: 'completed' },
      ai_construction: { status: 'completed' },
      scenario: { status: 'completed' },
      data_source: { status: 'skipped' },
      evaluate: { available: true },
    },
  });
  assert.equal(validationReady.evaluate.status, 'ready');
});

test('显式运行事实优先于导航依赖且不会被猜测为完成', () => {
  const steps = byId({
    steps: {
      project_setup: { status: 'completed' },
      ai_construction: { status: 'running' },
    },
  });

  assert.equal(steps.ai_construction.status, 'running');
  assert.equal(steps.scenario.status, 'not_started');
  assert.equal(steps.release.status, 'not_started');
});

test('失败评测只开放诊断，确诊后才开放优化', () => {
  const failed = byId({
    requiredSteps: ['diagnose'],
    enabledSteps: ['optimize'],
    steps: {
      evaluate: { status: 'failed' },
      diagnose: { available: true },
      optimize: { available: false },
    },
  });
  assert.equal(failed.diagnose.status, 'ready');
  assert.equal(failed.optimize.status, 'not_started');

  const diagnosed = byId({
    requiredSteps: ['diagnose'],
    enabledSteps: ['optimize'],
    steps: {
      evaluate: { status: 'failed' },
      diagnose: { status: 'completed' },
      optimize: { available: true },
    },
  });
  assert.equal(diagnosed.optimize.status, 'ready');
});

test('发布步骤可进入检查页，但过时事实保持过时', () => {
  const ready = byId({ releaseRequested: true, steps: { release: { available: true } } });
  assert.equal(ready.release.status, 'ready');

  const stale = byId({ releaseRequested: true, steps: { release: { status: 'stale' } } });
  assert.equal(stale.release.status, 'stale');
});

test('企业银行建议配置只引用八步运行时中的可配置能力', () => {
  assert.deepEqual(CORPORATE_MARKETING_PROFILE, {
    releaseRequested: true,
    enabledSteps: [],
    requiredSteps: ['scenario', 'data_source'],
    defaultSkippedSteps: [],
    afterFailedEvaluation: ['diagnose', 'optimize'],
  });
});
