import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const read = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), 'utf8');

test('workspace status stays with its identity and no longer consumes a standalone column', () => {
  const source = read('./admin/WorkspaceAdminPanel.jsx');

  assert.doesNotMatch(source, /<th>状态<\/th>/);
  assert.match(source, /settings-admin-identity-line/);
  assert.match(source, /colSpan=\{4\}/);
});

test('user table omits the password status column and keeps current account by username', () => {
  const source = read('./admin/UserAdminPanel.jsx');

  assert.doesNotMatch(source, /<th>密码状态<\/th>/);
  assert.match(source, /settings-admin-identity-line/);
  assert.match(source, /当前账号/);
  assert.match(source, /colSpan=\{3\}/);
});

test('settings layout contracts protect key columns, controls and long hints', () => {
  const modelCss = read('./models/modelSettings.css');
  const adminCss = read('./admin/adminSettings.css');
  const systemCss = read('./system/systemConfig.css');

  assert.match(modelCss, /\.mw-model-table\s*\{[^}]*table-layout:\s*fixed/s);
  assert.match(modelCss, /\.mw-model-status-cell[\s\S]*white-space:\s*nowrap/);
  assert.match(modelCss, /\.mw-model-form-grid input\[type='number'\][\s\S]*min-height:\s*38px/);
  assert.match(adminCss, /\.settings-admin-table td\s*\{[^}]*vertical-align:\s*middle/s);
  assert.match(adminCss, /\.settings-admin-table td:last-child \.settings-admin-row-actions\s*\{[^}]*flex-wrap:\s*nowrap/s);
  assert.match(systemCss, /\.system-config-fields \.fieldrow \.fl[\s\S]*min-width:\s*0/);
  assert.match(systemCss, /\.system-config-fields \.fieldrow \.fl \.h[\s\S]*overflow-wrap:\s*anywhere/);
});
