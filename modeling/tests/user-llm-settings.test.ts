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
process.env.MODELING_LLM_CONTEXT_WINDOW = '128000';
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
  repo.deleteUserLlmSetting('alice', 'context_window');
  repo.deleteUserLlmSetting('bob', 'base_url');
  repo.deleteUserLlmSetting('bob', 'model');
  repo.deleteUserLlmSetting('bob', 'max_tokens');
  repo.deleteUserLlmSetting('bob', 'context_window');
});

test('每个用户读取和保存独立的 LLM 配置', () => {
  context.runWithLlmUser('alice', () => {
    repo.setUserLlmSetting('alice', 'base_url', 'https://alice.example.com/v1');
    repo.setUserLlmSetting('alice', 'model', 'alice-model');
    repo.setUserLlmSetting('alice', 'max_tokens', '4096');
    repo.setUserLlmSetting('alice', 'context_window', '1000000');
  });

  const alice = context.runWithLlmUser('alice', () => repo.getLlmSettings());
  const bob = context.runWithLlmUser('bob', () => repo.getLlmSettings());
  assert.deepEqual(alice, {
    baseUrl: 'https://alice.example.com/v1', model: 'alice-model', apiKey: '',
    maxTokens: 4096, contextWindow: 1000000,
    thinkingMode: 'default', reasoningEffort: '', extraBodyJson: '{}',
  });
  assert.deepEqual(bob, {
    baseUrl: 'https://default.example.com/v1', model: 'default-model', apiKey: '',
    maxTokens: 8192, contextWindow: 128000,
    thinkingMode: 'default', reasoningEffort: '', extraBodyJson: '{}',
  });
  assert.equal(context.runWithLlmUser('alice', () => repo.getLlmSource()), 'user');
  assert.equal(context.runWithLlmUser('bob', () => repo.getLlmSource()), 'env');
});

test('后台任务模型未配置时使用当前用户的主模型配置', () => {
  const alice = context.runWithLlmUser('alice', () => provider.buildTaskLlm());
  const bob = context.runWithLlmUser('bob', () => provider.buildTaskLlm());
  assert.equal(alice.model.id, 'alice-model');
  assert.equal(alice.model.baseUrl, 'https://alice.example.com/v1');
  assert.equal(alice.model.maxTokens, 4096);
  assert.equal(alice.model.contextWindow, 1000000);
  assert.equal(bob.model.id, 'default-model');
  assert.equal(bob.model.baseUrl, 'https://default.example.com/v1');
  assert.equal(bob.model.contextWindow, 128000);
});

test('每个用户可独立配置后台任务模型，清空后重新使用主模型', () => {
  repo.updateUserLlmSettings('task-owner', {
    baseUrl: 'https://main-model.example.com/v1',
    model: 'main-model',
    apiKey: 'synthetic-main-key',
    maxTokens: '4096',
    contextWindow: '128000',
  });
  repo.updateUserLlmSettings('task-owner', {
    baseUrl: 'https://background-model.example.com/v1',
    model: 'background-model',
    apiKey: 'synthetic-background-key',
    maxTokens: '2048',
    contextWindow: '64000',
  }, 'task');

  assert.deepEqual(repo.getLlmSettings('task-owner'), {
    baseUrl: 'https://main-model.example.com/v1', model: 'main-model',
    apiKey: 'synthetic-main-key', maxTokens: 4096, contextWindow: 128000,
    thinkingMode: 'default', reasoningEffort: '', extraBodyJson: '{}',
  });
  assert.deepEqual(repo.getTaskLlmSettings('task-owner'), {
    baseUrl: 'https://background-model.example.com/v1', model: 'background-model',
    apiKey: 'synthetic-background-key', maxTokens: 2048, contextWindow: 64000,
    thinkingMode: 'default', reasoningEffort: '', extraBodyJson: '{}',
  });
  assert.equal(repo.getTaskLlmSource('task-owner'), 'user');

  repo.updateUserLlmSettings('task-owner', {
    providerClear: true,
    maxTokens: '',
    contextWindow: '',
  }, 'task');
  assert.deepEqual(repo.getTaskLlmSettings('task-owner'), repo.getLlmSettings('task-owner'));
  assert.equal(repo.getTaskLlmSource('task-owner'), 'main');
  assert.equal(repo.getUserLlmSetting('task-owner', 'api_key', 'task'), null);
});

test('清除用户字段后只回退环境默认值，不会读取其他用户配置', () => {
  context.runWithLlmUser('alice', () => {
    repo.deleteUserLlmSetting('alice', 'base_url');
    repo.deleteUserLlmSetting('alice', 'model');
    repo.deleteUserLlmSetting('alice', 'max_tokens');
    repo.deleteUserLlmSetting('alice', 'context_window');
  });
  const alice = context.runWithLlmUser('alice', () => repo.getLlmSettings());
  assert.equal(alice.baseUrl, 'https://default.example.com/v1');
  assert.equal(alice.model, 'default-model');
  assert.equal(alice.maxTokens, 8192);
  assert.equal(alice.contextWindow, 128000);
});

test('个人输出上限只接受安全整数范围', () => {
  assert.throws(() => repo.setUserLlmSetting('alice', 'max_tokens', '0'), /1 到 1000000/);
  assert.throws(() => repo.setUserLlmSetting('alice', 'max_tokens', '1.5'), /1 到 1000000/);
  assert.throws(() => repo.setUserLlmSetting('alice', 'max_tokens', '1000001'), /1 到 1000000/);
});

test('个人 provider patch 原子校验并可整体恢复默认', () => {
  assert.throws(() => repo.updateUserLlmSettings('charlie', {
    baseUrl: 'https://partial.example.com/v1',
  }), /Base URL 与模型名必须同时设置/);
  assert.equal(repo.getUserLlmSetting('charlie', 'base_url'), null);

  assert.throws(() => repo.updateUserLlmSettings('charlie', {
    baseUrl: 'file:///tmp/not-an-llm-endpoint', model: 'invalid-model',
  }), /http\(s\)/);
  assert.equal(repo.getUserLlmSetting('charlie', 'base_url'), null);

  repo.updateUserLlmSettings('charlie', {
    baseUrl: 'https://charlie.example.com/v1',
    model: 'charlie-model',
    apiKey: 'synthetic-charlie-key',
    maxTokens: '2048',
    contextWindow: '262144',
    thinkingMode: 'enabled',
    reasoningEffort: 'high',
    extraBodyJson: '{"parallel_tool_calls":false}',
  });
  assert.deepEqual(context.runWithLlmUser('charlie', () => repo.getLlmSettings()), {
    baseUrl: 'https://charlie.example.com/v1',
    model: 'charlie-model',
    apiKey: 'synthetic-charlie-key',
    maxTokens: 2048,
    contextWindow: 262144,
    thinkingMode: 'enabled',
    reasoningEffort: 'high',
    extraBodyJson: '{"parallel_tool_calls":false}',
  });

  repo.updateUserLlmSettings('charlie', {
    providerClear: true,
    maxTokens: '',
    contextWindow: '',
    thinkingMode: 'default',
    reasoningEffort: '',
    extraBodyJson: '{}',
  });
  const inherited = context.runWithLlmUser('charlie', () => repo.getLlmSettings());
  assert.equal(inherited.baseUrl, 'https://default.example.com/v1');
  assert.equal(inherited.model, 'default-model');
  assert.equal(inherited.maxTokens, 8192);
  assert.equal(inherited.contextWindow, 128000);
  assert.equal(repo.getUserLlmSetting('charlie', 'api_key'), null);
  assert.equal(repo.getUserLlmSetting('charlie', 'thinking_mode'), null);
  assert.equal(repo.getUserLlmSetting('charlie', 'extra_body_json'), null);
});

test('个人上下文窗口接受 API 支持的安全整数', () => {
  repo.setUserLlmSetting('alice', 'context_window', '1000000');
  assert.equal(repo.getLlmSettings('alice').contextWindow, 1000000);
  assert.throws(() => repo.setUserLlmSetting('alice', 'context_window', '0'), /大于等于 1/);
  assert.throws(() => repo.setUserLlmSetting('alice', 'context_window', '1.5'), /大于等于 1/);
  repo.deleteUserLlmSetting('alice', 'context_window');
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
