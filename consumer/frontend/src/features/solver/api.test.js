import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createCandidateSet,
  createValidationRun,
  getSolverEvidence,
  getSolverCandidateResults,
  getSolverHistory,
  getSolverRun,
  requestSolverJson,
} from './api.js';

const response = (payload, { ok = true, status = 200 } = {}) => ({
  ok,
  status,
  json: async () => payload,
});

test('solver API posts the normalized plan to the formal runs alias', async () => {
  const calls = [];
  const request = async (url, options = {}) => {
    calls.push({ url, options });
    return response({ source_run_id: 'solve_synthetic_001', candidate_count: 2 });
  };
  const payload = {
    knowledge_package_id: 'knowledge_package_demo',
    knowledge_package_revision: 'revision_demo_1',
    plan: { option_domain: ['OPTION_0', 'OPTION_1'], max_transitions: 1, items: [] },
  };

  assert.deepEqual(await createCandidateSet(payload, { request }), {
    source_run_id: 'solve_synthetic_001',
    candidate_count: 2,
  });
  assert.equal(calls[0].url, '/api/solver/runs');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].options.body), payload);
});

test('solver validation API sends a caller-owned idempotency key', async () => {
  const calls = [];
  const request = async (url, options = {}) => {
    calls.push({ url, options });
    return response({ validation_run_id: 'validation_run_synthetic_001' }, { status: 201 });
  };
  const payload = { source_run_id: 'solve_synthetic_001' };

  assert.equal(
    (await createValidationRun(payload, 'idem-synthetic-001', { request })).validation_run_id,
    'validation_run_synthetic_001',
  );
  assert.equal(calls[0].url, '/api/solver/validations');
  assert.equal(calls[0].options.headers['Idempotency-Key'], 'idem-synthetic-001');
  assert.deepEqual(JSON.parse(calls[0].options.body), payload);
});

test('solver run query encodes the run identifier', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    return response({ run_id: 'solve/synthetic' });
  };

  await getSolverRun('solve/synthetic', { request });
  assert.equal(calls[0], '/api/solver/runs/solve%2Fsynthetic');
});

test('solver history query clamps its limit and carries the cursor', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    return response({ items: [], next_cursor: null });
  };

  await getSolverHistory({ cursor: 'cursor synthetic', limit: 500 }, { request });

  assert.equal(
    calls[0],
    '/api/solver/history?cursor=cursor+synthetic&limit=100',
  );
});

test('solver evidence query encodes the job identifier', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    return response({ job: { job_id: 'job/synthetic' }, artifacts: [] });
  };

  await getSolverEvidence('job/synthetic', { request });

  assert.equal(calls[0], '/api/solver/history/job%2Fsynthetic/evidence');
});

test('solver candidate results query encodes the job identifier', async () => {
  const calls = [];
  const request = async (url) => {
    calls.push(url);
    return response({ schema_version: 'solver.candidate-results.v1' });
  };

  await getSolverCandidateResults('job/synthetic', { request });

  assert.equal(
    calls[0],
    '/api/solver/history/job%2Fsynthetic/candidate-results',
  );
});

test('solver API errors expose stable status, code and retryability', async () => {
  const request = async () => response({
    code: 'verification_runtime_failed',
    retryable: true,
    error: '验证运行时暂不可用',
  }, { ok: false, status: 503 });

  await assert.rejects(
    () => requestSolverJson(request, '/api/solver/runs', {}, '求解失败'),
    (error) => error.message === '验证运行时暂不可用'
      && error.status === 503
      && error.code === 'verification_runtime_failed'
      && error.retryable === true,
  );
});

test('network failures become retryable normalized solver errors', async () => {
  const request = async () => { throw new TypeError('Failed to fetch'); };

  await assert.rejects(
    () => getSolverRun('solve_synthetic_001', { request }),
    (error) => error.status === 0
      && error.code === 'network_error'
      && error.retryable === true
      && /网络/.test(error.message),
  );
});

test('validation does not issue a request without an idempotency key', async () => {
  let called = false;
  const request = async () => {
    called = true;
    return response({});
  };

  await assert.rejects(
    () => createValidationRun({}, '  ', { request }),
    /幂等键/,
  );
  assert.equal(called, false);
});
