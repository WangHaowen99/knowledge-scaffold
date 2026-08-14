import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-user-llm-'));
process.env.MODELING_LLM_BASE_URL = 'https://default.example.com/v1';
process.env.MODELING_LLM_MODEL = 'default-model';
process.env.MODELING_LLM_MAX_TOKENS = '8192';
process.env.MODELING_TASK_LLM_BASE_URL = 'https://task.example.com/v1';
process.env.MODELING_TASK_LLM_MODEL = 'task-default-model';
process.env.MODELING_TASK_LLM_API_KEY = '';

const repo = await import('../src/settings/repo.js');
const provider = await import('../src/llm/provider.js');
const context = await import('../src/llm/user-context.js');

before(() => {
  repo.deleteUserLlmSetting('alice', 'base_url');
  repo.deleteUserLlmSetting('alice', 'model');
  repo.deleteUserLlmSetting('alice', 'max_tokens');
  repo.deleteUserLlmSetting('bob', 'base_url');
  repo.deleteUserLlmSetting('bob', 'model');
  repo.deleteUserLlmSetting('bob', 'max_tokens');
});

test('每个用户读取和保存独立的 LLM 配置', () => {
  context.runWithLlmUser('alice', () => {
    repo.setUserLlmSetting('alice', 'base_url', 'https://alice.example.com/v1');
    repo.setUserLlmSetting('alice', 'model', 'alice-model');
    repo.setUserLlmSetting('alice', 'max_tokens', '4096');
  });

  const alice = context.runWithLlmUser('alice', () => repo.getLlmSettings());
  const bob = context.runWithLlmUser('bob', () => repo.getLlmSettings());
  assert.deepEqual(alice, {
    baseUrl: 'https://alice.example.com/v1', model: 'alice-model', apiKey: '', maxTokens: 4096,
  });
  assert.deepEqual(bob, {
    baseUrl: 'https://default.example.com/v1', model: 'default-model', apiKey: '', maxTokens: 8192,
  });
  assert.equal(context.runWithLlmUser('alice', () => repo.getLlmSource()), 'user');
  assert.equal(context.runWithLlmUser('bob', () => repo.getLlmSource()), 'env');
});

test('LLM provider 和任务 provider 均使用当前用户配置', () => {
  const alice = context.runWithLlmUser('alice', () => provider.buildTaskLlm());
  const bob = context.runWithLlmUser('bob', () => provider.buildTaskLlm());
  assert.equal(alice.model.id, 'alice-model');
  assert.equal(alice.model.baseUrl, 'https://alice.example.com/v1');
  assert.equal(alice.model.maxTokens, 4096);
  assert.equal(bob.model.id, 'task-default-model');
  assert.equal(bob.model.baseUrl, 'https://task.example.com/v1');
});

test('清除用户字段后只回退环境默认值，不会读取其他用户配置', () => {
  context.runWithLlmUser('alice', () => {
    repo.deleteUserLlmSetting('alice', 'base_url');
    repo.deleteUserLlmSetting('alice', 'model');
    repo.deleteUserLlmSetting('alice', 'max_tokens');
  });
  const alice = context.runWithLlmUser('alice', () => repo.getLlmSettings());
  assert.equal(alice.baseUrl, 'https://default.example.com/v1');
  assert.equal(alice.model, 'default-model');
  assert.equal(alice.maxTokens, 8192);
});

test('个人输出上限只接受安全整数范围', () => {
  assert.throws(() => repo.setUserLlmSetting('alice', 'max_tokens', '0'), /1 到 1000000/);
  assert.throws(() => repo.setUserLlmSetting('alice', 'max_tokens', '1.5'), /1 到 1000000/);
  assert.throws(() => repo.setUserLlmSetting('alice', 'max_tokens', '1000001'), /1 到 1000000/);
});

test('并发异步任务保持各自的用户上下文', async () => {
  repo.setUserLlmSetting('alice', 'base_url', 'https://alice.example.com/v1');
  repo.setUserLlmSetting('alice', 'model', 'alice-model');
  repo.setUserLlmSetting('bob', 'base_url', 'https://bob.example.com/v1');
  repo.setUserLlmSetting('bob', 'model', 'bob-model');

  const wait = () => new Promise<void>((resolve) => setTimeout(resolve, 5));
  const [alice, bob] = await Promise.all([
    context.runWithLlmUser('alice', async () => {
      await wait();
      return provider.buildLlm().model.id;
    }),
    context.runWithLlmUser('bob', async () => {
      await wait();
      return provider.buildLlm().model.id;
    }),
  ]);

  assert.equal(alice, 'alice-model');
  assert.equal(bob, 'bob-model');
});

test('Fastify 请求 hook 为并发认证请求保留独立用户上下文', async () => {
  const app = Fastify({ logger: false });
  app.addHook('onRequest', (_request, _reply, done) => context.runWithLlmRequest(done));
  app.addHook('preHandler', async (request) => {
    context.enterLlmUser(String(request.headers['x-test-user'] ?? ''));
  });
  app.get('/context', async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 5));
    return { username: context.currentLlmUser() };
  });
  await app.ready();
  const [alice, bob] = await Promise.all([
    app.inject({ method: 'GET', url: '/context', headers: { 'x-test-user': 'alice' } }),
    app.inject({ method: 'GET', url: '/context', headers: { 'x-test-user': 'bob' } }),
  ]);
  assert.deepEqual(alice.json(), { username: 'alice' });
  assert.deepEqual(bob.json(), { username: 'bob' });
  await app.close();
});
