import assert from 'node:assert/strict';
import test from 'node:test';

import { describeMaterialAnalysisFailure } from '../src/workflow/material-analysis-failure.js';

test('资料分析结构化失败转换为稳定错误码和可行动提示', () => {
  const cases = [
    {
      code: 'MODEL_REQUEST_TIMEOUT',
      currentStep: '模型请求超时',
      expected: /限定时间.*有限重试.*保留资料分析断点.*重新分析资料/u,
    },
    {
      code: 'MODEL_RATE_LIMITED',
      currentStep: '模型请求受到频率限制',
      expected: /请求过于频繁.*降低并发.*有限重试.*重新分析资料/u,
    },
    {
      code: 'MODEL_SERVICE_UNAVAILABLE',
      currentStep: '模型服务暂时不可用',
      expected: /不可用.*有限重试.*保留资料分析断点.*重新分析资料/u,
    },
    {
      code: 'MODEL_CONTEXT_LENGTH_EXCEEDED',
      currentStep: '模型上下文长度超限',
      expected: /超过.*上下文窗口.*减少本次资料数量.*拆分批次.*配置.*重新分析/u,
    },
    {
      code: 'MODEL_QUOTA_EXHAUSTED',
      currentStep: '模型服务额度不足',
      expected: /额度或余额.*用尽.*暂停.*保留断点.*补充额度.*重新分析/u,
    },
    {
      code: 'STRUCTURED_OUTPUT_TRUNCATED',
      currentStep: '结构化分析输出被截断',
      expected: /结构化输出.*长度上限.*自动重试.*重新分析资料/u,
    },
    {
      code: 'STRUCTURED_TOOL_VALIDATION_FAILED',
      currentStep: '结构化分析参数校验失败',
      expected: /提交参数.*结构校验.*自动.*修正.*重新分析资料/u,
    },
    {
      code: 'STRUCTURED_TOOL_NOT_CALLED',
      currentStep: '结构化分析未提交',
      expected: /未调用资料分析提交工具.*自动重试.*重新分析资料/u,
    },
    {
      code: 'DOMAIN_OUTPUT_TRUNCATED',
      currentStep: '领域汇总输出被截断',
      expected: /领域汇总输出.*长度上限.*自动重试.*保留全部文档分析结果.*重新分析资料/u,
    },
    {
      code: 'DOMAIN_TOOL_VALIDATION_FAILED',
      currentStep: '领域汇总参数校验失败',
      expected: /领域汇总参数.*结构校验.*自动.*修正.*保留全部文档分析结果.*重新分析资料/u,
    },
    {
      code: 'DOMAIN_TOOL_NOT_CALLED',
      currentStep: '领域汇总未提交',
      expected: /未调用领域汇总提交工具.*自动重试.*保留全部文档分析结果.*重新分析资料/u,
    },
    {
      code: 'DOMAIN_INPUT_BUDGET_EXCEEDED',
      currentStep: '领域汇总输入超过安全预算',
      expected: /保留每类代表候选和待确认问题.*减少本次资料数量.*拆分为多个批次.*重新分析/u,
    },
    {
      code: 'MATERIAL_ANALYSIS_PACKAGE_STALE',
      currentStep: '分析期间知识包修订已变化',
      expected: /知识包修订已变化.*本次结果未挂接.*最新修订.*重新分析资料/u,
    },
    {
      code: 'MATERIAL_ANALYSIS_DOCUMENTS_STALE',
      currentStep: '分析期间建模资料已变化',
      expected: /建模资料已变化.*本次结果未挂接.*最新资料.*重新分析/u,
    },
  ];

  for (const fixture of cases) {
    const error = Object.assign(new Error('合成内部诊断详情'), {
      name: 'SyntheticCaptureError',
      code: fixture.code,
    });
    const failure = describeMaterialAnalysisFailure(error);

    assert.equal(failure.errorCode, fixture.code);
    assert.equal(failure.diagnosticMessage, fixture.currentStep);
    assert.equal(failure.currentStep, fixture.currentStep);
    assert.match(failure.userMessage, fixture.expected);
    assert.doesNotMatch(failure.userMessage, /合成内部诊断详情/u);
    if (fixture.code.startsWith('DOMAIN_')) {
      assert.doesNotMatch(failure.currentStep, /文档汇总/u);
      assert.doesNotMatch(failure.userMessage, /文档汇总/u);
    }
  }
});

test('常见 provider 失败信号统一归类到五个终态错误码', () => {
  const cases = [
    { error: new Error('429'), expected: 'MODEL_RATE_LIMITED' },
    { error: new Error('503'), expected: 'MODEL_SERVICE_UNAVAILABLE' },
    { error: new Error('合成模型调用超时'), expected: 'MODEL_REQUEST_TIMEOUT' },
    { error: new Error('429 insufficient_quota'), expected: 'MODEL_QUOTA_EXHAUSTED' },
    { error: new Error('Model provider returned an error'), expected: 'MODEL_SERVICE_UNAVAILABLE' },
    { error: new Error('Stream ended without finish_reason'), expected: 'MODEL_SERVICE_UNAVAILABLE' },
    {
      error: Object.assign(new Error('synthetic context limit'), { code: 'context_length_exceeded' }),
      expected: 'MODEL_CONTEXT_LENGTH_EXCEEDED',
    },
  ];

  for (const fixture of cases) {
    assert.equal(describeMaterialAnalysisFailure(fixture.error).errorCode, fixture.expected);
  }
});

test('未知失败收敛到固定错误码且不持久化自由文本', () => {
  const error = new Error('合成资料修订发生变化');
  error.name = 'MaterialAnalysisStaleError';

  assert.deepEqual(describeMaterialAnalysisFailure(error), {
    errorCode: 'MATERIAL_ANALYSIS_FAILED',
    diagnosticMessage: '资料分析意外中断',
    userMessage: '资料分析未完成。请重新分析资料；如果仍然失败，请联系管理员查看任务诊断。',
    currentStep: '分析未完成',
  });
});

test('普通异常和无效错误标识使用稳定兜底诊断', () => {
  const longMessage = `合成失败：${'x'.repeat(4_000)}`;
  const failure = describeMaterialAnalysisFailure(Object.assign(new Error(longMessage), {
    code: 'invalid code with spaces',
    name: 'invalid name with spaces',
  }));

  assert.equal(failure.errorCode, 'MATERIAL_ANALYSIS_FAILED');
  assert.equal(failure.diagnosticMessage, '资料分析意外中断');
  assert.equal(failure.userMessage, '资料分析未完成。请重新分析资料；如果仍然失败，请联系管理员查看任务诊断。');
  assert.equal(failure.currentStep, '分析未完成');
  assert.deepEqual(describeMaterialAnalysisFailure(null), {
    errorCode: 'MATERIAL_ANALYSIS_FAILED',
    diagnosticMessage: '资料分析意外中断',
    userMessage: '资料分析未完成。请重新分析资料；如果仍然失败，请联系管理员查看任务诊断。',
    currentStep: '分析未完成',
  });
});

test('持久化诊断不保留网络标识、凭据、业务正文或原始工具参数', () => {
  const syntheticValues = [
    'https://service.example.net/synthetic/path',
    'worker.service.example.net',
    'synthetic-node-01',
    'user@example.com',
    '192.0.2.44',
    '2001:db8::44',
    '2001:db8::45',
    'SYNTHETIC_AUTHORIZATION_VALUE',
    'SYNTHETIC_TOKEN_VALUE',
    'SYNTHETIC_KEY_VALUE',
    'SYNTHETIC_SECRET_VALUE',
    'SYNTHETIC_PASSWORD_VALUE',
    'SYNTHETIC_RAW_ARGUMENT_VALUE',
  ];
  const message = [
    '合成诊断信息。',
    `url=${syntheticValues[0]}`,
    `bare_domain=${syntheticValues[1]}`,
    `host=${syntheticValues[2]}`,
    `email=${syntheticValues[3]}`,
    `ipv4=${syntheticValues[4]}`,
    `ipv6=${syntheticValues[5]}`,
    `bracketed_ipv6=[${syntheticValues[6]}]:443`,
    `Authorization: Bearer ${syntheticValues[7]}`,
    `token=${syntheticValues[8]}`,
    `key=${syntheticValues[9]}`,
    `secret=${syntheticValues[10]}`,
    `password=${syntheticValues[11]}`,
    'Received arguments:',
    JSON.stringify({ value: syntheticValues[12] }),
  ].join('\n');

  const failure = describeMaterialAnalysisFailure(Object.assign(new Error(message), {
    name: 'SyntheticDiagnosticError',
  }));

  assert.equal(failure.errorCode, 'MATERIAL_ANALYSIS_FAILED');
  for (const value of syntheticValues) {
    assert.equal(failure.diagnosticMessage.includes(value), false);
  }
  assert.equal(failure.diagnosticMessage, '资料分析意外中断');
  assert.doesNotMatch(failure.userMessage, /SYNTHETIC_/u);
});

test('未知诊断不会持久化 JSON、Bearer、DNS 主机或截断 PEM 正文', () => {
  const jsonToken = 'SYNTHETIC_JSON_TOKEN_VALUE';
  const jsonAuthorization = 'SYNTHETIC_JSON_AUTH_VALUE';
  const bareBearer = 'SYNTHETIC_BARE_BEARER_VALUE';
  const dnsHostA = 'synthetic-host-a';
  const dnsHostB = 'synthetic-host-b';
  const pemBody = `SYNTHETIC_PEM_BODY_${'X'.repeat(10_500)}`;
  const message = [
    JSON.stringify({ token: jsonToken, authorization: jsonAuthorization }),
    `Bearer ${bareBearer}`,
    `getaddrinfo ENOTFOUND ${dnsHostA}`,
    `EAI_AGAIN ${dnsHostB}`,
    '-----BEGIN PRIVATE KEY-----',
    pemBody,
  ].join('\n');

  const diagnostic = describeMaterialAnalysisFailure(new Error(message)).diagnosticMessage;

  assert.equal(diagnostic, '资料分析意外中断');
  for (const value of [jsonToken, jsonAuthorization, bareBearer, dnsHostA, dnsHostB, 'SYNTHETIC_PEM_BODY']) {
    assert.equal(diagnostic.includes(value), false);
  }
});

test('结构化校验失败只持久化字段路径和受控校验原因', () => {
  const validationError = [
    'Validation failed for tool "submit_document_analysis":',
    '  - summary: Expected string',
    '',
    'Received arguments:',
    JSON.stringify({ summary: 'SYNTHETIC_PRIVATE_ARGUMENT' }),
  ].join('\n');
  const error = Object.assign(new Error('SYNTHETIC_PRIVATE_PROVIDER_MESSAGE'), {
    code: 'STRUCTURED_TOOL_VALIDATION_FAILED',
    diagnostics: { validationError },
  });

  const failure = describeMaterialAnalysisFailure(error);
  assert.equal(
    failure.diagnosticMessage,
    '结构化分析参数校验失败：summary: Expected string',
  );
  assert.doesNotMatch(failure.diagnosticMessage, /SYNTHETIC_PRIVATE/u);
  assert.doesNotMatch(failure.diagnosticMessage, /Received arguments/u);
});
