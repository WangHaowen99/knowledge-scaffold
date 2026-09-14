// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import { isMessageUpdateScopeCurrent } from './streamUpdateScope.js';

test('queued message updates cannot cross a workspace runtime epoch', () => {
  assert.equal(isMessageUpdateScopeCurrent(4, 4), true);
  assert.equal(isMessageUpdateScopeCurrent(3, 4), false);
});
