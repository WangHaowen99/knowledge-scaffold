import test from 'node:test';
import assert from 'node:assert/strict';

import { getWorkspaceAgentMode, normalizeAgentMode } from './workspacePreferences.js';

test('legacy standard mode is normalized to thinking', () => {
  assert.equal(normalizeAgentMode('standard'), 'thinking');
});

test('unknown agent mode falls back to quick', () => {
  assert.equal(normalizeAgentMode('unexpected'), 'quick');
});

test('agent mode is read from the selected workspace key', () => {
  const values = new Map([
    ['mw_ws_agentmode_workspace-a', 'multi_agent'],
    ['mw_ws_agentmode_workspace-b', 'thinking'],
  ]);
  const storage = { getItem: (key) => values.get(key) || null };

  assert.equal(getWorkspaceAgentMode(storage, 'workspace-a'), 'multi_agent');
  assert.equal(getWorkspaceAgentMode(storage, 'workspace-b'), 'thinking');
});
