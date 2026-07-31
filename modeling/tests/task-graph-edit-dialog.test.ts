import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import TaskGraphEditDialog from '../frontend/src/TaskGraphEditDialog.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('新增动态任务先填写封闭类型、目标和必选性再预览影响', () => {
  const html = renderToStaticMarkup(React.createElement(TaskGraphEditDialog, {
    config: { mode: 'add', milestoneId: 'goal_confirmed' },
    onPreview: async () => ({}), onApply: () => {}, onCancel: () => {},
  }));

  assert.match(html, /编辑动态任务/);
  assert.match(html, /新增任务/);
  assert.match(html, /任务类型/);
  assert.match(html, /流程访谈/);
  assert.match(html, /完成目标/);
  assert.match(html, /设为当前里程碑必选任务/);
  assert.match(html, /预览影响/);
});

test('拆分任务默认提供两个可编辑子任务而不直接改任务图', () => {
  const html = renderToStaticMarkup(React.createElement(TaskGraphEditDialog, {
    config: {
      mode: 'split',
      task: { id: 'task_1', kind: 'process_interview', title: '访谈核心流程' },
    },
    onPreview: async () => ({}), onApply: () => {}, onCancel: () => {},
  }));

  assert.match(html, /拆分任务/);
  assert.match(html, /子任务 1/);
  assert.match(html, /子任务 2/);
  assert.match(html, /访谈核心流程（第一部分）/);
  assert.doesNotMatch(html, /确认应用/);
});
