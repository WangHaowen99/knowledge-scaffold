import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';

import AiConstructionMessageCard from '../frontend/src/AiConstructionMessageCard.jsx';
import EvaluationLifecycleTabs, {
  evaluationLifecycleTarget,
} from '../frontend/src/EvaluationLifecycleTabs.jsx';
import {
  formatMessageTimestamp,
} from '../frontend/src/messageTimestamp.js';
import {
  effectiveTaskPlanTasks,
  summarizeTaskPlan,
} from '../frontend/src/taskPlanStatus.js';
import WorkflowTimeline from '../frontend/src/WorkflowTimeline.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

const packageCheckSource = readFileSync(
  new URL('../frontend/src/PackageCheck.jsx', import.meta.url),
  'utf8',
);
const packageChatSource = readFileSync(
  new URL('../frontend/src/PackageChat.jsx', import.meta.url),
  'utf8',
);
const packageWorkspaceSource = readFileSync(
  new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url),
  'utf8',
);
const workbenchBaseStyles = readFileSync(
  new URL('../frontend/src/workbench/base.css', import.meta.url),
  'utf8',
);
const workbenchBlocksSource = readFileSync(
  new URL('../frontend/src/workbench/right/blocks/Blocks.jsx', import.meta.url),
  'utf8',
);

function cssRuleBody(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return source.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`))?.[1] || '';
}

test('工作台主按钮在交互和提交状态保持可读', () => {
  for (const selector of [
    '.wb .btn-p:hover',
    '.wb .btn-p:active',
    '.wb .btn-p:focus-visible',
    '.wb .btn-p[disabled]',
  ]) {
    const rule = cssRuleBody(workbenchBaseStyles, selector);
    assert.match(rule, /background:\s*var\(--grad-p\)/, `${selector} 必须保留主按钮背景`);
    assert.match(rule, /color:\s*#fff/, `${selector} 必须保留白色文字`);
  }
  assert.match(workbenchBlocksSource, /aria-busy=\{sending\}/);
  assert.match(workbenchBlocksSource, /sending \? '正在接受…'/);
});

test('内容检查按钮明确说明会逐项处理错误并复检候选', () => {
  assert.match(packageCheckSource, /逐项处理错误/);
  assert.match(packageCheckSource, /逐项分类处理当前知识包错误，并在候选通过复检后应用/);
  assert.doesNotMatch(packageCheckSource, />继续修复</);
});

test('结构化卡片和普通对话都展示消息时间', () => {
  const createdAt = '2026-07-26T08:05:00.000Z';
  const html = renderToStaticMarkup(React.createElement(AiConstructionMessageCard, {
    message: {
      id: 'timed-card', role: 'assistant', createdAt,
      blocks: [{ type: 'text', text: '已完成资料分析。' }],
    },
  }));

  assert.match(html, /class="ac-message-time"/);
  assert.match(html, /dateTime="2026-07-26T08:05:00.000Z"/);
  assert.ok(formatMessageTimestamp(createdAt));
  assert.match(packageChatSource, /formatMessageTimestamp\(entry\.item\.createdAt\)/);
});

test('评测到优化使用同一组四个标签并同步工作流目标', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationLifecycleTabs, {
    activeView: 'diagnose',
    onSelect: () => {},
  }));

  for (const label of ['评测用例', '运行与结果', '失败诊断', '优化更新']) {
    assert.match(html, new RegExp(`>${label}<`));
  }
  assert.deepEqual(evaluationLifecycleTarget('cases'), {
    workspaceTab: 'evaluate', view: 'cases', workflowStepId: 'evaluate',
  });
  assert.deepEqual(evaluationLifecycleTarget('diagnose'), {
    workspaceTab: 'improve', view: 'diagnose', workflowStepId: 'diagnose',
  });
  assert.deepEqual(evaluationLifecycleTarget('optimize'), {
    workspaceTab: 'improve', view: 'optimize', workflowStepId: 'optimize',
  });
  assert.match(packageWorkspaceSource, /pkw-workflow-stage/);
  assert.match(packageWorkspaceSource, /evaluationLifecycleView/);
});

test('建模流程不再显示条件、发布和可选标签', () => {
  const html = renderToStaticMarkup(React.createElement(WorkflowTimeline, {
    steps: [
      { id: 'scenario', name: '场景推演', group: 'verify', status: 'ready', requirement: 'conditional' },
      { id: 'optimize', name: '优化更新', group: 'improve', status: 'ready', requirement: 'optional' },
      { id: 'release', name: '发布交付', group: 'deliver', status: 'ready', requirement: 'release_required' },
    ],
  }));

  assert.doesNotMatch(html, />条件</);
  assert.doesNotMatch(html, />可选</);
  assert.doesNotMatch(html, />发布</);
  assert.match(html, />场景推演</);
  assert.match(html, />优化更新</);
  assert.match(html, />发布交付</);
});

test('任务概览明确区分运行、等待用户和完成状态', () => {
  assert.deepEqual(summarizeTaskPlan([
    { id: 'done', title: '提取知识', status: 'completed' },
    { id: 'wait', title: '校验知识结构草案', status: 'waiting_confirmation' },
  ]), {
    completed: 1,
    total: 2,
    status: 'waiting_confirmation',
    statusLabel: '等待确认',
    tone: 'waiting',
    title: '校验知识结构草案',
  });
  assert.equal(summarizeTaskPlan([
    { id: 'run', title: '生成知识结构', status: 'running' },
  ]).statusLabel, '运行中');
  assert.equal(summarizeTaskPlan([
    { id: 'done', title: '生成知识结构', status: 'completed' },
  ]).statusLabel, '已完成');
  assert.match(packageChatSource, /pchat-task-status__label/);
});

test('当前任务概览不计入历史过时任务', () => {
  const tasks = effectiveTaskPlanTasks([
    { id: 'old', kind: 'completion_review', milestoneId: 'construction_completed', title: '检查 AI 辅助构建成果', status: 'stale' },
    { id: 'current', kind: 'completion_review', milestoneId: 'construction_completed', title: '检查 AI 辅助构建成果', status: 'completed' },
    { id: 'orphan', kind: 'package_repair', milestoneId: 'construction_completed', title: '历史可选修复', status: 'stale' },
  ]);

  assert.deepEqual(tasks.map((task) => task.id), ['current']);
  assert.deepEqual(summarizeTaskPlan(tasks), {
    completed: 1,
    total: 1,
    status: 'completed',
    statusLabel: '已完成',
    tone: 'complete',
    title: '当前阶段已完成',
  });
});
