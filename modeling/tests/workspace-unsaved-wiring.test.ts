import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
const workbenchSource = readFileSync(new URL('../frontend/src/Workbench.jsx', import.meta.url), 'utf8');
const reviewPanelSource = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');
const packageChatSource = readFileSync(new URL('../frontend/src/PackageChat.jsx', import.meta.url), 'utf8');

test('顶层离开保护同时接收流程图和知识包文本的未保存状态', () => {
  assert.match(appSource, /confirmUnsavedNavigation\(workspaceDirty\)/);
  assert.match(appSource, /protectUnsavedBeforeUnload\(event, workspaceDirty\)/);
  assert.match(appSource, /<Workbench projectId=\{projectId\} onUnsavedChange=\{setWorkspaceDirty\}/);
  assert.match(workbenchSource, /const workspaceDirty = flowchartDirty \|\| packageEditorDirty/);
  assert.match(workbenchSource, /onDirtyChange=\{setPackageEditorDirty\}/);
  assert.match(workbenchSource, /confirmUnsavedNavigation\(workspaceDirty\)/);
});

test('后台包更新不会覆盖仍在自动保存或冲突中的本地输入', () => {
  assert.match(reviewPanelSource, /const deferredPackageRefreshRef = useRef\(false\)/);
  assert.match(reviewPanelSource, /deferredPackageRefreshRef\.current = true/);
  assert.match(reviewPanelSource, /当前文件有未保存修改，已保留你的输入/);
  assert.match(reviewPanelSource, /onDirtyChange\?\.\(hasPendingLocalChanges\)/);
  assert.match(reviewPanelSource, /if \(hasPendingLocalChanges \|\| !deferredPackageRefreshRef\.current\) return/);
});

test('助手消息提交失败时恢复输入草稿', () => {
  assert.match(packageChatSource, /setInput\(\(current\) => current\.trim\(\) \? current : message\)/);
});
