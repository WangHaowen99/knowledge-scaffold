// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findVisibleWorkflowRecoveryIndex,
  getWorkflowErrorPresentation,
  normalizeWorkflowRecovery,
} from './workflowErrorPresentation.js';

test('workflow errors map stable model categories to safe user messages', () => {
  assert.equal(
    getWorkflowErrorPresentation('', { errorCode: 'AUTHENTICATION_FAILED' }).message,
    '模型服务认证失败，请检查模型配置后重试。',
  );
  assert.equal(
    getWorkflowErrorPresentation('', { errorCode: 'MODEL_RATE_LIMITED' }).message,
    '模型服务请求过于频繁，请稍后继续执行。',
  );
  assert.equal(
    getWorkflowErrorPresentation('', { errorCode: 'MODEL_TIMEOUT' }).message,
    '模型服务响应超时，请稍后继续执行。',
  );
  assert.equal(
    getWorkflowErrorPresentation('', { errorCode: 'MODEL_CONNECTION_ERROR' }).message,
    '暂时无法连接模型服务，请稍后继续执行。',
  );
  assert.equal(
    getWorkflowErrorPresentation('', {
      errorCode: 'MODEL_PROVIDER_ERROR',
      retriable: true,
    }).message,
    '模型服务暂时不可用，请稍后继续执行。',
  );
  assert.equal(
    getWorkflowErrorPresentation('', { errorCode: 'MODEL_PROVIDER_ERROR' }).message,
    '模型服务请求失败，请检查模型配置或输入内容。',
  );
  assert.equal(
    getWorkflowErrorPresentation('private provider detail').message,
    '任务执行过程中出现异常，尚未完成。',
  );
});

test('workflow recovery accepts only a bounded continue action', () => {
  assert.deepEqual(normalizeWorkflowRecovery({
    action: 'continue',
    label: '继续执行',
    message: 'Continue from the preserved context.',
  }), {
    action: 'continue',
    label: '继续执行',
    message: 'Continue from the preserved context.',
  });
  assert.equal(normalizeWorkflowRecovery({ action: 'retry', message: 'retry' }), null);
  assert.equal(normalizeWorkflowRecovery({ action: 'continue', message: '' }), null);
});

test('only the latest recoverable failure without a later user reply is visible', () => {
  const recovery = normalizeWorkflowRecovery({
    action: 'continue',
    message: 'Continue from the preserved context.',
  });
  const messages = [
    { role: 'assistant', status: 'error', recovery },
    { role: 'assistant', status: 'error', recovery },
  ];

  assert.equal(findVisibleWorkflowRecoveryIndex(messages), 1);
  assert.equal(
    findVisibleWorkflowRecoveryIndex([...messages, { role: 'user', status: '' }]),
    -1,
  );
  assert.equal(
    findVisibleWorkflowRecoveryIndex([
      ...messages,
      { role: 'user', status: '' },
      { role: 'assistant', status: 'finished', recovery: null },
    ]),
    -1,
  );
});
