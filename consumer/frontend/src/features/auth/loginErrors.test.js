// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import { getLoginErrorMessage } from './loginErrors.js';

test('maps authentication failures to safe Chinese messages without exposing account state', () => {
  assert.equal(getLoginErrorMessage({ status: 401, error: 'Invalid credentials' }), '账号或密码错误，请重新输入。');
  assert.equal(getLoginErrorMessage({ status: 429, error: 'too many attempts' }), '登录尝试过于频繁，请稍后再试。');
  assert.equal(getLoginErrorMessage({ status: 503, error: 'service unavailable' }), '认证服务暂不可用，请稍后重试。');
});

test('maps timeout and network failures without exposing raw exception text', () => {
  assert.equal(getLoginErrorMessage({ error: '登录请求超时', name: 'AuthRequestTimeoutError' }), '登录请求超时，请稍后重试。');
  assert.equal(getLoginErrorMessage({ error: 'Failed to fetch', name: 'TypeError' }), '无法连接认证服务，请检查网络后重试。');
  assert.equal(getLoginErrorMessage({ status: 500, error: 'traceback: db password' }), '登录失败，请稍后重试；如持续出现，请联系管理员。');
});

test('falls back to a stable generic message for unknown failures', () => {
  assert.equal(getLoginErrorMessage({ status: 418, error: 'unexpected' }), '登录失败，请稍后重试。');
  assert.equal(getLoginErrorMessage({}), '登录失败，请稍后重试。');
});
