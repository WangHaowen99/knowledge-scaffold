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
});
