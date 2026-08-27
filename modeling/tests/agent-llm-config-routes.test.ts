import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';

const serviceToken = 'service-token-for-agent-runtime-test';
process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-agent-llm-config-'));
process.env.MODELING_SERVICE_TOKEN = serviceToken;
process.env.MODELING_LLM_CONFIG_ENCRYPTION_KEY = 'synthetic-test-only-encryption-key-32-bytes';
delete process.env.MODELING_LLM_BASE_URL;
delete process.env.MODELING_LLM_MODEL;
delete process.env.MODELING_LLM_API_KEY;

const { registerAgentLlmConfigRoutes } = await import('../src/agent/llm-config-routes.js');
const { saveLlmDefaultSettings } = await import('../src/settings/llm-defaults-repo.js');
const { updateUserLlmSettings } = await import('../src/settings/repo.js');

let app: FastifyInstance;

before(async () => {
  saveLlmDefaultSettings({
    purpose: 'main',
    baseUrl: 'https://platform.example/v1',
    model: 'platform-model',
    apiKey: 'platform-secret',
    maxTokens: 24000,
    contextWindow: 128000,
  }, 'admin');
  app = Fastify({ logger: false });
  app.addHook('preHandler', async (request) => {
    request.user = { username: String(request.headers['x-test-user'] ?? 'existing-user'), role: 'modeler' };
  });
  registerAgentLlmConfigRoutes(app);
  await app.ready();
});

after(async () => {
  await app.close();
});

test('浏览器身份不能读取 Agent 运行时 API Key', async () => {
  const response = await app.inject({ method: 'GET', url: '/internal/agent/llm-config' });
  assert.equal(response.statusCode, 403);
  assert.doesNotMatch(response.body, /platform-secret/);
});

test('受信 Agent 读取 admin 统一配置且个人覆盖优先', async () => {
  const platform = await app.inject({
    method: 'GET',
    url: '/internal/agent/llm-config',
    headers: { 'x-modeling-service-token': serviceToken },
  });
  assert.equal(platform.statusCode, 200);
  assert.equal(platform.headers['cache-control'], 'no-store');
  assert.deepEqual(platform.json().llm, {
    baseUrl: 'https://platform.example/v1',
    model: 'platform-model',
    apiKey: 'platform-secret',
    maxTokens: 24000,
    contextWindow: 128000,
    source: 'platform_default',
  });

  updateUserLlmSettings('custom-user', {
    baseUrl: 'https://custom.example/v1',
    model: 'custom-model',
    apiKey: 'custom-secret',
  });
  const personal = await app.inject({
    method: 'GET',
    url: '/internal/agent/llm-config',
    headers: {
      'x-modeling-service-token': serviceToken,
      'x-test-user': 'custom-user',
    },
  });
  assert.equal(personal.statusCode, 200);
  assert.equal(personal.json().llm.model, 'custom-model');
  assert.equal(personal.json().llm.apiKey, 'custom-secret');
  assert.equal(personal.json().llm.source, 'user');
});
