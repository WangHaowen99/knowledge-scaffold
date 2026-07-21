import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  buildVisibleTreeRows,
  directoryExpansionKey,
  kbTreeExpansionStorageKey,
  packageExpansionKey,
  readKbTreeExpansion,
  toggleExpandedKey,
  writeKbTreeExpansion,
} from './kbTreeState.js';

const currentDir = dirname(fileURLToPath(import.meta.url));
const sidebarSource = readFileSync(join(currentDir, 'KbSidebarTree.jsx'), 'utf8')
  .replace(/\r\n?/g, '\n');

const createStorage = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
};

test('tree expansion state is isolated and reloadable per workspace', () => {
  const storage = createStorage();
  const workspaceAKeys = new Set([
    packageExpansionKey('pkg-a'),
    directoryExpansionKey('pkg-a', 'objects'),
  ]);

  assert.equal(writeKbTreeExpansion(storage, 'workspace-a', workspaceAKeys), true);
  assert.deepEqual(readKbTreeExpansion(storage, 'workspace-a'), workspaceAKeys);
  assert.equal(readKbTreeExpansion(storage, 'workspace-b'), null);
  assert.notEqual(
    kbTreeExpansionStorageKey('workspace-a'),
    kbTreeExpansionStorageKey('workspace-b'),
  );
});

test('an explicitly collapsed workspace remains collapsed after reload', () => {
  const storage = createStorage();

  writeKbTreeExpansion(storage, 'workspace-a', new Set());

  assert.deepEqual(readKbTreeExpansion(storage, 'workspace-a'), new Set());
});

test('toggleExpandedKey does not mutate the previous selection', () => {
  const key = packageExpansionKey('pkg-a');
  const previous = new Set([key]);

  const collapsed = toggleExpandedKey(previous, key);
  const reopened = toggleExpandedKey(collapsed, key);

  assert.equal(previous.has(key), true);
  assert.equal(collapsed.has(key), false);
  assert.equal(reopened.has(key), true);
});

test('visible rows follow independent expansion state through deep directories', () => {
  const tree = [{
    type: 'dir',
    name: 'objects',
    path: 'objects',
    children: [{
      type: 'dir',
      name: 'equipment',
      path: 'objects/equipment',
      children: [{
        type: 'file',
        name: 'A320.md',
        path: 'objects/equipment/A320.md',
      }],
    }],
  }];
  const objectsKey = directoryExpansionKey('pkg-a', 'objects');
  const equipmentKey = directoryExpansionKey('pkg-a', 'objects/equipment');

  assert.deepEqual(
    buildVisibleTreeRows(tree, new Set(), 'pkg-a').map(({ entry, depth }) => [entry.path, depth]),
    [['objects', 0]],
  );
  assert.deepEqual(
    buildVisibleTreeRows(tree, new Set([objectsKey]), 'pkg-a')
      .map(({ entry, depth }) => [entry.path, depth]),
    [['objects', 0], ['objects/equipment', 1]],
  );
  assert.deepEqual(
    buildVisibleTreeRows(tree, new Set([objectsKey, equipmentKey]), 'pkg-a')
      .map(({ entry, depth }) => [entry.path, depth]),
    [['objects', 0], ['objects/equipment', 1], ['objects/equipment/A320.md', 2]],
  );
});

test('package and directory toggles are accessible and separate from selection', () => {
  assert.equal(sidebarSource.includes('aria-expanded={packageExpanded}'), true);
  assert.equal(sidebarSource.includes('aria-expanded={row.expanded}'), true);
  assert.equal(sidebarSource.includes('onClick={() => togglePackage(pkg)}'), true);
  assert.equal(sidebarSource.includes('onClick={() => selectPkg(pkg.id)}'), true);
  assert.equal(sidebarSource.includes('onClick={() => toggleDirectory(pkg.id, entry.path)}'), true);
  assert.equal(sidebarSource.includes('onClick={() => selectPackageEntry(pkg.id, entry)}'), true);
});
