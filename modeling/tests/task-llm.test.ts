import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'task-llm-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';
process.env.MODELING_LLM_CONTEXT_WINDOW = '1000000';
process.env.MODELING_TASK_LLM_BASE_URL = 'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1';
process.env.MODELING_TASK_LLM_MODEL = 'glm-5.2';
process.env.MODELING_TASK_LLM_API_KEY = 'test-task-key';

const { buildTaskLlm } = await import('../src/llm/provider.js');

test('任务模型配置完整时优先于页面主模型', () => {
  const llm = buildTaskLlm();

  assert.equal(llm.model.id, 'glm-5.2');
  assert.equal(llm.model.baseUrl, 'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1');
  assert.equal(llm.model.contextWindow, 1000000);
  assert.equal(llm.getApiKey('modeling-llm'), 'test-task-key');
});

test('后台任务可以限制单次模型输出上限', () => {
  const llm = buildTaskLlm({ maxTokensCap: 4096 });

  assert.equal(llm.model.maxTokens, 4096);
});

test('阿里云结构化后台任务在真实请求体中关闭思考模式', async () => {
  const originalFetch = globalThis.fetch;
  let requestBody: Record<string, unknown> | undefined;
  globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
    requestBody = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    const chunks = [
      { id: 'chatcmpl-test', model: 'glm-5.2', choices: [{ index: 0, delta: { role: 'assistant', content: '完成' }, finish_reason: null }] },
      { id: 'chatcmpl-test', model: 'glm-5.2', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
    ];
    return new Response(`${chunks.map((item) => `data: ${JSON.stringify(item)}\n\n`).join('')}data: [DONE]\n\n`, {
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
    });
  }) as typeof fetch;
  try {
    const llm = buildTaskLlm({ maxTokensCap: 4096, disableThinking: true });
    const stream = llm.streamFn(llm.model, { systemPrompt: 'test', messages: [], tools: [] }, {
      apiKey: llm.getApiKey('modeling-llm'),
      onPayload: (payload: Record<string, unknown>) => ({ ...payload, temperature: 0 }),
    });
    for await (const _event of stream) { /* consume SSE */ }
    assert.equal(requestBody?.enable_thinking, false);
    assert.equal(requestBody?.temperature, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
