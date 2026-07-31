import { createRequire } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';

import ProjectWorkbenchRouter, {
  projectWorkbenchMode,
} from '../frontend/src/ProjectWorkbenchRouter.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

const modelingSession = {
  project: { id: 'pj-modeling', kind: 'modeling', name: '建模项目' },
  documents: [],
  workflow: {
    steps: [
      { id: 'project_setup', label: '项目创建', group: 'prepare', focus: 'project_setup', status: 'completed' },
      { id: 'ingest', label: '建模资料上传', group: 'prepare', focus: 'ingest', status: 'completed' },
      { id: 'ai_construction', label: 'AI辅助构建', group: 'model', focus: 'ai_construction', status: 'ready' },
    ],
  },
};

test('项目工作台只按项目类型选择统一界面', () => {
  assert.equal(projectWorkbenchMode(modelingSession), 'ai_construction');
  assert.equal(projectWorkbenchMode({ project: { kind: 'modeling' } }), 'ai_construction');
});

test('历史导入知识包继续打开完整知识包工作区', () => {
  assert.equal(projectWorkbenchMode({
    project: { kind: 'package' },
  }), 'package');
  const html = renderToStaticMarkup(React.createElement(ProjectWorkbenchRouter, {
    projectId: 'pj-package',
    initialSession: {
      project: { id: 'pj-package', kind: 'package' },
    },
  }));
  assert.match(html, /package-workspace/);
});

test('建模项目直接进入统一 AI 辅助构建工作台', () => {
  const html = renderToStaticMarkup(React.createElement(ProjectWorkbenchRouter, {
    projectId: 'pj-modeling', initialSession: modelingSession,
  }));

  assert.match(html, /ai-construction-workbench/);
  assert.match(html, /AI辅助构建/);
});
