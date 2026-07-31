import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  isPackageDraftEditablePath,
  isPackageDraftRequiredError,
  isPackageDraftRevisionConflict,
  latestChangedPackageDraftPath,
  packageDraftOperationLabel,
} from '../frontend/src/package-draft-ui.js';

test('人工草稿只允许编辑四类详情 Markdown 文件', () => {
  for (const path of [
    'objects/Order.md',
    'operations/query_order.md',
    'rules/cancel_rule.md',
    'metrics/order_amount.md',
  ]) {
    assert.equal(isPackageDraftEditablePath(path), true, path);
  }

  for (const path of [
    'index.md',
    'objects/index.md',
    'manifest.yaml',
    'mapping/data-mapping.yaml',
    'scripts/query_order.py',
    'objects/nested/Order.md',
    'objects/Order.txt',
    '../objects/Order.md',
  ]) {
    assert.equal(isPackageDraftEditablePath(path), false, path);
  }
});

test('刷新恢复优先选择最近修改的可编辑草稿文件', () => {
  assert.equal(latestChangedPackageDraftPath({
    changedFiles: [
      { path: 'objects/Order.md', updatedAt: '2026-07-29T10:00:00.000Z' },
      { path: 'manifest.yaml', updatedAt: '2026-07-29T12:00:00.000Z' },
      { path: 'rules/cancel_rule.md', updatedAt: '2026-07-29T11:00:00.000Z' },
    ],
  }), 'rules/cancel_rule.md');
  assert.equal(latestChangedPackageDraftPath({ changedFiles: [] }), null);
  assert.equal(latestChangedPackageDraftPath(null), null);
});

test('草稿门禁与 revision 冲突可由前端稳定识别', () => {
  assert.equal(packageDraftOperationLabel('package-export'), '导出知识包');
  assert.equal(packageDraftOperationLabel('evaluate'), '运行知识包评测');
  assert.equal(packageDraftOperationLabel('unknown'), '当前操作');
  assert.equal(isPackageDraftRequiredError({ data: { code: 'PACKAGE_DRAFT_REQUIRES_PUBLISH' } }), true);
  assert.equal(isPackageDraftRequiredError({ data: { code: 'OTHER' } }), false);
  assert.equal(isPackageDraftRevisionConflict({ data: { code: 'PACKAGE_DRAFT_REVISION_CONFLICT' } }), true);
});

test('二进制响应解析保留草稿门禁错误详情', async () => {
  const { requireOkResponse } = await import('../frontend/src/api.js');
  const body = {
    code: 'PACKAGE_DRAFT_REQUIRES_PUBLISH',
    error: '请先发布草稿',
    operation: 'package-export',
  };
  await assert.rejects(
    requireOkResponse({ ok: false, status: 409, json: async () => body }),
    (error: any) => error.status === 409 && error.data === body && error.message === body.error,
  );
});

test('前端主流程已从旧 override 写入切换为草稿发布流程', () => {
  const review = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');
  const workspace = readFileSync(new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url), 'utf8');
  const check = readFileSync(new URL('../frontend/src/PackageCheck.jsx', import.meta.url), 'utf8');
  const api = readFileSync(new URL('../frontend/src/api.js', import.meta.url), 'utf8');

  assert.doesNotMatch(review, /\bsavePackageFile\s*\(/);
  assert.doesNotMatch(api, /export const (?:save|reset)PackageFile/);
  for (const apiName of [
    'createPackageDraft',
    'getPackageDraftFile',
    'savePackageDraftFile',
    'revertPackageDraftFile',
    'publishPackageDraft',
    'discardPackageDraft',
  ]) {
    assert.match(review, new RegExp(`\\b${apiName}\\s*\\(`));
  }
  assert.match(review, /setTimeout\(\(\) => \{\s*persistDraftFile\(path, editorContent, true\)/);
  assert.match(workspace, /packageDraft=\{pkg\.draft \?\? null\}/);
  assert.match(workspace, /onDraftChange=\{\(draft\) => setPkg/);
  assert.match(workspace, /protectUnsavedBeforeUnload\(event, true\)/);
  assert.match(check, /请先发布或放弃人工编辑草稿/);
  assert.doesNotMatch(api, /mw-package-draft-required/);
  assert.match(review, /撤销该派生文件覆盖/);
});

test('包修缮运行状态由服务端会话共享，并禁用人工编辑入口与草稿写操作', () => {
  const review = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');
  const workspace = readFileSync(new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url), 'utf8');
  const chat = readFileSync(new URL('../frontend/src/PackageChat.jsx', import.meta.url), 'utf8');

  assert.match(workspace, /packageRepairBusy=\{isPackageChatRunning\(session\.authoring\) \? 'plan' : ''\}/);
  assert.match(chat, /isPackageChatRunning\(authoring\)/);
  assert.match(chat, /const busy = localBusy \|\| \(chatRunning \? 'plan' : ''\)/);
  assert.match(review, /知识包修缮正在进行中，暂不能人工编辑/);
  assert.match(review, /if \(repairInProgress \|\| revisionConflict\?\.path === path/);
  assert.match(review, /disabled=\{repairInProgress \|\| !editable \|\| actionBusy === 'create'\}/);
  assert.match(review, /disabled=\{repairInProgress \|\| !!revisionConflict \|\| !!actionBusy \|\| saveState === 'saving'/);
  assert.match(review, /disabled=\{repairInProgress \|\| !!actionBusy\}/);
});

test('revision 冲突会暂停自动保存并要求用户明确选择文件版本', () => {
  const review = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');
  const api = readFileSync(new URL('../frontend/src/api.js', import.meta.url), 'utf8');

  assert.match(api, /savePackageDraftFile = \(id, path, content, revision, expectedContentHash, draftId\)/);
  assert.match(api, /JSON\.stringify\(\{ path, content, revision, expectedContentHash, draftId \}\)/);
  assert.match(review, /savedContentHashRef\.current/);
  assert.match(review, /repairInProgress \|\| revisionConflict\?\.path === path/);
  assert.match(review, /自动保存已暂停/);
  assert.match(review, /加载其他页面版本/);
  assert.match(review, /保留当前内容并覆盖/);
  assert.match(review, /contentHash: conflict\.file\.contentHash/);
  assert.match(review, /draftId: conflict\.draft\.id/);
});

test('发布时发生 revision 冲突会提示刷新页面后重试', () => {
  const review = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');

  assert.match(review, /setPublishRevisionConflict\(true\)/);
  assert.match(review, /role="alertdialog"/);
  assert.match(review, /有其他修改已应用到当前草稿/);
  assert.match(review, /当前页面的 revision 已过期/);
  assert.match(review, /window\.location\.reload\(\)/);
});

test('文件树使用草稿标签标识已修改文件', () => {
  const review = readFileSync(new URL('../frontend/src/M2Panels.jsx', import.meta.url), 'utf8');
  const tree = readFileSync(new URL('../frontend/src/PackageFileTree.jsx', import.meta.url), 'utf8');

  assert.match(tree, /className="tag tag-draft rv-file-draft-tag">草稿<\/span>/);
  assert.match(review, /带“草稿”标记的派生文件/);
  assert.doesNotMatch(tree, /\{f\}\{draftChangedSet\.has\(f\) \? ' ●' : ''\}/);
});

test('正式知识包消费链路不再读取或写入全局 override', () => {
  const runtimeSources = [
    '../src/routes.ts',
    '../src/workflow/package-chat.ts',
    '../src/graph-import/service.ts',
    '../src/evaluation/data-source-consumer-sync.ts',
    '../src/evaluation/engine.ts',
  ].map((path) => readFileSync(new URL(path, import.meta.url), 'utf8')).join('\n');
  const routes = readFileSync(new URL('../src/routes.ts', import.meta.url), 'utf8');

  assert.doesNotMatch(runtimeSources, /\b(?:getOverrides|setOverride|clearOverride)\b/);
  assert.match(routes, /PACKAGE_FILE_OVERRIDE_REMOVED/);
  assert.doesNotMatch(runtimeSources, /overrides\[path\]|Object\.entries\(overrides\)/);
});
