import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

import TaskAdjustmentPreviewCard from '../frontend/src/TaskAdjustmentPreviewCard.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('任务调整预览展示影响范围、ETA 变化并要求填写原因', () => {
  const html = renderToStaticMarkup(React.createElement(TaskAdjustmentPreviewCard, {
    preview: {
      action: 'redo', graphRevision: 8, allowed: true,
      task: { id: 'goal', title: '确认建模目标', status: 'completed', required: true, milestoneId: 'goal_confirmed' },
      affectedTasks: [
        { id: 'goal', title: '确认建模目标', status: 'completed', milestoneId: 'goal_confirmed' },
        { id: 'process', title: '访谈业务流程', status: 'completed', milestoneId: 'process_confirmed' },
      ],
      affectedMilestones: ['goal_confirmed', 'process_confirmed'],
      etaDelta: { minMinutes: 20, maxMinutes: 60 },
    },
    onConfirm: () => {}, onCancel: () => {},
  }));

  assert.match(html, /重做“确认建模目标”/);
  assert.match(html, /访谈业务流程/);
  assert.match(html, /预计增加 20–60 分钟/);
  assert.match(html, /调整原因/);
  assert.match(html, /disabled=""/);
});
