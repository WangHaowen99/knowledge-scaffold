import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildSystemConfigSectionPayload,
  nextSystemSettingsTab,
  SYSTEM_CONFIG_FIELDS,
  SYSTEM_SETTINGS_PANEL_SECTIONS,
  systemSettingsModuleKey,
  systemSettingsPanelId,
  systemSettingsTabId,
} from './fields.js';

test('default panel owns all non-model-pool backend sections and no removed sections', () => {
  assert.deepEqual(SYSTEM_SETTINGS_PANEL_SECTIONS, [
    'llm',
    'storage',
    'auth',
    'agent',
    'memory',
    'web_search',
    'direct_execution',
    'skill_evolution',
  ]);
  assert.equal('llm_profiles' in SYSTEM_CONFIG_FIELDS, false);
  assert.equal('observability' in SYSTEM_CONFIG_FIELDS, false);
  assert.equal('task_llm' in SYSTEM_CONFIG_FIELDS, false);
});

test('field schemas cover the backend agent and auth contracts', () => {
  assert.deepEqual(SYSTEM_CONFIG_FIELDS.auth.map((field) => field.name), [
    'auth_secret',
    'auth_token_ttl_seconds',
    'auth_server_url',
    'auth_server_admin_token',
    'auth_server_audience',
    'auth_server_issuer',
  ]);
  assert.deepEqual(SYSTEM_CONFIG_FIELDS.agent.map((field) => field.name), [
    'llm_connection_max_retries',
    'main_agent_max_iterations',
    'sub_agent_max_iterations',
    'sub_agent_max_concurrency',
    'sub_agent_max_depth',
    'parallel_default_max_workers',
    'context_max_rounds',
    'runtime_warmup_enabled',
    'evolution_min_occurrences',
    'evolution_min_applied',
    'evolution_max_rejection',
    'evolution_passive_reflection_enabled',
    'evolution_patch_auto_apply',
    'evolution_patch_cooldown_sec',
    'evolution_sweep_interval_sec',
    'execute_script_timeout',
  ]);
});

test('payload builder removes public secret flags and preserves saved secrets when inputs are blank', () => {
  assert.deepEqual(buildSystemConfigSectionPayload('web_search', {
    enabled: true,
    provider: 'tavily',
    base_url: 'https://api.tavily.com',
    api_key: '',
    has_api_key: true,
    timeout_seconds: 12,
    default_max_results: 3,
    max_results_limit: 6,
    search_depth: 'fast',
    allowed_domains: '',
    blocked_domains: '',
  }), {
    enabled: true,
    provider: 'tavily',
    base_url: 'https://api.tavily.com',
    timeout_seconds: 12,
    default_max_results: 3,
    max_results_limit: 6,
    search_depth: 'fast',
    allowed_domains: '',
    blocked_domains: '',
  });
});

test('payload builder sends new secrets or explicit supported clear flags', () => {
  assert.deepEqual(buildSystemConfigSectionPayload('llm', {
    base_url: 'https://llm.example/v1',
    model: 'chat',
    api_key: 'new-secret',
    has_api_key: true,
    timeout: 30,
    max_tokens: 1024,
    temperature: 0.2,
  }), {
    base_url: 'https://llm.example/v1',
    model: 'chat',
    api_key: 'new-secret',
    timeout: 30,
    max_tokens: 1024,
    temperature: 0.2,
  });
  assert.deepEqual(buildSystemConfigSectionPayload('auth', {
    auth_secret: '',
    has_auth_secret: true,
    clear_auth_secret: true,
    auth_token_ttl_seconds: 3600,
  }), {
    clear_auth_secret: true,
    auth_token_ttl_seconds: 3600,
  });
});

test('payload builder refuses independent model pool and removed observability ownership', () => {
  assert.throws(
    () => buildSystemConfigSectionPayload('llm_profiles', {}),
    /由独立设置功能管理/,
  );
  assert.throws(
    () => buildSystemConfigSectionPayload('observability', {}),
    /不支持的系统配置 section/,
  );
});

test('tab and panel ids stay paired while module instances only reset after an import revision', () => {
  const ids = SYSTEM_SETTINGS_PANEL_SECTIONS.map((section) => ({
    tab: systemSettingsTabId(section),
    panel: systemSettingsPanelId(section),
    module: systemSettingsModuleKey(section, 2),
  }));
  assert.equal(new Set(ids.map((item) => item.tab)).size, SYSTEM_SETTINGS_PANEL_SECTIONS.length);
  assert.equal(new Set(ids.map((item) => item.panel)).size, SYSTEM_SETTINGS_PANEL_SECTIONS.length);
  assert.equal(systemSettingsModuleKey('agent', 2), 'agent:import-2');
  assert.equal(systemSettingsModuleKey('agent', 2), systemSettingsModuleKey('agent', 2));
  assert.notEqual(systemSettingsModuleKey('agent', 2), systemSettingsModuleKey('agent', 3));
});

test('tab keyboard navigation wraps and supports Home and End', () => {
  assert.equal(nextSystemSettingsTab('llm', 'ArrowLeft'), 'skill_evolution');
  assert.equal(nextSystemSettingsTab('skill_evolution', 'ArrowRight'), 'llm');
  assert.equal(nextSystemSettingsTab('agent', 'Home'), 'llm');
  assert.equal(nextSystemSettingsTab('agent', 'End'), 'skill_evolution');
  assert.equal(nextSystemSettingsTab('agent', 'Enter'), 'agent');
});
