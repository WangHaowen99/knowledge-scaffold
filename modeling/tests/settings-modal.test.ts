import { test } from 'node:test';
import assert from 'node:assert/strict';

test('settings modal: backdrop does not close the modal', async () => {
  const { SettingsModalOverlay } = await import('../frontend/src/SettingsModal.jsx');
  const child = { type: 'modal-content' };

  const overlay = SettingsModalOverlay({ children: child });

  assert.equal(overlay.props.className, 'modal-ov');
  assert.equal(overlay.props.onClick, undefined);
  assert.equal(overlay.props.children, child);
});

test('settings modal: admin 角色拥有独立的平台默认作用域', async () => {
  const source = await import('node:fs').then(({ readFileSync }) => (
    readFileSync(new URL('../frontend/src/SettingsModal.jsx', import.meta.url), 'utf8')
  ));

  assert.match(source, /user\?\.role === 'admin'/);
  assert.match(source, />平台默认</);
  assert.match(source, />我的覆盖</);
  assert.match(source, /只提交用户实际编辑的字段/);
});

test('settings modal: 个人 LLM 可主动同步平台默认并按可用状态禁用', async () => {
  const source = await import('node:fs').then(({ readFileSync }) => (
    readFileSync(new URL('../frontend/src/SettingsModal.jsx', import.meta.url), 'utf8')
  ));

  assert.match(source, /platformDefaultAvailable=\{s\.llm\.platformDefaultAvailable\}/);
  assert.match(source, /syncDefaultLlm\(\)/);
  assert.match(source, /同步默认 LLM 配置/);
  assert.match(source, /disabled=\{!platformDefaultAvailable \|\| syncing \|\| testing\}/);
  assert.match(source, /CircleHelp/);
  assert.match(source, /只有 admin 账号设置了默认 LLM 配置后，才可使用。/);
  assert.match(source, /className="cfg-sync-help" tabIndex=\{0\}/);
  const styles = await import('node:fs').then(({ readFileSync }) => (
    readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8')
  ));
  assert.match(styles, /\.cfg-sync-help::after \{[^}]*white-space: nowrap;/s);
  assert.match(styles, /\.cfg-sync-help::after \{[^}]*padding: 7px 14px;/s);
  assert.doesNotMatch(styles, /\.cfg-sync-help::after \{[^}]*max-width:/s);
});

test('admin LLM 默认配置的测试与保存按钮等宽并右对齐', async () => {
  const { readFileSync } = await import('node:fs');
  const component = readFileSync(new URL('../frontend/src/AdminLlmDefaults.jsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');

  assert.match(component, /className="admin-llm-primary-actions"/);
  assert.match(component, /保存只更新默认模板，不会修改已有用户/);
  assert.match(styles, /\.admin-llm-primary-actions \{[^}]*grid-template-columns: repeat\(2, 112px\);[^}]*margin-left: auto;/s);
  assert.match(styles, /\.admin-llm-primary-actions \.btn \{[^}]*width: 100%;[^}]*justify-content: center;/s);
});
