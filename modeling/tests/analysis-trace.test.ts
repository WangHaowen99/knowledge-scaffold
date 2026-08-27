import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'analysis-trace-'));

const { createProject } = await import('../src/projects/repo.js');
const { upsertNode } = await import('../src/workflow/engine.js');
const {
  appendTraceEvent,
  listTraceEvents,
  sanitizeTraceDetails,
  traceEventFromAgentEvent,
} = await import('../src/workflow/trace.js');

test('分析轨迹按顺序持久化并过滤敏感内容', () => {
  const project = createProject({ owner: 'operator_a', name: 'trace', domain: 'demo', background: '' });
  const node = upsertNode({ projectId: project.id, type: 'analyze', status: 'running', ordinal: 0 });

  appendTraceEvent({
    projectId: project.id,
    nodeId: node.id,
    phase: 'prepare',
    kind: 'phase_started',
    status: 'running',
    title: '准备材料',
  });
  appendTraceEvent({
    projectId: project.id,
    nodeId: node.id,
    phase: 'artifact_batch',
    kind: 'tool_started',
    status: 'running',
    title: '读取文档',
    details: { filename: 'manual.docx', apiKey: 'secret', content: 'x'.repeat(9000) },
  });

  const events = listTraceEvents(node.id);
  assert.deepEqual(events.map((event) => event.seq), [1, 2]);
  assert.equal(events[1].details?.apiKey, '[已隐藏]');
  assert.equal('content' in (events[1].details ?? {}), false);
  assert.deepEqual(sanitizeTraceDetails({ reasoning: 'private', query: '装置负荷' }), { query: '装置负荷' });
});

test('捕获诊断 token 计数经过事件转换和持久化后仍为数值', () => {
  const project = createProject({ owner: 'operator_a', name: 'capture_trace', domain: 'demo', background: '' });
  const node = upsertNode({ projectId: project.id, type: 'analyze', status: 'running', ordinal: 0 });
  const draft = traceEventFromAgentEvent({
    type: 'capture_attempt_end',
    diagnostics: {
      attempt: 1,
      outcome: 'output_truncated',
      targetToolName: 'submit_synthetic_analysis',
      stopReason: 'length',
      inputTokens: 321,
      outputTokens: 123,
    },
  });
  assert.ok(draft);

  appendTraceEvent({
    projectId: project.id,
    nodeId: node.id,
    ...draft,
    details: {
      ...(draft.details ?? {}),
      apiToken: 'SYNTHETIC_TRACE_TOKEN_VALUE',
      nested: { secret: 'SYNTHETIC_TRACE_SECRET_VALUE' },
    },
  });

  const [stored] = listTraceEvents(node.id);
  assert.equal(stored.details?.inputTokens, 321);
  assert.equal(stored.details?.outputTokens, 123);
  assert.equal(stored.details?.apiToken, '[已隐藏]');
  assert.deepEqual(stored.details?.nested, { secret: '[已隐藏]' });
});

test('工具选择兼容降级只记录 warning 且不持久化 provider 原始错误', () => {
  const event = traceEventFromAgentEvent({
    type: 'tool_choice_fallback',
    targetToolName: 'submit_synthetic_analysis',
    fromMode: 'named',
    toMode: 'required',
    modelApi: 'openai-completions',
    providerError: 'SYNTHETIC_PRIVATE_PROVIDER_ERROR',
  });

  assert.deepEqual(event, {
    phase: 'agent',
    kind: 'validation_warning',
    status: 'warning',
    title: '模型工具调用模式已自动切换',
    summary: '兼容性切换已在当前任务内完成，资料分析继续运行',
    details: {
      fromMode: 'named',
      toMode: 'required',
      modelApi: 'openai-completions',
      toolName: 'submit_synthetic_analysis',
    },
  });
  assert.doesNotMatch(JSON.stringify(event), /SYNTHETIC_PRIVATE_PROVIDER_ERROR/u);
});

test('Agent 轨迹记录工具调用但丢弃私有 thinking 块', () => {
  assert.deepEqual(traceEventFromAgentEvent({
    type: 'tool_execution_start',
    toolName: 'read_document',
    args: { filename: 'manual.docx', offset: 6000 },
  }), {
    phase: 'agent',
    kind: 'tool_started',
    status: 'running',
    title: '读取文档',
    summary: 'manual.docx · offset 6000',
    details: { toolName: 'read_document', filename: 'manual.docx', offset: 6000 },
  });

  assert.equal(traceEventFromAgentEvent({
    type: 'turn_end',
    message: { content: [{ type: 'thinking', thinking: 'private' }] },
  }), null);

  const syntheticPrivateText = 'SYNTHETIC_MODEL_TEXT_VALUE';
  const syntheticBusinessText = '合成业务正文不应进入轨迹';
  const summary = traceEventFromAgentEvent({
    type: 'turn_end',
    message: {
      stopReason: 'stop',
      content: [
        { type: 'text', text: syntheticPrivateText },
        { type: 'text', text: syntheticBusinessText },
      ],
    },
  });
  assert.deepEqual(summary, {
    phase: 'agent',
    kind: 'model_summary',
    status: 'done',
    title: '模型公开说明',
    summary: '模型本轮返回了文本说明',
    details: {
      textBlockCount: 2,
      textCharCount: syntheticPrivateText.length + syntheticBusinessText.length,
    },
  });
  assert.equal(JSON.stringify(summary).includes(syntheticPrivateText), false);
  assert.equal(JSON.stringify(summary).includes(syntheticBusinessText), false);
});

test('Agent 工具结束轨迹区分成功和失败', () => {
  assert.deepEqual(traceEventFromAgentEvent({
    type: 'tool_execution_end',
    toolName: 'submit_analysis',
    isError: false,
    result: { content: [{ type: 'text', text: 'synthetic success' }] },
  }), {
    phase: 'agent',
    kind: 'tool_completed',
    status: 'done',
    title: '提交领域分析完成',
    details: { toolName: 'submit_analysis', isError: false },
  });

  const failed = traceEventFromAgentEvent({
    type: 'tool_execution_end',
    toolName: 'submit_analysis',
    isError: true,
    result: { content: [{
      type: 'text',
      text: [
        'Validation failed for tool "submit_analysis":',
        '  - summary: Expected string',
        '',
        'Received arguments:',
        '{"marker":"SYNTHETIC_TRACE_RAW_ARGUMENT"}',
      ].join('\n'),
    }] },
  });
  assert.equal(failed?.kind, 'tool_completed');
  assert.equal(failed?.status, 'error');
  assert.equal(failed?.title, '提交领域分析失败');
  assert.equal(failed?.details?.isError, true);
  assert.match(String(failed?.details?.validationError), /summary: Expected string/);
  assert.doesNotMatch(String(failed?.details?.validationError), /SYNTHETIC_TRACE_RAW_ARGUMENT|Received arguments/);
});

test('无公开文本的 length 结束会记录 token 元数据警告', () => {
  assert.deepEqual(traceEventFromAgentEvent({
    type: 'turn_end',
    message: {
      stopReason: 'length',
      usage: { input: 320, output: 128 },
      content: [{ type: 'thinking', thinking: 'synthetic private reasoning' }],
    },
  }), {
    phase: 'agent',
    kind: 'validation_warning',
    status: 'warning',
    title: '模型输出达到长度上限',
    details: { stopReason: 'length', inputTokens: 320, outputTokens: 128 },
  });
});

test('结构化捕获尝试轨迹只保留白名单诊断字段', () => {
  assert.deepEqual(traceEventFromAgentEvent({
    type: 'capture_attempt_end',
    diagnostics: {
      attempt: 2,
      outcome: 'tool_validation_failed',
      targetToolName: 'submit_document_analysis',
      stopReason: 'stop',
      targetToolStartedCount: 1,
      targetToolSucceededCount: 0,
      targetToolFailedCount: 1,
      inputTokens: 640,
      outputTokens: 96,
      validationError: [
        'Validation failed for tool "submit_document_analysis":',
        '  - summary: Expected string',
        '',
        'Received arguments:',
        '{"marker":"SYNTHETIC_CAPTURE_RAW_ARGUMENT"}',
      ].join('\n'),
      rawArguments: { marker: 'SYNTHETIC_CAPTURE_RAW_ARGUMENT' },
    },
  }), {
    phase: 'agent',
    kind: 'capture_attempt',
    status: 'warning',
    title: '结构化提交参数校验失败',
    summary: '第 2 次结构化提交尝试',
    details: {
      attempt: 2,
      outcome: 'tool_validation_failed',
      toolName: 'submit_document_analysis',
      stopReason: 'stop',
      targetToolStartedCount: 1,
      targetToolSucceededCount: 0,
      targetToolFailedCount: 1,
      inputTokens: 640,
      outputTokens: 96,
      validationError: [
        'Validation failed for tool "submit_document_analysis":',
        '  - summary: Expected string',
      ].join('\n'),
    },
  });

  const captured = traceEventFromAgentEvent({
    type: 'capture_attempt_end',
    diagnostics: { attempt: 3, outcome: 'captured', targetToolName: 'submit_document_analysis' },
  });
  assert.equal(captured?.status, 'done');
  assert.equal(captured?.kind, 'capture_attempt');
});

test('模型调用失败 attempt 轨迹只保留稳定分类和 API 类型', () => {
  const event = traceEventFromAgentEvent({
    type: 'capture_attempt_end',
    diagnostics: {
      attempt: 1,
      outcome: 'model_error',
      targetToolName: 'submit_synthetic_analysis',
      stopReason: 'error',
      modelApi: 'openai-completions',
      errorKind: 'provider_error',
      errorCode: 'SYNTHETIC_PROVIDER_UNAVAILABLE',
      errorMessage: 'SYNTHETIC_PRIVATE_PROVIDER_MESSAGE',
      baseUrl: 'https://service.example.net/synthetic/internal',
    },
  });

  assert.deepEqual(event, {
    phase: 'agent',
    kind: 'capture_attempt',
    status: 'error',
    title: '模型调用失败',
    summary: '第 1 次结构化提交尝试',
    details: {
      attempt: 1,
      outcome: 'model_error',
      toolName: 'submit_synthetic_analysis',
      stopReason: 'error',
      targetToolStartedCount: 0,
      targetToolSucceededCount: 0,
      targetToolFailedCount: 0,
      inputTokens: 0,
      outputTokens: 0,
      modelApi: 'openai-completions',
      errorKind: 'provider_error',
    },
  });
  assert.doesNotMatch(
    JSON.stringify(event),
    /SYNTHETIC_PRIVATE_PROVIDER_MESSAGE|"modelId"|"provider"|"baseUrl"|service\.example\.net/u,
  );

  assert.ok(event);
  const project = createProject({ owner: 'operator_a', name: 'model_error_trace', domain: 'demo', background: '' });
  const node = upsertNode({ projectId: project.id, type: 'analyze', status: 'running', ordinal: 0 });
  appendTraceEvent({ projectId: project.id, nodeId: node.id, ...event });
  const [stored] = listTraceEvents(node.id);
  assert.equal(stored.details?.modelApi, 'openai-completions');
  assert.equal(stored.details?.errorKind, 'provider_error');
  assert.equal(Object.hasOwn(stored.details ?? {}, 'errorCode'), false);

  const freeForm = traceEventFromAgentEvent({
    type: 'capture_attempt_end',
    diagnostics: {
      attempt: 1,
      outcome: 'model_error',
      targetToolName: 'submit_synthetic_analysis',
      errorKind: 'SyntheticProviderError',
      errorCode: 'SYNTHETIC_PROVIDER_UNAVAILABLE',
    },
  });
  assert.equal(Object.hasOwn(freeForm?.details ?? {}, 'errorKind'), false);
  assert.equal(Object.hasOwn(freeForm?.details ?? {}, 'errorCode'), false);
});
