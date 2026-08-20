import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-admin-llm-'));
process.env.MODELING_LLM_BASE_URL = 'https://deployment.example.com/v1';
process.env.MODELING_LLM_MODEL = 'deployment-model';
process.env.MODELING_LLM_API_KEY = 'synthetic-deployment-key';
process.env.MODELING_TASK_LLM_BASE_URL = 'https://deployment-task.example.com/v1';
process.env.MODELING_TASK_LLM_MODEL = 'deployment-task-model';
process.env.MODELING_TASK_LLM_API_KEY = 'synthetic-deployment-task-key';
process.env.MODELING_LLM_CONFIG_ENCRYPTION_KEY = 'synthetic-test-only-encryption-key-32-bytes';

const { registerAdminLlmRoutes } = await import('../src/settings/admin-llm-routes.js');
const defaultsRepo = await import('../src/settings/llm-defaults-repo.js');
const { decryptLlmDefaultApiKey } = await import('../src/settings/llm-secret-codec.js');
const settingsRepo = await import('../src/settings/repo.js');
const { getDb } = await import('../src/db/index.js');
const { runWithLlmUser } = await import('../src/llm/user-context.js');
const { buildLlm, buildTaskLlm } = await import('../src/llm/provider.js');
const {
  initializeNewUserLlmDefaults,
  syncUserLlmToPlatformDefault,
} = await import('../src/settings/llm-default-sync.js');

let app: FastifyInstance;

before(async () => {
  app = Fastify({ logger: false });
  app.addHook('preHandler', async (request) => {
    request.user = {
      username: String(request.headers['x-test-user'] ?? 'anonymous'),
      role: String(request.headers['x-test-role'] ?? 'user'),
    };
  });
  registerAdminLlmRoutes(app);
  await app.ready();
});

after(async () => {
  await app.close();
});

const adminHeaders = { 'x-test-user': 'platform-admin', 'x-test-role': 'admin' };

test('普通用户不能读取或修改平台 LLM 默认配置', async () => {
  const read = await app.inject({ method: 'GET', url: '/api/admin/llm-defaults' });
  assert.equal(read.statusCode, 403);

  const write = await app.inject({
    method: 'PUT',
    url: '/api/admin/llm-defaults',
    payload: { purpose: 'main', baseUrl: 'faux', model: 'forbidden', maxTokens: 1024 },
  });
  assert.equal(write.statusCode, 403);
  assert.equal(defaultsRepo.getPublicLlmDefaultSettings('main'), undefined);
});

test('admin 保存的平台 Key 仅以密文落库且接口不回显', async () => {
  const response = await app.inject({
    method: 'PUT',
    url: '/api/admin/llm-defaults',
    headers: adminHeaders,
    payload: {
      purpose: 'main',
      baseUrl: 'https://platform.example.com/v1/',
      model: 'platform-model',
      apiKey: 'synthetic-platform-key',
      maxTokens: 12000,
      expectedRevision: 0,
    },
  });
  assert.equal(response.statusCode, 200);
  assert.doesNotMatch(response.body, /synthetic-platform-key|enc:v1/);

  const stored = getDb().prepare(`SELECT api_key_ciphertext
    FROM llm_default_settings WHERE purpose = 'main'`).get() as { api_key_ciphertext: string };
  assert.match(stored.api_key_ciphertext, /^enc:v1:/);
  assert.doesNotMatch(stored.api_key_ciphertext, /synthetic-platform-key/);
  assert.throws(() => decryptLlmDefaultApiKey(stored.api_key_ciphertext, 'task'));

  const read = await app.inject({ method: 'GET', url: '/api/admin/llm-defaults', headers: adminHeaders });
  assert.equal(read.statusCode, 200);
  assert.deepEqual(read.json().defaults.main, {
    purpose: 'main',
    baseUrl: 'https://platform.example.com/v1',
    model: 'platform-model',
    maxTokens: 12000,
    contextWindow: 1000000,
    apiKeySet: true,
    revision: 1,
    updatedBy: 'platform-admin',
    updatedAt: read.json().defaults.main.updatedAt,
  });
  assert.doesNotMatch(read.body, /synthetic-platform-key|enc:v1/);
  assert.equal(settingsRepo.getUserLlmSetting('platform-admin', 'base_url'), null);
});

test('admin 保存模板不干扰老用户，新用户初始化后固化加密快照', () => {
  const untouched = runWithLlmUser('existing-user', () => settingsRepo.getLlmSettings());
  assert.equal(untouched.model, 'deployment-model');
  assert.equal(runWithLlmUser('existing-user', () => settingsRepo.getLlmSource()), 'env');

  assert.equal(initializeNewUserLlmDefaults('new-user'), true);
  const initialized = runWithLlmUser('new-user', () => settingsRepo.getLlmSettings());
  assert.deepEqual(initialized, {
    baseUrl: 'https://platform.example.com/v1',
    model: 'platform-model',
    apiKey: 'synthetic-platform-key',
    maxTokens: 12000,
    contextWindow: 1000000,
  });
  assert.equal(runWithLlmUser('new-user', () => settingsRepo.getLlmSource()), 'default_snapshot');
  assert.equal(runWithLlmUser('new-user', () => buildLlm().model.id), 'platform-model');
  const snapshot = getDb().prepare(`SELECT api_key_ciphertext
    FROM user_llm_default_snapshots WHERE username = ? AND purpose = 'main'`)
    .get('new-user') as { api_key_ciphertext: string };
  assert.match(snapshot.api_key_ciphertext, /^enc:v1:/);
  assert.doesNotMatch(snapshot.api_key_ciphertext, /synthetic-platform-key/);

  assert.equal(initializeNewUserLlmDefaults('snapshot-editor'), true);
  settingsRepo.updateUserLlmSettings('snapshot-editor', {
    baseUrl: 'https://custom.example.com/v1', model: 'custom-model',
  });
  const personal = runWithLlmUser('snapshot-editor', () => buildLlm());
  assert.equal(personal.model.baseUrl, 'https://custom.example.com/v1');
  assert.equal(personal.model.id, 'custom-model');
  assert.equal(personal.getApiKey('modeling-llm'), undefined);
  assert.equal(runWithLlmUser('snapshot-editor', () => settingsRepo.getLlmSource()), 'user');
});

test('任务模型只读取用户初始化/同步时固化的 task/main 快照', async () => {
  const mainFallback = runWithLlmUser('task-user', () => buildTaskLlm());
  assert.equal(mainFallback.model.id, 'deployment-task-model');

  assert.equal(initializeNewUserLlmDefaults('task-user'), true);
  const initializedBeforeTaskDefault = runWithLlmUser('task-user', () => buildTaskLlm());
  assert.equal(initializedBeforeTaskDefault.model.id, 'platform-model');
  assert.equal(initializedBeforeTaskDefault.getApiKey('modeling-llm'), 'synthetic-platform-key');

  const saved = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'task', baseUrl: 'https://platform-task.example.com/v1',
      model: 'platform-task-model', apiKey: 'synthetic-platform-task-key',
      maxTokens: 4096, expectedRevision: 0,
    },
  });
  assert.equal(saved.statusCode, 200);
  const unchanged = runWithLlmUser('task-user', () => buildTaskLlm());
  assert.equal(unchanged.model.id, 'platform-model');

  assert.equal(initializeNewUserLlmDefaults('new-task-user'), true);
  const initializedAfterTaskDefault = runWithLlmUser('new-task-user', () => buildTaskLlm());
  assert.equal(initializedAfterTaskDefault.model.id, 'platform-task-model');
  assert.equal(initializedAfterTaskDefault.model.baseUrl, 'https://platform-task.example.com/v1');
  assert.equal(initializedAfterTaskDefault.getApiKey('modeling-llm'), 'synthetic-platform-task-key');

  syncUserLlmToPlatformDefault('task-user');
  assert.equal(runWithLlmUser('task-user', () => buildTaskLlm()).model.id, 'platform-task-model');
});

test('revision 防止覆盖其他管理员的新修改，非法草稿不产生半写入', async () => {
  const conflict = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'https://stale.example.com/v1', model: 'stale-model',
      maxTokens: 2048, expectedRevision: 0,
    },
  });
  assert.equal(conflict.statusCode, 409);

  const invalid = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'https://invalid.example.com/v1', model: '',
      maxTokens: 0, expectedRevision: 1,
    },
  });
  assert.equal(invalid.statusCode, 400);
  assert.equal(defaultsRepo.getPublicLlmDefaultSettings('main')?.baseUrl, 'https://platform.example.com/v1');
  assert.equal(defaultsRepo.getPublicLlmDefaultSettings('main')?.revision, 1);
});

test('admin 更新模板不追写已有用户，主动同步才替换该用户快照', async () => {
  const updated = await app.inject({
    method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
    payload: {
      purpose: 'main', baseUrl: 'https://platform-v2.example.com/v1', model: 'platform-model-v2',
      apiKey: 'synthetic-platform-key-v2', maxTokens: 16000, expectedRevision: 1,
    },
  });
  assert.equal(updated.statusCode, 200);

  const beforeSync = runWithLlmUser('new-user', () => settingsRepo.getLlmSettings());
  assert.equal(beforeSync.model, 'platform-model');
  assert.equal(beforeSync.maxTokens, 12000);
  assert.equal(runWithLlmUser('existing-user', () => settingsRepo.getLlmSettings()).model, 'deployment-model');

  syncUserLlmToPlatformDefault('new-user');
  const afterSync = runWithLlmUser('new-user', () => settingsRepo.getLlmSettings());
  assert.equal(afterSync.model, 'platform-model-v2');
  assert.equal(afterSync.maxTokens, 16000);
});

test('admin 清除模板不影响快照，没有快照的用户继续使用部署配置', async () => {
  for (const purpose of ['task', 'main'] as const) {
    const current = defaultsRepo.getPublicLlmDefaultSettings(purpose)!;
    const response = await app.inject({
      method: 'PUT', url: '/api/admin/llm-defaults', headers: adminHeaders,
      payload: { purpose, enabled: false, expectedRevision: current.revision },
    });
    assert.equal(response.statusCode, 200);
  }
  const main = runWithLlmUser('fallback-user', () => settingsRepo.getLlmSettings());
  const task = runWithLlmUser('fallback-user', () => settingsRepo.getTaskLlmSettings());
  assert.equal(main.model, 'deployment-model');
  assert.equal(task.model, 'deployment-task-model');
  assert.equal(runWithLlmUser('new-user', () => settingsRepo.getLlmSettings()).model, 'platform-model-v2');
});
