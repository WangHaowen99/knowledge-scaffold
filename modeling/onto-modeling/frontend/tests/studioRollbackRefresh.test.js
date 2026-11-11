// 回退之后界面跟上、没保存的编辑不被盖掉（移植 develop studioRollbackRefresh：本分支没有 App/CenterPane，
// 回退生成的版本经项目流 workspace.changed 通知各页，按页检查同样的行为）
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const read = (rel) => readFileSync(new URL(`../src/${rel}`, import.meta.url), 'utf8');
const studioPages = ['pages/studio/StudioPage.jsx', 'pages/studio/StudioTablePage.jsx', 'pages/studio/StudioErView.jsx'];

test('回退生成新版本后，打开着的 Studio 各页重新读取', () => {
  for (const rel of studioPages) {
    assert.match(read(rel), /env\.type === 'workspace\.changed'\) setTick\(\(t\) => t \+ 1\)/, rel);
  }
  assert.match(read('pages/studio/StudioTablePage.jsx'), /<EntryEditor [^>]*tick=\{tick\}/);
});

test('Studio 刷新时重读打开的条目，但不盖掉没保存的表单', () => {
  const editor = read('pages/studio/EntryEditor.jsx');
  assert.match(editor, /useEffect\(\(\) => \{ if \(tick && !dirty\.current && !raw\) load\(/);
});

test('对比编辑器把没保存的内容登记给回退保护，离开页面时撤销登记', () => {
  const page = read('pages/versions/VersionsPage.jsx');
  assert.match(page, /const dirty = !!diff && text !== diff\.right/);
  assert.match(page, /setWorkspaceDirty\('diff-editor', dirty\)/);
  assert.match(page, /setWorkspaceDirty\('diff-editor', false\)/);
  const dialogs = read('pages/versions/dialogs.jsx');
  assert.match(dialogs, /hasWorkspaceDirty\(\) && blockerView\(\{ code: 'unsaved_editor' \}\)/);
  assert.match(dialogs, /disabled=\{busy \|\| !preview \|\| !!shownBlock\}/);
});

test('回退带上预览时的主线版本，预览之后有了新版本就重新预览', () => {
  const dialogs = read('pages/versions/dialogs.jsx');
  assert.match(dialogs, /api\.revertVersion\(pid, version\.commit, preview\.expected_head\)/);
  assert.match(dialogs, /if \(e\.code === 'head_moved'\) setRound\(\(n\) => n \+ 1\)/);
});
