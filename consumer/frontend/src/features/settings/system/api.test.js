import assert from 'node:assert/strict';
import test from 'node:test';

import {
  fetchSystemConfigTemplate,
  importSystemConfig,
  loadSystemConfig,
  requestSystemConfigJson,
  saveSystemConfigSection,
  systemConfigTemplateFileName,
  testSystemConfigSection,
} from './api.js';

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

test('load adapter uses the authenticated GET system config contract', async () => {
  const calls = [];
  const request = async (path, options) => {
    calls.push({ path, options });
    return jsonResponse({ agent: { main_agent_max_iterations: 50 } });
  };

  const payload = await loadSystemConfig(request);

  assert.deepEqual(payload, { agent: { main_agent_max_iterations: 50 } });
  assert.deepEqual(calls, [{ path: '/api/system-config', options: {} }]);
});

test('save adapter sends PATCH section and the unsaved module draft', async () => {
  let call;
  const request = async (path, options) => {
    call = { path, options };
    return jsonResponse({
      section: 'agent',
      config: { main_agent_max_iterations: 32 },
    });
  };

  await saveSystemConfigSection(
    'agent',
    { main_agent_max_iterations: 32 },
    request,
  );

  assert.equal(call.path, '/api/system-config');
  assert.equal(call.options.method, 'PATCH');
  assert.equal(call.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(call.options.body), {
    section: 'agent',
    data: { main_agent_max_iterations: 32 },
  });
});

test('test adapter sends POST section and the unsaved draft and returns tested_at', async () => {
  let call;
  const request = async (path, options) => {
    call = { path, options };
    return jsonResponse({
      section: 'web_search',
      success: true,
      message: '联网搜索连接成功',
      tested_at: '2026-07-15T10:00:00Z',
    });
  };

  const payload = await testSystemConfigSection(
    'web_search',
    {
      enabled: true,
      api_key: 'draft-key',
    },
    request,
  );

  assert.equal(call.path, '/api/system-config/test');
  assert.equal(call.options.method, 'POST');
  assert.deepEqual(JSON.parse(call.options.body), {
    section: 'web_search',
    data: { enabled: true, api_key: 'draft-key' },
  });
  assert.equal(payload.tested_at, '2026-07-15T10:00:00Z');
});

test('import adapter sends only the prepared config envelope', async () => {
  let call;
  const request = async (path, options) => {
    call = { path, options };
    return jsonResponse({
      success: true,
      imported_sections: ['agent'],
      config: { agent: {} },
    });
  };

  await importSystemConfig(
    { agent: { main_agent_max_iterations: 24 } },
    request,
  );

  assert.equal(call.path, '/api/system-config/import');
  assert.equal(call.options.method, 'POST');
  assert.deepEqual(JSON.parse(call.options.body), {
    config: { agent: { main_agent_max_iterations: 24 } },
  });
});

test('request adapter preserves backend detail, status and error code', async () => {
  const request = async () =>
    jsonResponse({ detail: 'section 非法', code: 'INVALID_SECTION' }, 400);

  await assert.rejects(
    () =>
      requestSystemConfigJson(request, '/api/system-config', {}, '请求失败'),
    (error) =>
      error.message === 'section 非法' &&
      error.status === 400 &&
      error.code === 'INVALID_SECTION',
  );
});

test('template adapter keeps the backend filename and blob payload', async () => {
  const request = async (path, options) => {
    assert.equal(path, '/api/system-config/template');
    assert.equal(options, undefined);
    return new Response('{"agent":{}}', {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Content-Disposition':
          'attachment; filename="system_config.template.json"',
      },
    });
  };

  const result = await fetchSystemConfigTemplate(request);

  assert.equal(result.fileName, 'system_config.template.json');
  assert.equal(await result.blob.text(), '{"agent":{}}');
});

test('content disposition parser supports RFC 5987 and safe fallback names', () => {
  assert.equal(
    systemConfigTemplateFileName(
      "attachment; filename*=UTF-8''system%20config.json",
    ),
    'system config.json',
  );
  assert.equal(systemConfigTemplateFileName(''), 'system_config.template.json');
});

test('frontend adapter rejects removed observability before making a request', async () => {
  let called = false;
  const request = async () => {
    called = true;
    return jsonResponse({});
  };

  assert.throws(
    () => testSystemConfigSection('observability', {}, request),
    /不支持的系统配置 section/,
  );
  assert.equal(called, false);
});
