import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'task-llm-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';
process.env.MODELING_LLM_CONTEXT_WINDOW = '1000000';
process.env.MODELING_TASK_LLM_BASE_URL = 'https://task-model.example/v1';
process.env.MODELING_TASK_LLM_MODEL = 'deepseek-v4-pro';
process.env.MODELING_TASK_LLM_API_KEY = 'test-task-key';

const { buildTaskLlm } = await import('../src/llm/provider.js');

test('任务模型配置完整时优先于页面主模型', () => {
  const llm = buildTaskLlm();

  assert.equal(llm.model.id, 'deepseek-v4-pro');
  assert.equal(llm.model.baseUrl, 'https://task-model.example/v1');
  assert.equal(llm.model.contextWindow, 1000000);
  assert.equal(llm.getApiKey('modeling-llm'), 'test-task-key');
});

test('后台任务可以限制单次模型输出上限', () => {
  const llm = buildTaskLlm({ maxTokensCap: 4096 });

  assert.equal(llm.model.maxTokens, 4096);
});
