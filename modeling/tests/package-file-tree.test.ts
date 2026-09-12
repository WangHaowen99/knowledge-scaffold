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

test('展示分组保留原路径、修订状态和深层目录，源树保持不变', async () => {
  const { presentPackageFileTree, displayDirectoryPathsForFile } = await import('../frontend/src/packageFileTreePresentation.js');
  const source = buildPackageFileTree(['diagrams/nested/check.mmd', 'operations/check.md', 'index.md'], [], ['diagrams/nested/check.mmd']);
  const original = structuredClone(source);
  const tree = presentPackageFileTree(source);
  const operations = tree.find((node) => node.path === 'operations');
  assert.deepEqual(source, original);
  assert.equal(tree.some((node) => node.path === 'diagrams'), false);
  assert.equal(operations?.fileCount, 2);
  assert.equal(operations?.modifiedCount, 1);
  const diagram = operations?.children.find((node) => node.path === 'diagrams');
  assert.equal(diagram?.children[0].children[0].path, 'diagrams/nested/check.mmd');
  assert.deepEqual(displayDirectoryPathsForFile('diagrams/nested/check.mmd'), ['operations', 'diagrams', 'diagrams/nested']);
  assert.deepEqual(presentPackageFileTree(tree), tree);
});

test('只有流程图时补出操作分组，并排在根文件之前', async () => {
  const { presentPackageFileTree, directoryLabel } = await import('../frontend/src/packageFileTreePresentation.js');
  const tree = presentPackageFileTree(buildPackageFileTree(['index.md', 'diagrams/check.mmd']));
  assert.deepEqual(tree.map((node) => node.path), ['operations', 'index.md']);
  assert.equal(tree[0].fileCount, 1);
  assert.equal(directoryLabel('operations'), '操作');
  assert.equal(directoryLabel('diagrams'), '流程图');
  assert.equal(directoryLabel('custom-folder'), '目录');
  assert.equal(directoryLabel('流程图'), '');
});
