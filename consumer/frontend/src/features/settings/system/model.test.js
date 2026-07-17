import assert from 'node:assert/strict';
import test from 'node:test';

import {
  SYSTEM_CONFIG_SECTIONS,
  assertSystemConfigSection,
  configValuesEqual,
  createSystemConfigModuleState,
  maskSystemConfigSecrets,
  parseSystemConfigImport,
  prepareSystemConfigImport,
  reduceSystemConfigModuleState,
} from './model.js';

test('legal sections match the backend contract and exclude removed observability fields', () => {
  assert.deepEqual(SYSTEM_CONFIG_SECTIONS, [
    'llm',
    'llm_profiles',
    'storage',
    'auth',
    'agent',
    'memory',
    'web_search',
    'direct_execution',
    'skill_evolution',
  ]);
  assert.equal(assertSystemConfigSection(' AGENT '), 'agent');
  assert.throws(() => assertSystemConfigSection('observability'), /不支持的系统配置 section/);
  assert.throws(() => assertSystemConfigSection('task_llm'), /不支持的系统配置 section/);
});

test('import preparation strips blank secret placeholders without mutating the source', () => {
  const source = {
    llm: { base_url: 'https://llm.example/v1', model: 'chat', api_key: '' },
    auth: { auth_secret: ' ', auth_server_admin_token: null, auth_token_ttl_seconds: 3600 },
    web_search: { enabled: true, api_key: '' },
    llm_profiles: {
      default_main_profile_ref: { scope: 'global', id: 'main' },
      default_task_profile_ref: null,
      profiles: [{ id: 'main', model: 'chat', api_key: '' }],
    },
  };

  const preview = prepareSystemConfigImport(source);

  assert.equal(source.llm.api_key, '');
  assert.equal(source.llm_profiles.profiles[0].api_key, '');
  assert.equal('api_key' in preview.config.llm, false);
  assert.equal('auth_secret' in preview.config.auth, false);
  assert.equal('auth_server_admin_token' in preview.config.auth, false);
  assert.equal('api_key' in preview.config.web_search, false);
  assert.equal('api_key' in preview.config.llm_profiles.profiles[0], false);
  assert.deepEqual(preview.removedSecretPaths, [
    'llm.api_key',
    'llm_profiles.profiles.0.api_key',
    'auth.auth_secret',
    'auth.auth_server_admin_token',
    'web_search.api_key',
  ]);
});

test('import preview masks supplied secrets at every supported nested level', () => {
  const preview = prepareSystemConfigImport({
    auth: { auth_secret: 'do-not-render', auth_server_admin_token: 'admin-token' },
    llm_profiles: {
      default_main_profile_ref: { scope: 'global', id: 'main' },
      profiles: [{ id: 'main', api_key: 'profile-secret', metadata: { access_token: 'nested-token' } }],
    },
  });

  const rendered = JSON.stringify(preview.maskedConfig);
  assert.doesNotMatch(rendered, /do-not-render|admin-token|profile-secret|nested-token/);
  assert.equal(preview.config.auth.auth_secret, 'do-not-render');
  assert.equal(preview.maskedConfig.auth.auth_secret, '••••••••');
  assert.equal(preview.maskedConfig.llm_profiles.profiles[0].metadata.access_token, '••••••••');
});

test('mask helper protects secret-looking fields without changing regular values', () => {
  assert.deepEqual(maskSystemConfigSecrets({
    endpoint: 'https://example.test',
    max_tokens: 4096,
    auth_token_ttl_seconds: 3600,
    password: 'private',
    nested: { credential: 'private-too' },
  }), {
    endpoint: 'https://example.test',
    max_tokens: 4096,
    auth_token_ttl_seconds: 3600,
    password: '••••••••',
    nested: { credential: '••••••••' },
  });
});

test('empty model template is omitted so it cannot erase or invalidate an existing model pool', () => {
  const preview = prepareSystemConfigImport({
    llm_profiles: {
      default_main_profile_ref: null,
      default_task_profile_ref: null,
      profiles: [],
    },
    agent: { main_agent_max_iterations: 20 },
  });

  assert.deepEqual(preview.sections, ['agent']);
  assert.equal('llm_profiles' in preview.config, false);
  assert.deepEqual(preview.omittedSections, [{
    section: 'llm_profiles',
    reason: '模板中的空模型列表不会覆盖现有模型池',
  }]);
  assert.throws(() => prepareSystemConfigImport({
    llm_profiles: {
      default_main_profile_ref: { scope: 'global', id: 'missing' },
      profiles: [],
    },
  }), /至少需要一个模型/);
});

test('unsupported sections and malformed documents are blocked before import', () => {
  assert.throws(
    () => prepareSystemConfigImport({ agent: {}, observability: {} }),
    /不支持的 section：observability/,
  );
  assert.throws(() => prepareSystemConfigImport([]), /必须是 JSON 对象/);
  assert.throws(() => prepareSystemConfigImport({ agent: [] }), /agent 必须是 JSON 对象/);
  assert.throws(() => parseSystemConfigImport('{invalid'), /不是有效的 JSON/);
  assert.throws(() => prepareSystemConfigImport({
    llm_profiles: { default_main_profile_ref: null, default_task_profile_ref: null, profiles: [] },
  }), /只包含空模型模板/);
});

test('module reducer tracks dirty drafts, save results and tested_at independently', () => {
  let state = createSystemConfigModuleState({ enabled: false }, false);
  assert.equal(configValuesEqual(state.baseline, state.draft), true);

  state = reduceSystemConfigModuleState(state, { type: 'PATCH_DRAFT', data: { enabled: true } });
  assert.equal(state.draft.enabled, true);
  assert.equal(configValuesEqual(state.baseline, state.draft), false);

  state = reduceSystemConfigModuleState(state, { type: 'BEGIN_ACTION', action: 'test' });
  state = reduceSystemConfigModuleState(state, {
    type: 'TEST_FINISHED',
    result: { success: false, message: '连接失败', tested_at: '2026-07-15T10:00:00Z' },
  });
  assert.deepEqual(state.testStatus, {
    success: false,
    message: '连接失败',
    tested_at: '2026-07-15T10:00:00Z',
  });
  assert.equal(state.error, '连接失败');

  state = reduceSystemConfigModuleState(state, { type: 'BEGIN_ACTION', action: 'save' });
  state = reduceSystemConfigModuleState(state, {
    type: 'SAVE_SUCCEEDED',
    data: { enabled: true },
    message: '配置已保存',
  });
  assert.deepEqual(state.baseline, { enabled: true });
  assert.deepEqual(state.draft, { enabled: true });
  assert.equal(state.success, '配置已保存');
  assert.equal(state.testStatus.tested_at, null);
});

test('read-only module state ignores action starts but still accepts new server data', () => {
  let state = createSystemConfigModuleState({ enabled: false }, true);
  state = reduceSystemConfigModuleState(state, { type: 'BEGIN_ACTION', action: 'save' });
  assert.equal(state.busyAction, '');

  state = reduceSystemConfigModuleState(state, {
    type: 'SOURCE_CHANGED',
    data: { enabled: true },
    readOnly: true,
  });
  assert.deepEqual(state.draft, { enabled: true });
  assert.equal(state.readOnly, true);
});
