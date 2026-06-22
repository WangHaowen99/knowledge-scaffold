import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getLocalObservabilityRuntime,
  getSessionPerformanceEntryState,
} from './observabilityStatus.js';

test('local observability runtime reports active local writes only when enabled and mode is local', () => {
  assert.equal(
    getLocalObservabilityRuntime({ observability: { enabled: true, mode: 'local' } }).isWritingLocal,
    true,
  );
  assert.equal(
    getLocalObservabilityRuntime({ observability: { enabled: true, mode: 'langfuse' } }).isWritingLocal,
    false,
  );
  assert.equal(
    getLocalObservabilityRuntime({ observability: { enabled: false, mode: 'local' } }).isWritingLocal,
    false,
  );
  assert.equal(
    getLocalObservabilityRuntime({ observability: { mode: 'local' } }).isWritingLocal,
    false,
  );
});

test('session performance entry stays usable when historical data exists', () => {
  const state = getSessionPerformanceEntryState({
    hasLocalData: true,
    isWritingLocal: false,
    configLoaded: true,
    loading: false,
  });

  assert.equal(state.disabled, false);
  assert.equal(state.tone, 'ready');
});

test('session performance entry is disabled only when there is no data and local writes are inactive', () => {
  const state = getSessionPerformanceEntryState({
    hasLocalData: false,
    isWritingLocal: false,
    configLoaded: true,
    loading: false,
  });

  assert.equal(state.disabled, true);
  assert.equal(state.tone, 'disabled');
});
