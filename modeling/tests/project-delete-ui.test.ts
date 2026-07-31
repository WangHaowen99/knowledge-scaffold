import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import test from 'node:test';

import { ProjectCard } from '../frontend/src/ProjectList.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('项目卡片提供独立且可识别的删除入口', () => {
  const html = renderToStaticMarkup(React.createElement(ProjectCard, {
    project: {
      id: 'pj-delete', name: '待删除项目', kind: 'modeling', stage: 'project_setup',
      background: '', domain: '', updatedAt: '2026-07-23T00:00:00.000Z',
    },
    onOpen: () => {},
    onRequestDelete: () => {},
  }));

  assert.match(html, /aria-label="删除项目：待删除项目"/);
  assert.match(html, /title="删除项目"/);
  assert.match(html, />×</);
});

test('项目卡片具有按钮语义并支持键盘打开', () => {
  const html = renderToStaticMarkup(React.createElement(ProjectCard, {
    project: {
      id: 'pj-open', name: '可访问项目', kind: 'modeling', stage: 'analyze',
      background: '', domain: '', updatedAt: '2026-07-23T00:00:00.000Z',
    },
    onOpen: () => {},
    onRequestDelete: () => {},
  }));

  assert.match(html, /role="button"/);
  assert.match(html, /tabindex="0"/);
  assert.match(html, /aria-label="打开项目：可访问项目"/);
});
