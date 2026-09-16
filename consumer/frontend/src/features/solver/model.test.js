// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildValidationPayload,
  createValidationIdempotencyKey,
  createRequestEpochGuard,
  createValidationAttemptKeyManager,
  createDefaultPlan,
  isCompleteCandidateArtifact,
  normalizePlan,
  readSolverRunIds,
  solverStorageKey,
  writeSolverRunIds,
} from './model.js';

const validPlan = () => ({
  knowledge_package_id: ' knowledge_package_demo ',
  knowledge_package_revision: ' revision_demo_1 ',
  option_domain: ['OPTION_0', 'OPTION_1'],
  max_transitions: '1',
  items: [{
    item_order: '1',
    item_id: ' ITEM_SYNTHETIC_1 ',
    quantity: '1001.50',
    duration: '2.5',
  }],
});

const createStorage = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
};

test('default solver plan has the fixed option domain and one empty item row', () => {
  const plan = createDefaultPlan();

  assert.deepEqual(plan.option_domain, ['OPTION_0', 'OPTION_1']);
  assert.equal(plan.items.length, 1);
  assert.equal(plan.items[0].item_id, '');
  assert.equal(plan.items[0].item_order, 1);
});

test('plan normalization returns only the public candidate request payload', () => {
  assert.deepEqual(normalizePlan(validPlan()), {
    knowledge_package_id: 'knowledge_package_demo',
    knowledge_package_revision: 'revision_demo_1',
    plan: {
      option_domain: ['OPTION_0', 'OPTION_1'],
      max_transitions: 1,
      items: [{
        item_order: 1,
        item_id: 'ITEM_SYNTHETIC_1',
        quantity: '1001.50',
        duration: '2.5',
      }],
    },
  });
});

test('plan normalization rejects invalid package, option, transition and item fields', () => {
  const invalidCases = [
    [{ ...validPlan(), knowledge_package_id: ' ' }, /知识包 ID/],
    [{ ...validPlan(), knowledge_package_revision: '' }, /知识包版本/],
    [{ ...validPlan(), option_domain: ['OPTION_0', 'OPTION_0'] }, /候选选项/],
    [{ ...validPlan(), max_transitions: '0' }, /最大切换次数/],
    [{ ...validPlan(), max_transitions: '2' }, /最大切换次数/],
    [{ ...validPlan(), items: [{ ...validPlan().items[0], item_order: '1.5' }] }, /顺序/],
    [{ ...validPlan(), items: [{ ...validPlan().items[0], item_id: '' }] }, /条目 ID/],
    [{ ...validPlan(), items: [{ ...validPlan().items[0], quantity: '0' }] }, /数量/],
    [{ ...validPlan(), items: [{ ...validPlan().items[0], duration: '-1' }] }, /时长/],
  ];

  for (const [plan, message] of invalidCases) {
    assert.throws(() => normalizePlan(plan), message);
  }
});

test('solver run storage contains only workspace-scoped run identifiers', () => {
  const storage = createStorage();

  assert.equal(solverStorageKey('workspace-a'), 'mw_solver_workspace-a');
  assert.notEqual(solverStorageKey('workspace-a'), solverStorageKey('workspace-b'));
  assert.equal(writeSolverRunIds(storage, 'workspace-a', {
    solveRunId: 'solve_synthetic_001',
    validationRunId: 'validation_run_synthetic_001',
    ignored: { candidate: 'must-not-persist' },
  }), true);
  assert.deepEqual(readSolverRunIds(storage, 'workspace-a'), {
    solveRunId: 'solve_synthetic_001',
    validationRunId: 'validation_run_synthetic_001',
  });
  assert.deepEqual(readSolverRunIds(storage, 'workspace-b'), {
    solveRunId: '',
    validationRunId: '',
  });
});

test('validation payload is available only for a complete candidate artifact', () => {
  const artifact = {
    source_run_id: 'solve_synthetic_001',
    candidate_set_ref: 'candidate_set_synthetic_001',
    candidate_set_digest: 'sha256:solver-coordination.v1:synthetic',
    candidate_count: 2,
    candidates: [
      { candidate_id: 'ALL_0', candidate_digest: 'digest-0' },
      { candidate_id: 'ALL_1', candidate_digest: 'digest-1' },
    ],
  };

  assert.equal(isCompleteCandidateArtifact(artifact), true);
  assert.deepEqual(buildValidationPayload(artifact), {
    source_run_id: 'solve_synthetic_001',
    parent_solve_run_id: 'solve_synthetic_001',
    candidate_set_ref: 'candidate_set_synthetic_001',
    candidate_set_digest: 'sha256:solver-coordination.v1:synthetic',
    validator_revision: 'ontology_operation_batch.v1',
  });
  assert.equal(isCompleteCandidateArtifact({ ...artifact, candidates: artifact.candidates.slice(0, 1) }), false);
  assert.throws(
    () => buildValidationPayload({ ...artifact, candidate_set_ref: '' }),
    /候选集制品不完整/,
  );
});

test('each validation action can create a fresh caller-owned idempotency key', () => {
  let sequence = 0;
  const randomId = () => `synthetic-${++sequence}`;

  assert.equal(createValidationIdempotencyKey(randomId), 'solver-validation-synthetic-1');
  assert.equal(createValidationIdempotencyKey(randomId), 'solver-validation-synthetic-2');
});

test('a newer request epoch rejects a slower restore response', async () => {
  let releaseRestore;
  const restoreResponse = new Promise((resolve) => {
    releaseRestore = resolve;
  });
  const guard = createRequestEpochGuard();
  const restoreEpoch = guard.capture();
  const accepted = restoreResponse.then(() => guard.accepts(restoreEpoch));

  guard.invalidate();
  releaseRestore();

  assert.equal(await accepted, false);
  assert.equal(guard.accepts(guard.capture()), true);
});

test('validation retries reuse one key until the attempt completes or resets', () => {
  let sequence = 0;
  const manager = createValidationAttemptKeyManager(
    () => `solver-validation-synthetic-${++sequence}`,
  );

  assert.equal(manager.begin(), 'solver-validation-synthetic-1');
  assert.equal(manager.retry(), 'solver-validation-synthetic-1');
  manager.complete();
  assert.equal(manager.begin(), 'solver-validation-synthetic-2');
  manager.reset();
  assert.equal(manager.retry(), 'solver-validation-synthetic-3');
});
