// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import { clearWorkspaceRuntime } from './workspaceRuntime.js';

test('workspace switch clears streams, message caches, session state and optional solver state', () => {
  const calls = [];
  clearWorkspaceRuntime({
    streamManager: {
      clearAllCaches: () => calls.push('clear-caches'),
    },
    chat: {
      resetSessionsState: () => calls.push('reset-sessions'),
    },
    resetSolver: () => calls.push('reset-solver'),
  });

  assert.deepEqual(calls, ['clear-caches', 'reset-sessions', 'reset-solver']);
});

test('workspace runtime reset keeps the solver callback optional', () => {
  assert.doesNotThrow(() => clearWorkspaceRuntime({
    streamManager: { clearAllCaches() {} },
    chat: { resetSessionsState() {} },
  }));
});
