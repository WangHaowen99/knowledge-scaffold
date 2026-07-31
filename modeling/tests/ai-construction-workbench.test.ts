import { createRequire } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import AiConstructionWorkbench, {
  CollapsedWorkflowStep,
  initialAiConstructionStepId,
  selectWorkflowStepAfterRefresh,
  workspaceNavigationTarget,
} from '../frontend/src/AiConstructionWorkbench.jsx';
import { AI_CONSTRUCTION_WORKBENCH_STEPS } from '../frontend/src/Tour.jsx';
import WorkflowTimeline from '../frontend/src/WorkflowTimeline.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

const steps = [
  { id: 'project_setup', label: '项目创建', group: 'prepare', focus: 'project_setup', status: 'completed' },
  { id: 'ingest', label: '建模资料上传', group: 'prepare', focus: 'ingest', status: 'completed' },
  { id: 'ai_construction', label: 'AI辅助构建', group: 'model', focus: 'ai_construction', status: 'waiting_answer' },
  { id: 'scenario', label: '场景推演', group: 'verify', focus: 'scenario', status: 'not_started' },
  { id: 'data_source', label: '数据源绑定', group: 'verify', focus: 'data_source', status: 'not_started' },
  { id: 'evaluate', label: '评测', group: 'verify', focus: 'evaluate', status: 'not_started' },
  { id: 'release', label: '发布交付', group: 'deliver', focus: 'release', status: 'not_started' },
];

const session = {
  project: {
    id: 'pj-ai', kind: 'modeling', name: 'AI 建模项目',
    workflowProfile: { enabledSteps: [], requiredSteps: [], releaseRequested: true },
  },
  documents: [],
  workflow: { steps },
};

test('统一工作台默认定位当前 AI 辅助构建任务', () => {
  assert.equal(initialAiConstructionStepId(steps), 'ai_construction');
});

test('评测待确认时仍定位评测阶段', () => {
  const failed = steps.map((step) => {
    if (step.id === 'ai_construction') return { ...step, status: 'completed' };
    if (step.id === 'evaluate') return { ...step, status: 'failed' };
    return step;
  });
  assert.equal(initialAiConstructionStepId(failed), 'evaluate');
});

test('诊断和优化页签映射到评测流程节点但保留内部视图', () => {
  assert.deepEqual(workspaceNavigationTarget('diagnose'), {
    workflowStepId: 'evaluate', workspaceFocus: 'diagnose',
  });
  assert.deepEqual(workspaceNavigationTarget('optimize'), {
    workflowStepId: 'evaluate', workspaceFocus: 'optimize',
  });
  assert.deepEqual(workspaceNavigationTarget('scenario'), {
    workflowStepId: 'scenario', workspaceFocus: '',
  });
});

test('流程栏收起后仍显示当前步骤和状态', () => {
  const html = renderToStaticMarkup(React.createElement(CollapsedWorkflowStep, {
    step: steps[2],
  }));

  assert.match(html, /AI辅助构建/);
  assert.match(html, /待回答/);
  assert.match(html, /aria-current="step"/);
  assert.match(html, /title="AI辅助构建 · 待回答"/);
});

test('子面板推进步骤前先刷新服务端流程状态', async () => {
  const calls = [];
  const refreshed = {
    workflow: {
      steps: steps.map((step) => (
        step.id === 'evaluate' ? { ...step, status: 'ready' } : step
      )),
    },
  };

  const selected = await selectWorkflowStepAfterRefresh({
    stepId: 'evaluate',
    refresh: async () => { calls.push('refresh'); return refreshed; },
    select: (stepId) => { calls.push(`select:${stepId}`); },
    fallbackSteps: steps,
  });

  assert.equal(selected, true);
  assert.deepEqual(calls, ['refresh', 'select:evaluate']);
});

test('评测阶段内切换页签立即更新视图而不重刷项目会话', async () => {
  const calls = [];
  const selected = await selectWorkflowStepAfterRefresh({
    stepId: 'evaluate',
    currentStepId: 'evaluate',
    refresh: async () => { calls.push('refresh'); throw new Error('不应调用'); },
    select: (stepId) => { calls.push(`select:${stepId}`); },
    fallbackSteps: steps.map((step) => (
      step.id === 'evaluate' ? { ...step, status: 'ready' } : step
    )),
  });

  assert.equal(selected, true);
  assert.deepEqual(calls, ['select:evaluate']);
});

test('统一工作台直接嵌入 AI 辅助构建并提供可收缩流程栏', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionWorkbench, {
    projectId: 'pj-ai', session, onProjectChanged: () => {},
  }));

  assert.match(html, /AI辅助构建/);
  assert.match(html, /aria-label="收起建模流程"/);
  assert.match(html, /ai-construction-workbench/);
  assert.match(html, /package-workspace embedded/);
  assert.doesNotMatch(html, /打开知识包工作区/);
  assert.doesNotMatch(html, /渐进提取|胜任力问题|知识包生成/);
});

test('流程栏收缩后仍保留当前步骤、状态和整体位置', () => {
  const html = renderToStaticMarkup(React.createElement(AiConstructionWorkbench, {
    projectId: 'pj-ai', session, onProjectChanged: () => {},
  }));

  assert.match(html, /acw-flow-compact/);
  assert.match(html, /AI辅助构建/);
  assert.match(html, /待回答/);
  assert.match(html, /3\/7/);
});

test('流程栏收缩后仍展示当前步骤名称和状态', () => {
  const html = renderToStaticMarkup(React.createElement(WorkflowTimeline, {
    steps,
    activeId: 'ai_construction',
    compact: true,
    onSelect: () => {},
  }));

  assert.match(html, /workflow-timeline compact/);
  assert.match(html, /AI辅助构建/);
  assert.match(html, /待回答/);
  assert.match(html, /aria-current="step"/);
});

test('工作台引导只讲解收敛后的统一流程', () => {
  const titles = AI_CONSTRUCTION_WORKBENCH_STEPS.map((step) => step.title);
  assert.deepEqual(titles.slice(0, 6), [
    '建模流程', '建模资料上传', 'AI辅助构建',
    '场景推演', '数据源绑定', '评测',
  ]);
  assert.doesNotMatch(
    AI_CONSTRUCTION_WORKBENCH_STEPS.map((step) => `${step.title}${step.body}`).join('\n'),
    /渐进提取|胜任力问题|打开知识包工作区/,
  );
});
