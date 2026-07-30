import assert from 'node:assert/strict';
import test from 'node:test';

import {
  describeConsumerRequestError,
  resolveConsumerLongRequestTimeoutMs,
} from '../src/consumer/client.js';

test('Consumer 长请求默认允许 45 分钟', () => {
  assert.equal(resolveConsumerLongRequestTimeoutMs(undefined), 45 * 60_000);
});

test('Consumer 长请求超时可通过环境变量配置，并限制在安全范围内', () => {
  assert.equal(resolveConsumerLongRequestTimeoutMs('1800000'), 30 * 60_000);
  assert.equal(resolveConsumerLongRequestTimeoutMs('1000'), 60_000);
  assert.equal(resolveConsumerLongRequestTimeoutMs(String(3 * 60 * 60_000)), 2 * 60 * 60_000);
  assert.equal(resolveConsumerLongRequestTimeoutMs('invalid'), 45 * 60_000);
});

test('Consumer 请求失败时展示底层超时原因', () => {
  const error = new TypeError('fetch failed', {
    cause: Object.assign(new Error('Headers Timeout Error'), { code: 'UND_ERR_HEADERS_TIMEOUT' }),
  });
  assert.equal(
    describeConsumerRequestError(error),
    'fetch failed（UND_ERR_HEADERS_TIMEOUT：Headers Timeout Error）',
  );
});
