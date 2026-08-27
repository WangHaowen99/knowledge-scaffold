import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AGENTIC_RUNS_ENDPOINT,
  buildRunRequestHeaders,
  buildRunRequestPayload,
  normalizeRunAdmission,
} from './runAdmission.js';

const admissionPayload = {
  run_id: 'run-server-1',
  session_id: 'session-demo',
  turn_id: 7,
  status: 'queued',
  created: true,
  links: {
    self: '/agentic/runs/run-server-1',
    events: '/agentic/runs/run-server-1/events',
    cancel: '/agentic/runs/run-server-1/cancel',
  },
};

test('normalizes the canonical server-assigned run admission response', () => {
  assert.equal(AGENTIC_RUNS_ENDPOINT, '/agentic/runs');
  assert.deepEqual(normalizeRunAdmission(admissionPayload, 'session-demo'), {
    runId: 'run-server-1',
    sessionId: 'session-demo',
    turnId: 7,
    status: 'queued',
    created: true,
    links: admissionPayload.links,
  });
});

test('accepts an idempotent admission hit and rejects incomplete responses', () => {
  assert.equal(
    normalizeRunAdmission({ ...admissionPayload, created: false }, 'session-demo').created,
    false,
  );
  assert.throws(
    () => normalizeRunAdmission({ ...admissionPayload, run_id: '' }, 'session-demo'),
    /run_id/,
  );
  assert.throws(
    () => normalizeRunAdmission({ ...admissionPayload, session_id: 'other-session' }, 'session-demo'),
    /session_id/,
  );
  assert.throws(
    () => normalizeRunAdmission({ ...admissionPayload, links: {} }, 'session-demo'),
    /links/,
  );
});

test('builds canonical JSON and idempotency headers', () => {
  assert.deepEqual(buildRunRequestHeaders('request-demo'), {
    'Content-Type': 'application/json',
    'Idempotency-Key': 'request-demo',
  });
  assert.throws(() => buildRunRequestHeaders(''), /Idempotency-Key/);
});

test('builds ordinary and HITL requests without a client run id', () => {
  const ordinary = buildRunRequestPayload('  analyze this  ', ' session-demo ', {
    agentMode: 'thinking',
    modelProfileId: 'model-demo',
    arguments: { selected_ontology_id: 'ontology-demo' },
    includeTrace: true,
  });
  assert.deepEqual(ordinary, {
    request: 'analyze this',
    session_id: 'session-demo',
    agent_mode: 'thinking',
    model_profile_id: 'model-demo',
    arguments: { selected_ontology_id: 'ontology-demo' },
    include_trace: true,
  });
  assert.equal(Object.hasOwn(ordinary, 'run_id'), false);

  const interactionResponse = {
    interaction_id: 'interaction-demo',
    type: 'single_choice',
    option_id: 'option-a',
    value: 'A',
  };
  const answer = buildRunRequestPayload('choose A', 'session-demo', { interactionResponse });
  assert.deepEqual(answer.interaction_response, interactionResponse);
  assert.equal(Object.hasOwn(answer, 'run_id'), false);
});
