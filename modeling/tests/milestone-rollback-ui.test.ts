import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const packageChat = readFileSync(new URL('../frontend/src/PackageChat.jsx', import.meta.url), 'utf8');
const dialog = readFileSync(new URL('../frontend/src/MilestoneRollbackDialog.jsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../frontend/src/authoring-api.js', import.meta.url), 'utf8');

test('建模助手顶部提供阶段回退菜单并明确区分流程回退', () => {
  assert.match(packageChat, /回退阶段/);
  assert.match(packageChat, /这是流程回退，不是只切换页面/);
  assert.match(packageChat, /previewAiConstructionMilestoneRollback/);
});

test('阶段回退弹窗同时展示保留项、失效项和必填原因', () => {
  assert.match(dialog, /会保留/);
  assert.match(dialog, /需要重新确认或生成/);
  assert.match(dialog, /回退原因（必填）/);
  assert.match(dialog, /当前版本和之后的历史版本不会被删除/);
});

test('前端通过专用预览和执行接口提交阶段回退', () => {
  assert.match(api, /rollback-preview/);
  assert.match(api, /rollbackAiConstructionMilestone/);
});
