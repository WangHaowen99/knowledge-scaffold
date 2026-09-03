import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildLlmRequestPreviewBody,
  normalizeLlmAdvancedSettings,
} from '../src/settings/llm-extra-params.js';

test('LLM advanced settings build thinking and custom request body preview', () => {
  const normalized = normalizeLlmAdvancedSettings({
    thinkingMode: 'enabled',
    reasoningEffort: 'high',
    extraBodyJson: '{"parallel_tool_calls": false}',
  });

  assert.deepEqual(buildLlmRequestPreviewBody({ model: 'demo-model', ...normalized }), {
    model: 'demo-model',
    messages: [{ role: 'user', content: 'ping' }],
    max_tokens: 1,
    parallel_tool_calls: false,
    thinking: { type: 'enabled' },
    reasoning_effort: 'high',
  });
});

test('LLM custom request params reject secrets and core request overrides', () => {
  assert.throws(() => normalizeLlmAdvancedSettings({
    extraBodyJson: '{"authorization":"Bearer synthetic"}',
  }), /blocked key/);
  assert.throws(() => normalizeLlmAdvancedSettings({
    extraBodyJson: '{"nested":{"messages":[]}}',
  }), /blocked key/);
  assert.throws(() => normalizeLlmAdvancedSettings({
    extraBodyJson: '[]',
  }), /JSON object/);
});
