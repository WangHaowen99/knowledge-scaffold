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
  assert.match(page, /if \(hasWorkspaceDirty\(\)\) \{\n\s+toast\('当前页面有未保存修改，请先保存或放弃后再回退'/);
});

test('回退带上预览时的主线版本，成功后提示并报出没清理掉的派生状态（develop 同）', () => {
  const page = read('pages/versions/VersionsPage.jsx');
  assert.match(page, /api\.revertVersion\(pid, version\.commit, preview\.expected_head\)/);
  assert.match(page, /toast\('已回退并生成新版本'/);
  assert.match(page, /if \(result\.warning\) toast\(result\.warning/);
});

test('流程图、drawio 与 Studio 条目编辑器有没保存的内容时登记回退保护（develop 同）', () => {
  const drawio = read('components/DrawioView.jsx');
  assert.match(drawio, /setWorkspaceDirty\('drawio-editor', saveState === 'dirty' \|\| saveState === 'saving' \|\| saveState === 'error'\)/);
  const flowchart = readFileSync(new URL('../../assets/packs/diagram/ui/flowchart/FlowchartDialog.jsx', import.meta.url), 'utf8');
  assert.match(flowchart, /setWorkspaceDirty\('flowchart-editor', session\.dirty \|\| session\.saveStatus === 'saving'\)/);
  assert.match(flowchart, /setWorkspaceDirty\('flowchart-editor', false\)/);
  const editor = read('pages/studio/EntryEditor.jsx');
  assert.match(editor, /setWorkspaceDirty\('studio-editor', hasDraft \|\| dirty\.current/);
});
