import assert from 'node:assert/strict';
import test from 'node:test';

import { clearWorkspaceRuntime } from './workspaceRuntime.js';

test('workspace switch clears streams, message caches and session state', () => {
  const calls = [];
  clearWorkspaceRuntime({
    streamManager: {
      stopAllStreams: () => calls.push('stop-streams'),
      clearAllCaches: () => calls.push('clear-caches'),
    },
    chat: {
      resetSessionsState: () => calls.push('reset-sessions'),
    },
  });

  assert.deepEqual(calls, ['stop-streams', 'clear-caches', 'reset-sessions']);
});
