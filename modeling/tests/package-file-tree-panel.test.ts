import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import PackageFileTree from '../frontend/src/PackageFileTree.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('目录树自动展开当前文件父目录并显示类型、修订与附件状态', () => {
  const html = renderToStaticMarkup(React.createElement(PackageFileTree, {
    files: ['index.md', 'objects/customer/profile.md', 'assets/logo.png'],
    binaryPaths: ['assets/logo.png'],
    overriddenPaths: ['objects/customer/profile.md'],
    selectedPath: 'objects/customer/profile.md',
    version: '1.2.0',
    onSelect: () => {},
    onDownload: () => {},
  }));

  assert.match(html, /知识包结构/);
  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /profile\.md/);
  assert.match(html, /MD/);
  assert.match(html, /已修订/);
  assert.match(html, /附件/);
  assert.match(html, /展开全部目录/);
  assert.match(html, /收起全部目录/);
  assert.match(html, /objects/);
  assert.match(html, /对象/);
});

test('流程图目录在展示上归入操作，并显示英文与中文目录名', () => {
  const html = renderToStaticMarkup(React.createElement(PackageFileTree, {
    files: ['operations/check.md', 'diagrams/check.mmd', 'rules/rule.md'],
    selectedPath: 'diagrams/check.mmd',
    version: '1.0.0',
    onSelect: () => {},
    onDownload: () => {},
  }));

  assert.match(html, /operations/);
  assert.match(html, /操作/);
  assert.match(html, /diagrams/);
  assert.match(html, /流程图/);
  assert.match(html, /data-package-path="diagrams\/check\.mmd"/);
  assert.match(html, /aria-level="1" aria-expanded="true" title="收起 operations"/);
  assert.match(html, /aria-level="2" aria-expanded="true" title="收起 diagrams"/);
  assert.match(html, /aria-level="3" aria-selected="true"[^>]+data-package-path="diagrams\/check\.mmd"/);
});
