import { createRequire } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import PackageChat, { MaterialAnalysisStoppedNotice } from '../frontend/src/PackageChat.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('资料分析被服务重启中断后显示停止原因和重试入口', () => {
  const html = renderToStaticMarkup(React.createElement(MaterialAnalysisStoppedNotice, {
    task: {
      id: 'task-material',
      kind: 'material_analysis',
      status: 'failed',
      reason: '服务重启，任务已中断，请重新执行当前步骤',
    },
    onRetry: () => {},
  }));

  assert.match(html, /资料分析已停止/);
  assert.match(html, /服务重启，任务已中断，请重新执行当前步骤/);
  assert.match(html, />重新分析资料</);
  assert.match(html, /role="alert"/);
  assert.doesNotMatch(html, /正在分析|spinner/);
});

test('刷新建模对话时隐藏失败任务遗留的进行中消息', () => {
  const task = {
    id: 'task-material',
    kind: 'material_analysis',
    milestoneId: 'materials_ready',
    status: 'failed',
    reason: '服务重启，任务已中断，请重新执行当前步骤',
  };
  const html = renderToStaticMarkup(React.createElement(PackageChat, {
    projectId: 'project-material',
    structuredMessages: [{
      id: 'message-progress',
      key: 'analysis.progress.old',
      taskId: task.id,
      role: 'assistant',
      blocks: [
        { type: 'status', title: '正在分析第 3/4 批资料', summary: '正在整理候选' },
        { type: 'text', text: '当前正在分析资料。' },
      ],
    }],
    aiConstruction: {
      status: 'active',
      progress: { currentTaskId: task.id, currentMilestoneId: 'materials_ready' },
      tasks: [task],
    },
    onProjectChanged: () => {},
  }));

  assert.match(html, /资料分析已停止/);
  assert.match(html, />重新分析资料</);
  assert.doesNotMatch(html, /正在分析第 3\/4 批资料|当前正在分析资料/);
});
