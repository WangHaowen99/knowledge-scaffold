import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM } from 'jsdom';

const appShellSource = readFileSync(new URL('../../app/AppShell.jsx', import.meta.url), 'utf8');
const browserSource = readFileSync(new URL('./hooks/useKbBrowser.js', import.meta.url), 'utf8');
const knowledgeCss = readFileSync(new URL('./knowledge.css', import.meta.url), 'utf8');
const workbenchSource = readFileSync(new URL('./KnowledgeWorkbench.jsx', import.meta.url), 'utf8');

test('primary navigation keeps the workbench entry without rendering a nested knowledge tree', () => {
  assert.match(appShellSource, /navBtn\('kb', PackageOpen, '知识包工作台', 'nav-kb'\)/);
  assert.doesNotMatch(appShellSource, /KbSidebarTree/);
});

test('the workbench owns package switching and package-list feedback', () => {
  assert.match(workbenchSource, /<label className="kb-tb-package">切换知识包/);
  assert.match(workbenchSource, /value=\{pkgId \|\| ''\}/);
  assert.match(workbenchSource, /onChange=\{\(event\) => selectPkg\(event.target.value\)\}/);
  assert.match(workbenchSource, /packages\.map\(\(candidate\) =>/);
  assert.match(workbenchSource, /正在加载知识包…/);
  assert.match(workbenchSource, /知识包加载失败/);
  assert.match(workbenchSource, /暂无知识包/);
  assert.match(workbenchSource, /onClick=\{\(\) => retryPackages\(\)\}/);
});

test('the empty package state is centered in the available workbench canvas', () => {
  assert.match(workbenchSource, /className="kbx kb-empty-shell"/);
  const dom = new JSDOM(
    `<style>${knowledgeCss}</style><div class="mwr"><div class="kbx kb-empty-shell"></div></div>`,
  );
  const emptyShell = dom.window.document.querySelector('.kb-empty-shell');
  const style = dom.window.getComputedStyle(emptyShell);

  assert.equal(style.display, 'grid');
  assert.equal(style.placeItems, 'center');
  dom.window.close();
});

test('package changes clear stale content and directory failures remain recoverable in the workbench', () => {
  assert.match(browserSource, /setTree\(\[\]\);[\s\S]*setSel\(\{ dir: 'objects', file: null \}\);/);
  assert.match(workbenchSource, /title="目录加载失败"/);
  assert.match(workbenchSource, /onClick=\{retryTree\}/);
  assert.match(workbenchSource, /if \(!treeContainsPath\(tree, focusRequest\.path\)\) return;/);
  assert.doesNotMatch(workbenchSource, /className="col2" style=/);
  assert.match(knowledgeCss, /@media \(max-width: 900px\)[\s\S]*\.mwr \.kbx \.col2 \{ width: 100%;/);
});
