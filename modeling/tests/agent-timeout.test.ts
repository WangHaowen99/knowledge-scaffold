import assert from 'node:assert/strict';
import test from 'node:test';
import { createFauxCore, fauxAssistantMessage } from '@earendil-works/pi-ai/providers/faux';

import { makeAgent, ModelContextLengthExceededError, runAgent } from '../src/workflow/agent.js';

test('模型任务超过时限会中止并返回可识别的超时错误', async () => {
  let aborted = false;
  const agent = {
    state: { messages: [] },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => new Promise<void>(() => undefined),
    abort: () => { aborted = true; },
  };

  const outcome = await Promise.race([
    runAgent(agent as any, '执行原子修订', undefined, { timeoutMs: 20 })
      .then(() => 'completed', (error) => error),
    new Promise((resolve) => setTimeout(() => resolve('hung'), 150)),
  ]);

  assert.notEqual(outcome, 'hung');
  assert.match(String((outcome as Error)?.message ?? outcome), /模型任务.*超时/);
  assert.equal((outcome as any)?.code, 'MODEL_REQUEST_TIMEOUT');
  assert.equal(aborted, true);
});

test('模型流把供应商错误写入 agent state 时必须向调用方抛出', async () => {
  const agent = {
    state: {
      messages: [{ role: 'assistant', stopReason: 'error', errorMessage: '429 insufficient_quota', content: [] }],
      errorMessage: '429 insufficient_quota',
    },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => undefined,
    abort: () => undefined,
  };

  await assert.rejects(
    runAgent(agent as any, '执行诊断'),
    /429 insufficient_quota/,
  );
});

test('同一 Agent 后续成功时不会重新抛出历史供应商错误', async () => {
  const core = createFauxCore({ provider: 'synthetic-error-history', models: [{ id: 'synthetic-error-history' }] });
  core.setResponses([
    fauxAssistantMessage('合成失败响应。', {
      stopReason: 'error',
      errorMessage: 'synthetic provider failure',
    }),
    fauxAssistantMessage('合成恢复响应。'),
  ]);
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [],
  );

  await assert.rejects(runAgent(agent, '执行合成失败轮次。'), /synthetic provider failure/u);
  await assert.doesNotReject(runAgent(agent, '执行合成恢复轮次。'));
  assert.equal(core.state.callCount, 2);
});

test('模型上下文超限会归一化为可识别错误', async () => {
  const state = {
    model: { contextWindow: 1_000_000 },
    messages: [] as unknown[],
  };
  const agent = {
    state,
    subscribe: () => () => undefined,
    prompt: async () => {
      state.messages.push({ role: 'assistant', stopReason: 'error', errorMessage: 'context_length_exceeded', content: [] });
    },
    waitForIdle: async () => undefined,
    abort: () => undefined,
  };

  await assert.rejects(
    runAgent(agent as any, '读取资料'),
    (error: unknown) => error instanceof ModelContextLengthExceededError
      && error.code === 'context_length_exceeded'
      && error.contextWindow === 1_000_000
      && error.message.includes('context_length_exceeded'),
  );
});

test('嵌套供应商错误对象中的上下文超限码也能被识别', async () => {
  const agent = {
    state: {
      model: { contextWindow: 1_000_000 },
      messages: [],
      errorMessage: { error: { code: 'context_length_exceeded' } },
    },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => undefined,
    abort: () => undefined,
  };

  await assert.rejects(
    runAgent(agent as any, '读取资料'),
    (error: unknown) => error instanceof ModelContextLengthExceededError,
  );
});

test('取消模型任务会等待底层运行完成中断清理后再返回', async () => {
  const controller = new AbortController();
  let releaseRun: () => void = () => undefined;
  let waitStarted: () => void = () => undefined;
  const started = new Promise<void>((resolve) => { waitStarted = resolve; });
  const running = new Promise<void>((resolve) => { releaseRun = resolve; });
  let aborted = false;
  const agent = {
    state: { messages: [] },
    subscribe: () => () => undefined,
    prompt: async () => undefined,
    waitForIdle: async () => {
      waitStarted();
      await running;
    },
    abort: () => { aborted = true; },
  };

  let settled = false;
  const outcome = runAgent(agent as any, '执行评测判分', undefined, { signal: controller.signal })
    .then(() => undefined, (error) => error)
    .finally(() => { settled = true; });
  await started;
  controller.abort(new Error('本轮评测已由用户停止'));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(aborted, true);
  assert.equal(settled, false);
  releaseRun();
  const error = await outcome;
  assert.match(String(error?.message ?? error), /本轮评测已由用户停止/);
});
