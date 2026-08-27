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

  assert.match(source, /config=\{llmPurpose === 'main' \? s\.llm : s\.taskLlm\}/);
  assert.match(source, /syncDefaultLlm\(purpose\)/);
  assert.match(source, /使用平台默认配置/);
  assert.match(source, /disabled=\{!config\.platformDefaultAvailable \|\| syncing \|\| testing\}/);
  assert.match(source, /CircleHelp/);
  assert.match(source, /清除当前用途的个人覆盖，改用 admin 统一配置的平台默认。/);
  assert.match(source, /className="cfg-sync-help" tabIndex=\{0\}/);
  const styles = await import('node:fs').then(({ readFileSync }) => (
    readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8')
  ));
  assert.match(styles, /\.cfg-sync-help::after \{[^}]*white-space: nowrap;/s);
  assert.match(styles, /\.cfg-sync-help::after \{[^}]*padding: 7px 14px;/s);
  assert.doesNotMatch(styles, /\.cfg-sync-help::after \{[^}]*max-width:/s);
});

test('settings modal: 主模型与后台任务模型可切换且仅后台任务模型显示用途 Tag', async () => {
  const { readFileSync } = await import('node:fs');
  const { default: LlmPurposeTabs, LLM_PURPOSE_HELP } = await import('../frontend/src/LlmPurposeTabs.jsx');
  const settings = readFileSync(new URL('../frontend/src/SettingsModal.jsx', import.meta.url), 'utf8');
  const purposeTabs = readFileSync(new URL('../frontend/src/LlmPurposeTabs.jsx', import.meta.url), 'utf8');
  const adminDefaults = readFileSync(new URL('../frontend/src/AdminLlmDefaults.jsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');

  assert.match(settings, /taskLlmBaseUrl: d\.taskLlm\.baseUrl/);
  assert.match(settings, /llmPurpose: purpose/);
  assert.match(settings, /<LlmPurposeTabs purpose=\{purpose\}/);
  assert.match(adminDefaults, /<LlmPurposeTabs/);
  assert.match(purposeTabs, />主模型</);
  assert.match(purposeTabs, />后台任务模型</);
  assert.match(purposeTabs, /主模型用于核心建模、资料理解和知识生成/);
  assert.match(purposeTabs, /后台任务模型用于流程草稿、评测判分、诊断优化等辅助任务/);
  assert.match(purposeTabs, /后台任务模型未配置时自动使用主模型配置/);
  assert.match(purposeTabs, /purpose === 'task'/);
  assert.match(purposeTabs, /className="tag tag-lang cfg-purpose-tag" role="note"/);
  assert.doesNotMatch(purposeTabs, /CircleHelp|cfg-purpose-help|data-tooltip/);
  assert.match(styles, /\.cfg-purpose-tag \{[^}]*white-space: normal;/s);
  assert.doesNotMatch(styles, /\.cfg-purpose-help/);

  const mainView = LlmPurposeTabs({ purpose: 'main', onChange() {} });
  const taskView = LlmPurposeTabs({ purpose: 'task', onChange() {} });
  assert.equal(mainView.props.children[1], false);
  assert.equal(taskView.props.children[1].props.children, LLM_PURPOSE_HELP);
});

test('admin LLM 默认配置的测试与保存按钮等宽并右对齐', async () => {
  const { readFileSync } = await import('node:fs');
  const component = readFileSync(new URL('../frontend/src/AdminLlmDefaults.jsx', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');

  assert.match(component, /className="admin-llm-primary-actions"/);
  assert.match(component, /保存后立即作为所有账号的运行时默认/);
  assert.match(styles, /\.admin-llm-primary-actions \{[^}]*grid-template-columns: repeat\(2, 112px\);[^}]*margin-left: auto;/s);
  assert.match(styles, /\.admin-llm-primary-actions \.btn \{[^}]*width: 100%;[^}]*justify-content: center;/s);
});

test('settings modal: 项目上下文会暴露 MCP 接入 tab 并接线到项目级 API', async () => {
  const { readFileSync } = await import('node:fs');
  const app = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
  const settings = readFileSync(new URL('../frontend/src/SettingsModal.jsx', import.meta.url), 'utf8');
  const api = readFileSync(new URL('../frontend/src/api.js', import.meta.url), 'utf8');
  const styles = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');

  assert.match(app, /<SettingsModal user=\{user\} projectId=\{projectId\} tab=\{settingsTab\}/);
  assert.match(settings, /projectId, tab: tabProp = 'llm'/);
  assert.match(settings, /\{projectId && <button className=\{tab === 'mcp' \? 'on' : ''\} onClick=\{\(\) => setTab\('mcp'\)\}>MCP 接入<\/button>\}/);
  assert.match(settings, /getProjectMcpRuntimeStatus\(projectId\)/);
  assert.match(settings, /listProjectMcpTokens\(projectId\)/);
  assert.match(settings, /createProjectMcpToken\(projectId, draft\)/);
  assert.match(settings, /revokeProjectMcpToken\(projectId, tokenId\)/);
  assert.match(settings, /MODELING_MCP_PUBLIC_BASE_URL/);
  assert.match(settings, /key=\{`\$\{item\.toolset\}:\$\{item\.transport\}`\}/);
  assert.match(api, /export const getProjectMcpRuntimeStatus = \(id\) => api\(`\$\{P\(id\)\}\/mcp\/runtime\/status`\)/);
  assert.match(api, /export const listProjectMcpTokens = \(id\) => api\(`\$\{P\(id\)\}\/mcp\/tokens`\)/);
  assert.match(api, /export const createProjectMcpToken = \(id, draft\) => api\(`\$\{P\(id\)\}\/mcp\/tokens`/);
  assert.match(api, /export const revokeProjectMcpToken = \(id, tokenId\) => api\(`\$\{P\(id\)\}\/mcp\/tokens\/\$\{encodeURIComponent\(tokenId\)\}`/);
  assert.match(styles, /\.cfg-mcp \{ display: grid; gap: 14px; \}/);
  assert.match(styles, /\.cfg-mcp-token-row \{/);
});
