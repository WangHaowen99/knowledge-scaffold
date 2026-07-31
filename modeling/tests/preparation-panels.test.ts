import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { ProjectSetupView } from '../frontend/src/PreparationPanels.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const projectListSource = readFileSync(
  new URL('../frontend/src/ProjectList.jsx', import.meta.url),
  'utf8',
);

const project = {
  id: 'pj-modeling',
  name: '准备阶段',
  domain: '', background: '', targetUsers: '', glossaryPrefs: '',
  sensitivityLevel: 'standard',
  workflowProfile: {
    enabledSteps: ['optimize'], requiredSteps: ['scenario', 'data_source'],
    releaseRequested: true, externalResearch: false,
  },
};

const render = () => renderToStaticMarkup(
  React.createElement(ProjectSetupView, {
    project, onChanged: () => {},
  }),
);

test('统一项目设置不再暴露场景、代码生成和数据源流程开关', () => {
  const html = render();
  assert.doesNotMatch(html, />场景推演</);
  assert.doesNotMatch(html, />代码生成</);
  assert.doesNotMatch(html, />数据源</);
  assert.match(html, /验证阶段固定包含场景推演、数据源绑定和评测/);
  assert.match(html, />正式交付</);
});

test('新建项目弹窗使用固定的新流程说明', () => {
  assert.doesNotMatch(projectListSource, /toggleStep\('requiredSteps', 'scenario'\)/);
  assert.doesNotMatch(projectListSource, /toggleStep\('enabledSteps', 'optimize'\)/);
  assert.doesNotMatch(projectListSource, /toggleStep\('requiredSteps', 'data_source'\)/);
  assert.match(projectListSource, /验证阶段固定包含场景推演、数据源绑定和评测/);
});
