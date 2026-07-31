import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildPackageFileTree,
  directoryPathsForFile,
  listDirectoryPaths,
} from '../frontend/src/packageFileTree.js';

test('知识包文件按目录层级构建并聚合文件数量', () => {
  const tree = buildPackageFileTree([
    'index.md',
    'operations/recommend.md',
    'objects/customer.md',
    'objects/order.md',
    'assets/logo.png',
  ], ['assets/logo.png'], ['objects/customer.md']);

  assert.deepEqual(tree.map((node) => [node.type, node.name]), [
    ['directory', 'assets'],
    ['directory', 'objects'],
    ['directory', 'operations'],
    ['file', 'index.md'],
  ]);
  const objects = tree.find((node) => node.path === 'objects');
  assert.equal(objects?.fileCount, 2);
  assert.equal(objects?.modifiedCount, 1);
  assert.deepEqual(objects?.children.map((node) => [node.name, node.modified]), [
    ['customer.md', true],
    ['order.md', false],
  ]);
  const logo = tree.find((node) => node.path === 'assets')?.children[0];
  assert.equal(logo?.binary, true);
  assert.equal(logo?.typeLabel, 'IMG');
});

test('选中文件能找到所有父目录，目录路径可以一次性展开', () => {
  const tree = buildPackageFileTree([
    'objects/customer/profile.md',
    'objects/customer/contact.md',
    'operations/recommend.md',
  ]);

  assert.deepEqual(directoryPathsForFile('objects/customer/profile.md'), ['objects', 'objects/customer']);
  assert.deepEqual(listDirectoryPaths(tree), ['objects', 'objects/customer', 'operations']);
});
