// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import test from 'node:test';
import assert from 'node:assert/strict';
import { friendlyRequestError } from './errors.js';

test('browser network errors are converted to a useful Chinese message', () => {
  assert.equal(friendlyRequestError(new TypeError('Failed to fetch'), '知识包加载失败'), '知识包加载失败：网络异常或后端服务不可用');
});

test('server-provided messages are preserved', () => {
  assert.equal(friendlyRequestError(new Error('没有访问权限'), '请求失败'), '没有访问权限');
});
