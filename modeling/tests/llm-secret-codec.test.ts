import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dataDir = mkdtempSync(join(tmpdir(), 'modeling-llm-secret-'));
process.env.DATA_DIR = dataDir;
delete process.env.MODELING_LLM_CONFIG_ENCRYPTION_KEY;

const defaultsRepo = await import('../src/settings/llm-defaults-repo.js');
const codec = await import('../src/settings/llm-secret-codec.js');
const { config } = await import('../src/config.js');
const { getDb } = await import('../src/db/index.js');

test('环境变量为空时自动生成持久化密钥并保持平台 API Key 密文落库', () => {
  const saved = defaultsRepo.saveLlmDefaultSettings({
    purpose: 'main',
    baseUrl: 'https://keyless.example.com/v1',
    model: 'keyless-model',
    apiKey: 'synthetic-must-not-persist',
    maxTokens: 4096,
    expectedRevision: 0,
  }, 'platform-admin');

  assert.equal(saved.apiKeySet, true);
  assert.equal(codec.llmConfigEncryptionKeyMode(), 'managed');
  assert.equal(defaultsRepo.getLlmDefaultSettings('main')?.apiKey, 'synthetic-must-not-persist');

  const row = getDb().prepare(`SELECT api_key_ciphertext
    FROM llm_default_settings WHERE purpose = 'main'`).get() as { api_key_ciphertext: string };
  assert.match(row.api_key_ciphertext, /^enc:v1:/);
  assert.doesNotMatch(row.api_key_ciphertext, /synthetic-must-not-persist/);

  const keyPath = join(dataDir, codec.MANAGED_LLM_CONFIG_ENCRYPTION_KEY_FILE);
  const managedKey = readFileSync(keyPath, 'utf8');
  assert.ok(Buffer.byteLength(managedKey, 'utf8') >= 32);
  if (process.platform !== 'win32') assert.equal(statSync(keyPath).mode & 0o777, 0o600);
});

test('显式配置不足 32 字节时拒绝静默改用自动密钥', () => {
  const previous = config.llmConfigEncryptionKey;
  config.llmConfigEncryptionKey = 'too-short';
  try {
    assert.throws(() => defaultsRepo.saveLlmDefaultSettings({
      purpose: 'task',
      baseUrl: 'https://invalid-key.example.com/v1',
      model: 'invalid-key-model',
      apiKey: 'synthetic-key',
      maxTokens: 4096,
      expectedRevision: 0,
    }, 'platform-admin'), codec.LlmConfigEncryptionKeyError);
  } finally {
    config.llmConfigEncryptionKey = previous;
  }
  assert.equal(defaultsRepo.getPublicLlmDefaultSettings('task'), undefined);
});
