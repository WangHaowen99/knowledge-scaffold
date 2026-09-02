import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { Type } from '@earendil-works/pi-ai';
import { streamSimple as streamOpenAICompletions } from '@earendil-works/pi-ai/api/openai-completions';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

import {
  AgentCaptureAttemptTracker,
  type AgentCaptureAttemptDiagnostics,
  sanitizeToolValidationError,
} from '../src/workflow/agent-capture-diagnostics.js';
import {
  AgentCaptureError,
  isToolChoiceCompatibilityError,
  makeAgent,
  nextStructuredToolChoiceMode,
  runAgent,
  runAgentCaptured,
  type StructuredToolChoiceMode,
} from '../src/workflow/agent.js';

const TARGET_TOOL = 'submit_synthetic_analysis';
const RAW_ARGUMENT_MARKER = 'SYNTHETIC_RAW_ARGUMENT_MARKER';

test('makeAgent 按工具名称去重并保留最后一个定义', () => {
  const core = createFauxCore({ provider: 'deduplicate-tools', models: [{ id: 'deduplicate-tools' }] });
  const first = { ...syntheticSubmitTool(), label: '旧定义' };
  const latest = { ...syntheticSubmitTool(), label: '最新定义' };

  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [first, latest],
  );

  assert.equal(agent.state.tools.length, 1);
  assert.equal(agent.state.tools[0]?.label, '最新定义');
});

function turnEnd(stopReason: string, input = 0, output = 0) {
  return {
    type: 'turn_end',
    message: { stopReason, usage: { input, output } },
  };
}

function validationFailure(toolName = TARGET_TOOL, field = 'summary') {
  return [
    `Validation failed for tool "${toolName}":`,
    `  - ${field}: Expected string`,
    '',
    'Received arguments:',
    JSON.stringify({ marker: RAW_ARGUMENT_MARKER }),
  ].join('\n');
}

function latestUserPrompt(context: any): string {
  const message = [...(context.messages ?? [])].reverse().find((item: any) => item?.role === 'user');
  if (typeof message?.content === 'string') return message.content;
  return Array.isArray(message?.content)
    ? message.content.map((block: any) => block?.type === 'text' ? block.text : '').join('\n')
    : '';
}

function syntheticSubmitTool(onSubmit: () => void = () => {}) {
  return {
    name: TARGET_TOOL,
    label: '提交合成分析',
    description: '提交完全合成的分析结果。',
    parameters: Type.Object({ summary: Type.String() }),
    async execute() {
      onSubmit();
      return { content: [{ type: 'text' as const, text: '已提交。' }], details: {}, terminate: true };
    },
  };
}

test('makeAgent 仅在显式配置时强制指定已注册工具', async () => {
  const forcedCore = createFauxCore({
    api: 'openai-completions',
    provider: 'synthetic-required-tool',
    models: [{ id: 'synthetic-required-tool' }],
  });
  let forcedOptions: any;
  forcedCore.setResponses([(_context, options) => {
    forcedOptions = options;
    return fauxAssistantMessage('合成响应。');
  }]);
  const forcedAgent = makeAgent(
    { model: forcedCore.getModel(), streamFn: forcedCore.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool()],
    [],
    { requiredToolName: TARGET_TOOL },
  );
  await runAgent(forcedAgent, '分析合成输入。');

  assert.deepEqual(forcedOptions?.toolChoice, {
    type: 'function',
    function: { name: TARGET_TOOL },
  });

  const defaultCore = createFauxCore({ provider: 'synthetic-default-tool-choice', models: [{ id: 'synthetic-default-tool-choice' }] });
  let defaultOptions: any;
  defaultCore.setResponses([(_context, options) => {
    defaultOptions = options;
    return fauxAssistantMessage('合成响应。');
  }]);
  const defaultAgent = makeAgent(
    { model: defaultCore.getModel(), streamFn: defaultCore.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool()],
  );
  await runAgent(defaultAgent, '分析合成输入。');

  assert.equal(Object.hasOwn(defaultOptions ?? {}, 'toolChoice'), false);
});

test('makeAgent 为 OpenAI-compatible 映射 named required auto 三段工具选择', async () => {
  const scenarios: Array<{ mode: StructuredToolChoiceMode; expected: unknown }> = [
    { mode: 'named', expected: { type: 'function', function: { name: TARGET_TOOL } } },
    { mode: 'required', expected: 'required' },
    { mode: 'auto', expected: 'auto' },
  ];

  for (const scenario of scenarios) {
    const core = createFauxCore({
      api: 'openai-completions',
      provider: `synthetic-openai-tool-choice-${scenario.mode}`,
      models: [{ id: `synthetic-openai-tool-choice-${scenario.mode}` }],
    });
    let receivedOptions: Record<string, unknown> | undefined;
    core.setResponses([(_context, options) => {
      receivedOptions = options as Record<string, unknown>;
      return fauxAssistantMessage('合成响应。');
    }]);
    const agent = makeAgent(
      { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
      '仅处理完全合成的测试输入。',
      [syntheticSubmitTool()],
      [],
      { requiredToolName: TARGET_TOOL, toolChoiceMode: scenario.mode },
    );

    await runAgent(agent, '分析合成输入。');
    assert.deepEqual(receivedOptions?.toolChoice, scenario.expected);
  }
});

test('工具选择兼容错误只匹配明确拒绝并按 named required auto 有界推进', () => {
  const compatibilityErrors = [
    new Error('invalid_request_error: named tool_choice is not supported'),
    new Error('tool_choice must be one of auto or none'),
    new Error('具名工具选择不支持，请使用自动模式'),
  ];
  for (const error of compatibilityErrors) {
    assert.equal(isToolChoiceCompatibilityError(error), true);
  }
  assert.equal(isToolChoiceCompatibilityError(new Error('Validation failed for tool "submit_synthetic_analysis"')), false);
  assert.equal(isToolChoiceCompatibilityError(new Error('429 rate limit exceeded')), false);
  assert.equal(isToolChoiceCompatibilityError(new Error('context_length_exceeded')), false);
  assert.equal(nextStructuredToolChoiceMode('named'), 'required');
  assert.equal(nextStructuredToolChoiceMode('required'), 'auto');
  assert.equal(nextStructuredToolChoiceMode('auto'), undefined);
});

test('runAgentCaptured 在同次尝试中降级工具选择并复用协商结果', async () => {
  const core = createFauxCore({
    api: 'openai-completions',
    provider: 'synthetic-tool-choice-fallback',
    models: [{ id: 'synthetic-tool-choice-fallback' }],
  });
  const receivedModes: string[] = [];
  const fallbackEvents: Array<{ fromMode: string; toMode: string }> = [];
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  let captured = false;
  const streamFn = ((model: unknown, context: unknown, options: any) => {
    const mode = typeof options?.toolChoice === 'object'
      ? 'named'
      : String(options?.toolChoice ?? 'none');
    receivedModes.push(mode);
    if (mode === 'named' || mode === 'required') {
      throw new Error(`invalid_request_error: tool_choice mode ${mode} is not supported`);
    }
    core.setResponses([
      fauxAssistantMessage([fauxToolCall(TARGET_TOOL, { summary: '合成降级结果' })]),
    ]);
    return core.streamSimple(model as any, context as any, options);
  }) as any;
  const agent = makeAgent(
    { model: core.getModel(), streamFn, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool(() => { captured = true; })],
  );
  const toolChoiceState: { mode: StructuredToolChoiceMode } = { mode: 'named' };

  await runAgentCaptured(
    agent,
    '提交合成结果。',
    () => captured,
    '请提交合成结果。',
    (event: any) => {
      if (event?.type === 'tool_choice_fallback') fallbackEvents.push(event);
    },
    {
      maxCaptureAttempts: 2,
      requireCapture: true,
      requiredToolName: TARGET_TOOL,
      toolChoiceState,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  );

  assert.equal(captured, true);
  assert.deepEqual(receivedModes, ['named', 'required', 'auto']);
  assert.deepEqual(fallbackEvents.map(({ fromMode, toMode }) => [fromMode, toMode]), [
    ['named', 'required'],
    ['required', 'auto'],
  ]);
  assert.equal(toolChoiceState.mode, 'auto');
  assert.deepEqual(reports.map(({ attempt, outcome, toolChoiceMode }) => ({
    attempt, outcome, toolChoiceMode,
  })), [{ attempt: 1, outcome: 'captured', toolChoiceMode: 'auto' }]);
});

test('makeAgent 按 provider API 映射工具选择能力', async () => {
  const scenarios: Array<{ api: string; expected?: unknown }> = [
    {
      api: 'openai-completions',
      expected: { type: 'function', function: { name: TARGET_TOOL } },
    },
    {
      api: 'mistral-conversations',
      expected: { type: 'function', function: { name: TARGET_TOOL } },
    },
    {
      api: 'anthropic-messages',
      expected: { type: 'tool', name: TARGET_TOOL },
    },
    {
      api: 'bedrock-converse-stream',
      expected: { type: 'tool', name: TARGET_TOOL },
    },
    { api: 'google-generative-ai', expected: 'any' },
    { api: 'google-vertex', expected: 'any' },
    { api: 'openai-responses' },
    { api: 'azure-openai-responses' },
    { api: 'openai-codex-responses' },
    { api: 'synthetic-custom-api' },
  ];

  for (const scenario of scenarios) {
    const core = createFauxCore({
      api: scenario.api,
      provider: `synthetic-tool-choice-${scenario.api}`,
      models: [{ id: `synthetic-tool-choice-${scenario.api}` }],
    });
    let receivedOptions: Record<string, unknown> | undefined;
    core.setResponses([(_context, options) => {
      receivedOptions = options as Record<string, unknown>;
      return fauxAssistantMessage('合成响应。');
    }]);
    const agent = makeAgent(
      { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
      '仅处理完全合成的测试输入。',
      [syntheticSubmitTool()],
      [],
      { requiredToolName: TARGET_TOOL },
    );

    await runAgent(agent, '分析合成输入。');

    if (scenario.expected === undefined) {
      assert.equal(Object.hasOwn(receivedOptions ?? {}, 'toolChoice'), false, scenario.api);
    } else {
      assert.deepEqual(receivedOptions?.toolChoice, scenario.expected, scenario.api);
    }
  }
});

test('makeAgent 强制的目标工具未注册时立即拒绝', () => {
  const core = createFauxCore({ provider: 'synthetic-missing-required-tool', models: [{ id: 'synthetic-missing-required-tool' }] });
  assert.throws(() => makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [],
    [],
    { requiredToolName: TARGET_TOOL },
  ), /强制调用的工具未注册/);
  assert.equal(core.state.callCount, 0);
});

test('length 结束且未调用目标工具时识别为输出截断', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe(turnEnd('length', 120, 45));

  assert.deepEqual(tracker.finish(false), {
    attempt: 1,
    outcome: 'output_truncated',
    targetToolName: TARGET_TOOL,
    stopReason: 'length',
    targetToolStartedCount: 0,
    targetToolSucceededCount: 0,
    targetToolFailedCount: 0,
    inputTokens: 120,
    outputTokens: 45,
  });
});

test('正常结束但未调用目标工具时识别为未提交', () => {
  const tracker = new AgentCaptureAttemptTracker(2, TARGET_TOOL);
  tracker.observe(turnEnd('stop'));

  assert.equal(tracker.finish(false).outcome, 'tool_not_called');
});

test('目标工具校验失败时记录失败次数和脱敏摘要', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe({ type: 'tool_execution_start', toolName: TARGET_TOOL });
  tracker.observe({
    type: 'tool_execution_end',
    toolName: TARGET_TOOL,
    isError: true,
    result: { content: [{ type: 'text', text: validationFailure() }] },
  });
  tracker.observe(turnEnd('stop'));

  const diagnostics = tracker.finish(false);
  assert.equal(diagnostics.outcome, 'tool_validation_failed');
  assert.equal(diagnostics.targetToolStartedCount, 1);
  assert.equal(diagnostics.targetToolFailedCount, 1);
  assert.match(diagnostics.validationError ?? '', /summary: Expected string/);
  assert.doesNotMatch(diagnostics.validationError ?? '', new RegExp(RAW_ARGUMENT_MARKER));
  assert.doesNotMatch(diagnostics.validationError ?? '', /Received arguments/);
});

test('length 结束优先于已经写入的捕获结果', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe({ type: 'tool_execution_start', toolName: TARGET_TOOL });
  tracker.observe({ type: 'tool_execution_end', toolName: TARGET_TOOL, isError: false, result: {} });
  tracker.observe(turnEnd('length'));

  const diagnostics = tracker.finish(true);
  assert.equal(diagnostics.outcome, 'output_truncated');
  assert.equal(diagnostics.targetToolSucceededCount, 1);
});

test('同次尝试的目标工具失败优先于成功并保留首个校验摘要', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe({
    type: 'tool_execution_end',
    toolName: TARGET_TOOL,
    isError: true,
    result: { content: [{ type: 'text', text: validationFailure(TARGET_TOOL, 'summary') }] },
  });
  tracker.observe({ type: 'tool_execution_end', toolName: TARGET_TOOL, isError: false, result: {} });
  tracker.observe({
    type: 'tool_execution_end',
    toolName: TARGET_TOOL,
    isError: true,
    result: { content: [{ type: 'text', text: validationFailure(TARGET_TOOL, 'details') }] },
  });

  const diagnostics = tracker.finish(true);
  assert.equal(diagnostics.outcome, 'tool_validation_failed');
  assert.equal(diagnostics.targetToolSucceededCount, 1);
  assert.equal(diagnostics.targetToolFailedCount, 2);
  assert.match(diagnostics.validationError ?? '', /summary: Expected string/u);
  assert.doesNotMatch(diagnostics.validationError ?? '', /details: Expected string/u);
});

test('旧 capture 未伴随本次目标工具成功时不视为 captured', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);

  assert.equal(tracker.finish(true).outcome, 'tool_not_called');
});

test('其他工具调用不计入目标工具统计', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe({ type: 'tool_execution_start', toolName: 'read_synthetic_source' });
  tracker.observe({ type: 'tool_execution_end', toolName: 'read_synthetic_source', isError: false });
  tracker.observe(turnEnd('stop'));

  const diagnostics = tracker.finish(false);
  assert.equal(diagnostics.outcome, 'tool_not_called');
  assert.equal(diagnostics.targetToolStartedCount, 0);
  assert.equal(diagnostics.targetToolSucceededCount, 0);
  assert.equal(diagnostics.targetToolFailedCount, 0);
});

test('同一次捕获尝试保留最严重 stopReason 并聚合多个 turn 的 token', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe(turnEnd('length', 100, 20));
  tracker.observe(turnEnd('stop', 150, 30));

  const diagnostics = tracker.finish(false);
  assert.equal(diagnostics.outcome, 'output_truncated');
  assert.equal(diagnostics.stopReason, 'length');
  assert.equal(diagnostics.inputTokens, 250);
  assert.equal(diagnostics.outputTokens, 50);
});

test('已观察到 length 时模型异常标记不会降低 stopReason 优先级', () => {
  const tracker = new AgentCaptureAttemptTracker(1, TARGET_TOOL);
  tracker.observe(turnEnd('length', 100, 20));

  const diagnostics = tracker.finish(false, { modelError: true });
  assert.equal(diagnostics.outcome, 'output_truncated');
  assert.equal(diagnostics.stopReason, 'length');
});

test('校验摘要只保留错误字段，不包含原始参数', () => {
  const summary = sanitizeToolValidationError(validationFailure());

  assert.match(summary ?? '', /^Validation failed for tool/);
  assert.match(summary ?? '', /summary: Expected string/);
  assert.doesNotMatch(summary ?? '', new RegExp(RAW_ARGUMENT_MARKER));
  assert.equal(sanitizeToolValidationError(`ordinary error ${RAW_ARGUMENT_MARKER}`), undefined);
});

test('runAgentCaptured 为每次外层捕获尝试报告诊断', async () => {
  const core = createFauxCore({ provider: 'synthetic-capture-attempts', models: [{ id: 'synthetic-capture-attempts' }] });
  core.setResponses([
    fauxAssistantMessage('本轮未提交结构化结果。'),
    fauxAssistantMessage('修正后仍未提交结构化结果。'),
  ]);
  const reports: Array<{ attempt: number; outcome: string }> = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [],
  );

  await runAgentCaptured(
    agent,
    '分析合成输入。',
    () => false,
    '提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 2,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  );

  assert.equal(core.state.callCount, 2);
  assert.deepEqual(reports.map(({ attempt, outcome }) => ({ attempt, outcome })), [
    { attempt: 1, outcome: 'tool_not_called' },
    { attempt: 2, outcome: 'tool_not_called' },
  ]);
});

test('length 后使用截断修正提示自动重试并成功捕获', async () => {
  const core = createFauxCore({ provider: 'synthetic-length-recovery', models: [{ id: 'synthetic-length-recovery' }] });
  const prompts: string[] = [];
  core.setResponses([
    (context) => {
      prompts.push(latestUserPrompt(context));
      return fauxAssistantMessage('合成输出未完成。', { stopReason: 'length' });
    },
    (context) => {
      prompts.push(latestUserPrompt(context));
      return fauxAssistantMessage([fauxToolCall(TARGET_TOOL, { summary: '合成结果' })]);
    },
  ]);
  let captured = false;
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool(() => { captured = true; })],
    [],
    { requiredToolName: TARGET_TOOL },
  );

  await runAgentCaptured(
    agent,
    '分析合成输入。',
    () => captured,
    '请提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 3,
      requireCapture: true,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  );

  assert.equal(core.state.callCount, 2);
  assert.deepEqual(reports.map((item) => item.outcome), ['output_truncated', 'captured']);
  assert.equal(reports[0].stopReason, 'length');
  assert.match(prompts[1], /达到输出长度上限/);
  assert.match(prompts[1], /必填字段和 JSON 结构完整/);
  assert.match(prompts[1], /不要重复分析原文/);
});

test('目标工具成功但 stopReason 为 length 时清理旧 capture 后重试', async () => {
  const core = createFauxCore({ provider: 'synthetic-truncated-capture', models: [{ id: 'synthetic-truncated-capture' }] });
  core.setResponses([
    fauxAssistantMessage(
      [fauxToolCall(TARGET_TOOL, { summary: '合成截断结果' })],
      { stopReason: 'length' },
    ),
    fauxAssistantMessage([fauxToolCall(TARGET_TOOL, { summary: '合成完整结果' })]),
  ]);
  let captured = false;
  let resetCount = 0;
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool(() => { captured = true; })],
  );

  await runAgentCaptured(
    agent,
    '分析合成输入。',
    () => captured,
    '请提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 2,
      requireCapture: true,
      resetCapture: () => {
        resetCount += 1;
        captured = false;
      },
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  );

  assert.equal(core.state.callCount, 2);
  assert.equal(resetCount, 1);
  assert.equal(captured, true);
  assert.deepEqual(reports.map((item) => item.outcome), ['output_truncated', 'captured']);
});

test('真实 OpenAI Completions SSE 截断后重置 partial capture 并自动恢复', async () => {
  let requestCount = 0;
  const requestPaths: string[] = [];
  const requestPayloads: Array<Record<string, unknown>> = [];
  const server = createServer((request, response) => {
    requestCount += 1;
    const attempt = requestCount;
    requestPaths.push(request.url ?? '');
    let requestBody = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => { requestBody += chunk; });
    request.on('end', () => {
      requestPayloads.push(JSON.parse(requestBody) as Record<string, unknown>);

      const shared = {
        id: `synthetic-completion-id-${attempt}`,
        object: 'chat.completion.chunk',
        created: attempt,
        model: 'synthetic-openai-model',
      };
      const chunks = [
        {
          ...shared,
          choices: [{
            index: 0,
            delta: {
              role: 'assistant',
              tool_calls: [{
                index: 0,
                id: `synthetic-tool-call-id-${attempt}`,
                type: 'function',
                function: {
                  name: TARGET_TOOL,
                  arguments: attempt === 1
                    ? '{"summary":"synthetic unterminated value'
                    : '{"summary":"synthetic complete value"}',
                },
              }],
            },
            finish_reason: null,
          }],
        },
        {
          ...shared,
          choices: [{
            index: 0,
            delta: {},
            finish_reason: attempt === 1 ? 'length' : 'tool_calls',
          }],
        },
      ];

      response.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'close',
      });
      for (const chunk of chunks) response.write(`data: ${JSON.stringify(chunk)}\n\n`);
      response.end('data: [DONE]\n\n');
    });
  });

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });

  try {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const reports: AgentCaptureAttemptDiagnostics[] = [];
    let captured = false;
    let resetCount = 0;
    const agent = makeAgent(
      {
        model: {
          id: 'synthetic-openai-model',
          name: 'Synthetic OpenAI Model',
          api: 'openai-completions',
          provider: 'synthetic-openai-compatible',
          baseUrl: `http://127.0.0.1:${address.port}/v1`,
          reasoning: false,
          input: ['text'],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 4_096,
          maxTokens: 1_024,
          compat: { supportsUsageInStreaming: false },
        },
        streamFn: streamOpenAICompletions,
        getApiKey: () => 'your-api-key-here',
      },
      '仅处理完全合成的测试输入。',
      [syntheticSubmitTool(() => { captured = true; })],
      [],
      { requiredToolName: TARGET_TOOL },
    );

    await runAgentCaptured(
      agent,
      '分析合成输入。',
      () => captured,
      '提交合成结果。',
      undefined,
      {
        maxCaptureAttempts: 2,
        requireCapture: true,
        resetCapture: () => {
          resetCount += 1;
          captured = false;
        },
        captureDiagnostics: {
          targetToolName: TARGET_TOOL,
          onAttempt: (diagnostics) => reports.push(diagnostics),
        },
      },
    );

    assert.equal(requestCount, 2);
    assert.deepEqual(requestPaths, ['/v1/chat/completions', '/v1/chat/completions']);
    assert.equal(requestPayloads.length, 2);
    for (const payload of requestPayloads) {
      assert.deepEqual(payload.tool_choice, {
        type: 'function',
        function: { name: TARGET_TOOL },
      });
    }
    assert.equal(resetCount, 1);
    assert.equal(captured, true);
    assert.equal(reports.length, 2);
    assert.deepEqual(reports.map((report) => report.outcome), ['output_truncated', 'captured']);
    assert.equal(reports[0].outcome, 'output_truncated');
    assert.equal(reports[0].stopReason, 'length');
    assert.equal(reports[0].targetToolStartedCount, 1);
    assert.equal(reports[0].targetToolSucceededCount, 1);
    assert.equal(reports[0].targetToolFailedCount, 0);
    assert.equal(reports[0].validationError, undefined);
    assert.equal(reports[1].stopReason, 'toolUse');
    assert.equal(reports[1].targetToolSucceededCount, 1);
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
      server.closeAllConnections();
    });
  }
});

test('工具参数校验失败后只反馈脱敏字段错误并自动修正', async () => {
  const core = createFauxCore({ provider: 'synthetic-validation-recovery', models: [{ id: 'synthetic-validation-recovery' }] });
  const prompts: string[] = [];
  core.setResponses([
    (context) => {
      prompts.push(latestUserPrompt(context));
      return fauxAssistantMessage(
        [fauxToolCall(TARGET_TOOL, { marker: RAW_ARGUMENT_MARKER })],
        { stopReason: 'toolUse' },
      );
    },
    (context) => {
      prompts.push(latestUserPrompt(context));
      return fauxAssistantMessage([fauxToolCall(TARGET_TOOL, { summary: '合成修正结果' })]);
    },
  ]);
  let captured = false;
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool(() => { captured = true; })],
    [],
    { requiredToolName: TARGET_TOOL },
  );

  await runAgentCaptured(
    agent,
    '分析合成输入。',
    () => captured,
    '请提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 3,
      requireCapture: true,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  );

  assert.equal(core.state.callCount, 2);
  assert.deepEqual(reports.map((item) => item.outcome), ['tool_validation_failed', 'captured']);
  assert.equal(reports[0].stopReason, 'toolUse');
  assert.match(prompts[1], /工具参数未通过校验/);
  assert.match(prompts[1], /summary/);
  assert.doesNotMatch(prompts[1], new RegExp(RAW_ARGUMENT_MARKER));
  assert.doesNotMatch(prompts[1], /Received arguments/);
});

test('未调用目标工具后使用对应提示自动重试', async () => {
  const core = createFauxCore({ provider: 'synthetic-missing-tool-recovery', models: [{ id: 'synthetic-missing-tool-recovery' }] });
  const prompts: string[] = [];
  core.setResponses([
    (context) => {
      prompts.push(latestUserPrompt(context));
      return fauxAssistantMessage('仅返回合成正文。');
    },
    (context) => {
      prompts.push(latestUserPrompt(context));
      return fauxAssistantMessage([fauxToolCall(TARGET_TOOL, { summary: '合成结果' })]);
    },
  ]);
  let captured = false;
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool(() => { captured = true; })],
  );

  await runAgentCaptured(
    agent,
    '分析合成输入。',
    () => captured,
    '请提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 2,
      requireCapture: true,
      captureDiagnostics: { targetToolName: TARGET_TOOL, onAttempt: () => {} },
    },
  );

  assert.match(prompts[1], /上一次未调用目标提交工具/);
  assert.match(prompts[1], new RegExp(TARGET_TOOL));
});

test('三类结构化捕获失败在重试耗尽后使用稳定错误码', async () => {
  for (const scenario of [
    { provider: 'synthetic-exhausted-length', stopReason: 'length' as const, code: 'STRUCTURED_OUTPUT_TRUNCATED' },
    { provider: 'synthetic-exhausted-no-tool', stopReason: 'stop' as const, code: 'STRUCTURED_TOOL_NOT_CALLED' },
  ]) {
    const core = createFauxCore({ provider: scenario.provider, models: [{ id: scenario.provider }] });
    core.setResponses([fauxAssistantMessage('合成失败响应。', { stopReason: scenario.stopReason })]);
    const agent = makeAgent(
      { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
      '仅处理完全合成的测试输入。',
      [],
    );

    await assert.rejects(runAgentCaptured(
      agent,
      '分析合成输入。',
      () => false,
      '请提交合成结果。',
      undefined,
      {
        maxCaptureAttempts: 1,
        requireCapture: true,
        captureDiagnostics: { targetToolName: TARGET_TOOL, onAttempt: () => {} },
      },
    ), (error: unknown) => {
      assert.ok(error instanceof AgentCaptureError);
      assert.equal(error.code, scenario.code);
      assert.equal(error.maxAttempts, 1);
      return true;
    });
  }

  const validationCore = createFauxCore({ provider: 'synthetic-exhausted-validation', models: [{ id: 'synthetic-exhausted-validation' }] });
  validationCore.setResponses([
    fauxAssistantMessage([fauxToolCall(TARGET_TOOL, {})], { stopReason: 'toolUse' }),
  ]);
  const validationAgent = makeAgent(
    { model: validationCore.getModel(), streamFn: validationCore.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool()],
  );
  await assert.rejects(runAgentCaptured(
    validationAgent,
    '分析合成输入。',
    () => false,
    '请提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 1,
      requireCapture: true,
      captureDiagnostics: { targetToolName: TARGET_TOOL, onAttempt: () => {} },
    },
  ), (error: unknown) => {
    assert.ok(error instanceof AgentCaptureError);
    assert.equal(error.code, 'STRUCTURED_TOOL_VALIDATION_FAILED');
    assert.match(error.message, /summary/);
    return true;
  });
  assert.equal(validationCore.state.callCount, 1);
});

test('重试耗尽时抛出所有 attempts 中最严重的诊断', async () => {
  const core = createFauxCore({ provider: 'synthetic-severe-attempt', models: [{ id: 'synthetic-severe-attempt' }] });
  core.setResponses([
    fauxAssistantMessage('合成输出未完成。', { stopReason: 'length' }),
    fauxAssistantMessage('合成输出正常结束但未提交。'),
  ]);
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [],
  );

  await assert.rejects(runAgentCaptured(
    agent,
    '分析合成输入。',
    () => false,
    '提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 2,
      requireCapture: true,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  ), (error: unknown) => {
    assert.ok(error instanceof AgentCaptureError);
    assert.equal(error.code, 'STRUCTURED_OUTPUT_TRUNCATED');
    assert.equal(error.diagnostics.attempt, 1);
    assert.equal(error.diagnostics.outcome, 'output_truncated');
    return true;
  });

  assert.equal(core.state.callCount, 2);
  assert.deepEqual(reports.map((item) => item.outcome), ['output_truncated', 'tool_not_called']);
});

test('模型调用异常先报告 model_error attempt 再原样抛出', async () => {
  const privateErrorMessage = 'SYNTHETIC_PRIVATE_PROVIDER_FAILURE';
  const core = createFauxCore({
    api: 'openai-completions',
    provider: 'synthetic-provider-error',
    models: [{ id: 'synthetic-provider-error-model' }],
  });
  core.setResponses([fauxAssistantMessage('合成失败响应。', {
    stopReason: 'error',
    errorMessage: privateErrorMessage,
  })]);
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool()],
  );

  await assert.rejects(runAgentCaptured(
    agent,
    '分析合成输入。',
    () => false,
    '提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 3,
      requireCapture: true,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  ), (error: unknown) => {
    assert.equal(error instanceof AgentCaptureError, false);
    assert.match(String((error as Error)?.message ?? error), new RegExp(privateErrorMessage));
    return true;
  });

  assert.equal(core.state.callCount, 1);
  assert.equal(reports.length, 1);
  assert.equal(reports[0].outcome, 'model_error');
  assert.equal(reports[0].stopReason, 'error');
  assert.equal(reports[0].modelApi, 'openai-completions');
  assert.equal(reports[0].errorKind, 'provider_error');
  assert.equal(Object.hasOwn(reports[0], 'errorCode'), false);
  assert.equal(Object.hasOwn(reports[0], 'errorMessage'), false);
  assert.equal(JSON.stringify(reports[0]).includes(privateErrorMessage), false);
});

test('未启用强制捕获时保持原有的非抛错返回语义', async () => {
  const core = createFauxCore({ provider: 'synthetic-legacy-capture', models: [{ id: 'synthetic-legacy-capture' }] });
  core.setResponses([fauxAssistantMessage('合成响应。')]);
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [],
  );

  await assert.doesNotReject(runAgentCaptured(
    agent,
    '分析合成输入。',
    () => false,
    '请提交合成结果。',
    undefined,
    { maxCaptureAttempts: 1 },
  ));
});

test('诊断回调失败不会改变 runAgentCaptured 的业务结果', async () => {
  const core = createFauxCore({ provider: 'synthetic-diagnostic-failure', models: [{ id: 'synthetic-diagnostic-failure' }] });
  core.setResponses([fauxAssistantMessage('合成响应。')]);
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [],
  );

  await assert.doesNotReject(runAgentCaptured(
    agent,
    '分析合成输入。',
    () => false,
    '提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 1,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: () => { throw new Error('synthetic diagnostics failure'); },
      },
    },
  ));
});

test('真实 faux Agent 的 TypeBox 参数错误会形成校验失败诊断', async () => {
  const core = createFauxCore({ provider: 'synthetic-invalid-payload', models: [{ id: 'synthetic-invalid-payload' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall(TARGET_TOOL, {})], { stopReason: 'toolUse' }),
  ]);
  let captured = false;
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const submitTool = {
    name: TARGET_TOOL,
    label: '提交合成分析',
    description: '提交完全合成的分析结果。',
    parameters: Type.Object({ summary: Type.String() }),
    async execute() {
      captured = true;
      return { content: [{ type: 'text' as const, text: '已提交。' }], details: {}, terminate: true };
    },
  };
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [submitTool],
  );

  await runAgentCaptured(
    agent,
    '分析合成输入。',
    () => captured,
    '提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 1,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  );

  assert.equal(captured, false);
  assert.equal(core.state.callCount, 1);
  assert.equal(reports.length, 1);
  assert.equal(reports[0].outcome, 'tool_validation_failed');
  assert.equal(reports[0].stopReason, 'toolUse');
  assert.equal(reports[0].targetToolFailedCount, 1);
  assert.match(reports[0].validationError, /summary/);
  assert.doesNotMatch(reports[0].validationError, /Received arguments/);
});

test('连续无效工具参数时每次外层捕获尝试只发起一次模型请求', async () => {
  const core = createFauxCore({ provider: 'synthetic-request-limit', models: [{ id: 'synthetic-request-limit' }] });
  core.setResponses(Array.from(
    { length: 4 },
    () => fauxAssistantMessage([fauxToolCall(TARGET_TOOL, {})], { stopReason: 'toolUse' }),
  ));
  const reports: AgentCaptureAttemptDiagnostics[] = [];
  const agent = makeAgent(
    { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' },
    '仅处理完全合成的测试输入。',
    [syntheticSubmitTool()],
  );

  await assert.rejects(runAgentCaptured(
    agent,
    '分析合成输入。',
    () => false,
    '提交合成结果。',
    undefined,
    {
      maxCaptureAttempts: 3,
      requireCapture: true,
      captureDiagnostics: {
        targetToolName: TARGET_TOOL,
        onAttempt: (diagnostics) => reports.push(diagnostics),
      },
    },
  ), (error: unknown) => {
    assert.ok(error instanceof AgentCaptureError);
    assert.equal(error.code, 'STRUCTURED_TOOL_VALIDATION_FAILED');
    return true;
  });

  assert.equal(core.state.callCount, 3);
  assert.equal(core.getPendingResponseCount(), 1);
  assert.deepEqual(reports.map((item) => item.outcome), [
    'tool_validation_failed',
    'tool_validation_failed',
    'tool_validation_failed',
  ]);
  assert.deepEqual(reports.map((item) => item.stopReason), ['toolUse', 'toolUse', 'toolUse']);
});
