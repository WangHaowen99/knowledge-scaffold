import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  classifyGenerationError,
  generationModelCallError,
  shouldRetryGenerationError,
} from '../src/workflow/generation-error-policy.js';

test('知识生成遇到额度耗尽时不重复调用模型', () => {
  const policy = classifyGenerationError(new Error('429 insufficient_quota: token-plan quota has been exhausted'));
  assert.equal(policy.kind, 'quota_exhausted');
  assert.equal(policy.retry, false);
  assert.equal(shouldRetryGenerationError(new Error('额度耗尽')), false);
  assert.match(generationModelCallError(new Error('429 insufficient_quota')).message, /模型额度不足/);
  assert.match(generationModelCallError(new Error('429 insufficient_quota')).message, /已保留当前知识生成断点/);
});

test('知识生成遇到限流和暂时性网络错误时仍允许有限重试', () => {
  assert.deepEqual(classifyGenerationError(new Error('429 rate limit; Retry-After: 4')), {
    kind: 'rate_limited', retry: true, retryAfterMs: 4000, message: '429 rate limit; Retry-After: 4',
  });
  assert.equal(classifyGenerationError(new Error('502 upstream timeout')).kind, 'transient_provider');
  assert.equal(shouldRetryGenerationError(new Error('502 upstream timeout')), true);
});
