import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
const tourSource = readFileSync(new URL('../frontend/src/Tour.jsx', import.meta.url), 'utf8');

test('主导航不再提供独立打开知识包工作区入口', () => {
  assert.doesNotMatch(source, />\s*打开知识包工作区\s*</);
  assert.doesNotMatch(source, /<PackageWorkspace/);
  assert.doesNotMatch(source, /projectView/);
  assert.match(source, /ProjectWorkbenchRouter/);
  assert.match(source, /统一建模工作台/);
  assert.match(source, /onProjectUnavailable=\{handleProjectUnavailable\}/);
  assert.match(source, /onAuthenticationRequired=\{handleAuthenticationRequired\}/);
  assert.doesNotMatch(tourSource, /生成后从右上角进入/);
  assert.doesNotMatch(tourSource, /data-tour="package-workspace"/);
});

test('导入已有知识包后直接进入新工作台 Studio', () => {
  assert.match(source, /导入已有知识包/);
  assert.match(source, /window\.location\.assign\(importedPackageStudioUrl\(result\.project\.id\)\)/);
  assert.match(source, /workbench\.html\?project=/);
  assert.match(source, /studioView=graph/);
  assert.match(source, /kind === 'modeling' \|\| runtime === 'agent'/);
});
