import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isPackageMarkdownHref,
  resolvePackageMarkdownTarget,
} from '../frontend/src/packageMarkdownLink.js';

const files = [
  'index.md',
  'diagrams/index.md',
  'diagrams/授信审批.mmd',
  'objects/客户.md',
];

test('相对 Markdown 链接按当前知识文件目录解析', () => {
  assert.equal(
    resolvePackageMarkdownTarget('diagrams/index.md', '授信审批.mmd', files),
    'diagrams/授信审批.mmd',
  );
  assert.equal(
    resolvePackageMarkdownTarget('diagrams/index.md', '../objects/%E5%AE%A2%E6%88%B7.md', files),
    'objects/客户.md',
  );
});

test('外部链接、页内锚点和越界路径不作为知识包文件导航', () => {
  assert.equal(isPackageMarkdownHref('https://example.com'), false);
  assert.equal(isPackageMarkdownHref('#流程说明'), false);
  assert.equal(resolvePackageMarkdownTarget('diagrams/index.md', '../../secret.md', files), '');
  assert.equal(resolvePackageMarkdownTarget('diagrams/index.md', 'missing.mmd', files), '');
});
