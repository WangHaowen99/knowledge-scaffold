import assert from 'node:assert/strict';
import test from 'node:test';

import type { AuthoringInteraction } from '../src/authoring/types.js';
import { resolveGenerationContinuation } from '../src/workflow/generation-gate.js';

function interaction(overrides: Partial<AuthoringInteraction> = {}): AuthoringInteraction {
  return {
    id: 'ai_gate',
    projectId: 'pj_gate',
    runId: 'ar_generation',
    type: 'decision',
    status: 'answered',
    payload: {
      purpose: 'generation_gate_skip',
      phase: 'operation_batch_2',
    },
    responseId: 'response_gate',
    response: { value: 'skip' },
    createdAt: '2026-07-30T08:00:00.000Z',
    respondedAt: '2026-07-30T08:01:00.000Z',
    ...overrides,
  };
}

test('生成门禁只从已回答待办恢复原任务并授权对应失败批次', () => {
  assert.deepEqual(resolveGenerationContinuation(interaction()), {
    resumeRunId: 'ar_generation',
    skipFailedPhases: ['operation_batch_2'],
  });
  assert.deepEqual(resolveGenerationContinuation(interaction({ response: { value: 'retry' } })), {
    resumeRunId: 'ar_generation',
    skipFailedPhases: [],
  });
});

test('生成门禁拒绝未回答、越界 purpose 和伪造阶段', () => {
  assert.throws(() => resolveGenerationContinuation(null), /待办不存在/);
  assert.throws(() => resolveGenerationContinuation(interaction({ status: 'pending', response: undefined })), /先回答/);
  assert.throws(() => resolveGenerationContinuation(interaction({
    payload: { purpose: 'release_gate', phase: 'operation_batch_1' },
  })), /不属于知识生成门禁/);
  assert.throws(() => resolveGenerationContinuation(interaction({
    payload: { purpose: 'generation_gate_skip', phase: 'validation' },
  })), /阶段不正确/);
  assert.throws(() => resolveGenerationContinuation(interaction({ response: { value: 'approve' } })), /retry 或 skip/);
});
