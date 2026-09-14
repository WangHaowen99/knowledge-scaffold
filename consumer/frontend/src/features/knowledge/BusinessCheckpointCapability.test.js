// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import assert from 'node:assert/strict';

const currentDir = dirname(fileURLToPath(import.meta.url));
const hookSource = readFileSync(join(currentDir, 'hooks/useKbBrowser.js'), 'utf8');
const workbenchSource = readFileSync(join(currentDir, 'KnowledgeWorkbench.jsx'), 'utf8');
const checkpointToggleSource = hookSource.slice(
  hookSource.indexOf('const setBusinessCheckpointEnabled'),
  hookSource.indexOf('const activateVersion'),
);

test('knowledge workbench exposes the package-level CheckPoint capability toggle', () => {
  assert.match(hookSource, /capabilities\/business-checkpoint/);
  assert.match(hookSource, /expected_revision: expectedRevision/);
  assert.match(hookSource, /setBusinessCheckpointEnabled/);
  assert.match(
    workbenchSource,
    /CheckPoint \{checkpointEnabled \? '已开启' : '已关闭'\}/,
  );
  assert.match(workbenchSource, /关闭 CheckPoint/);
  assert.match(workbenchSource, /CheckPoint 需要本体包配套配置才能完整生效/);
  assert.match(workbenchSource, /本体包 Checkpoint 通用规范/);
  assert.match(workbenchSource, /checkpointDialog === 'guide'/);
  assert.match(workbenchSource, /businessCheckpointGuide\.js/);
  assert.match(workbenchSource, /download="business-checkpoint-template\.md"/);
  assert.match(workbenchSource, /下载 business-checkpoint-template\.md/);
  assert.match(workbenchSource, /business-checkpoint-template\.md/);
});

test('CheckPoint cannot be enabled without an enabled package and readable template', () => {
  assert.match(
    workbenchSource,
    /!checkpointEnabled\s*&&\s*\(\s*!pkg\.enabled\s*\|\|\s*!checkpointTemplateAvailable\s*\)/,
  );
});

test('CheckPoint revision conflicts refresh package state before retry', () => {
  assert.match(
    checkpointToggleSource,
    /res\.status === 409[\s\S]*await refreshPackageList\(\)[\s\S]*setOpNotice/,
  );
});
