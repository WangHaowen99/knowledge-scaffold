import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const appSource = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
const routerSource = readFileSync(new URL('../frontend/src/ProjectWorkbenchRouter.jsx', import.meta.url), 'utf8');
const workbenchSource = readFileSync(new URL('../frontend/src/AiConstructionWorkbench.jsx', import.meta.url), 'utf8');
const workspaceSource = readFileSync(new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url), 'utf8');
const reviewPanelSource = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');
const packageChatSource = readFileSync(new URL('../frontend/src/PackageChat.jsx', import.meta.url), 'utf8');

test('顶层离开保护同时接收流程图和知识包文本的未保存状态', () => {
  assert.match(appSource, /confirmUnsavedNavigation\(workspaceDirty\)/);
  assert.match(appSource, /protectUnsavedBeforeUnload\(event, workspaceDirty\)/);
  assert.match(appSource, /<ProjectWorkbenchRouter projectId=\{projectId\} onUnsavedChange=\{setWorkspaceDirty\}/);
  assert.match(routerSource, /onUnsavedChange=\{onUnsavedChange\}/);
  assert.match(workspaceSource, /const workspaceDraftDirty = flowchartDraftDirty \|\| packageDraftDirty/);
  assert.match(workspaceSource, /onDirtyChange=\{setPackageDraftDirty\}/);
  assert.match(workbenchSource, /confirmUnsavedNavigation\(workspaceDirty\)/);
});

test('后台包更新不会覆盖仍在自动保存或冲突中的本地输入', () => {
  assert.match(reviewPanelSource, /const dirtyRef = useRef\(false\)/);
  assert.match(reviewPanelSource, /\(dirtyRef\.current \|\| diagramDirtyRef\.current\) && current \? current/);
  assert.match(reviewPanelSource, /当前文件有未保存修改，已保留你的草稿/);
  assert.match(reviewPanelSource, /onDirtyChange\?\.\(hasUnsavedDraft\)/);
  assert.match(
    workspaceSource,
    /if \(workspaceDirtyRef\.current\) \{[\s\S]*?store\.advanceRequested = true;[\s\S]*?deferredRefreshRef\.current = true/,
  );
});

test('助手消息提交失败时恢复输入草稿', () => {
  assert.match(packageChatSource, /setInput\(\(current\) => current\.trim\(\) \? current : message\)/);
});
