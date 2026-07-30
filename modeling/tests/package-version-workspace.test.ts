import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';

import VersionPanel from '../frontend/src/VersionPanel.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('版本面板同时展示修订说明、文件差异和安全恢复入口', () => {
  const html = renderToStaticMarkup(React.createElement(VersionPanel, {
    versions: [
      {
        id: 'revision-new', parentRevision: 'revision-old', sequence: 2,
        packageVersion: '1.1.0', summary: '更新产品规则',
        actor: { kind: 'user', displayName: 'alice' }, changedPaths: ['rules/product.md'],
      },
      {
        id: 'revision-old', sequence: 1, packageVersion: '1.0.0', summary: '生成第一版',
        actor: { kind: 'agent', displayName: '助手' }, changedPaths: ['index.md'],
      },
    ],
    currentVersionId: 'revision-new',
    selectedVersionId: 'revision-old',
    diff: {
      fromRevision: 'revision-old',
      toRevision: 'revision-new',
      files: [{
        path: 'rules/product.md', status: 'modified', additions: 1, deletions: 1,
        hunks: [{ oldStart: 1, newStart: 1, lines: [
          { type: 'remove', content: '旧规则' },
          { type: 'add', content: '新规则' },
        ] }],
      }],
    },
    onRestore: () => {},
  }));

  assert.match(html, /版本 2/);
  assert.match(html, /更新产品规则/);
  assert.match(html, /恢复/);
  assert.match(html, /rules\/product\.md/);
  assert.match(html, /新规则/);
});

test('建模工作台在协作区和版本工作区之间切换', () => {
  const source = readFileSync(new URL('../frontend/src/Workbench.jsx', import.meta.url), 'utf8');
  assert.match(source, /PackageVersionWorkspace/);
  assert.match(source, />协作</);
  assert.match(source, />版本</);
  assert.match(source, /ctxTab === 'versions'/);
});

test('版本工作区提供发布警告确认和正式发布入口', () => {
  const source = readFileSync(new URL('../frontend/src/PackageVersionWorkspace.jsx', import.meta.url), 'utf8');
  assert.match(source, /发布警告与已知限制/);
  assert.match(source, /acceptPackageWarnings/);
  assert.match(source, /releasePackage/);
  assert.match(source, /正式发布当前候选/);
});
