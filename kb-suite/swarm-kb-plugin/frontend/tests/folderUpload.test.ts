import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parentUploadFolder } from '../src/folderUpload.ts';

// Synthetic directory selections.
test('root files and same-named files in different subdirectories retain their own parents', () => {
  const folders = ['folder-demo', 'folder-demo/alpha', 'folder-demo/示例 目录/deep'];
  const selected = folders.map((folder) => ({ name: 'notes.md', webkitRelativePath: `${folder}/notes.md` }));
  assert.deepEqual(selected.map(parentUploadFolder), folders);
});

test('an incomplete or non-relative selection cannot silently upload into the root', () => {
  for (const path of ['', 'notes.md', '/demo/notes.md', 'X:/demo/notes.md', 'demo/../notes.md', 'demo//notes.md', 'demo\\deep/notes.md', 'demo/else.md']) {
    assert.throws(() => parentUploadFolder({ name: 'notes.md', webkitRelativePath: path }), /相对路径/);
  }
});
