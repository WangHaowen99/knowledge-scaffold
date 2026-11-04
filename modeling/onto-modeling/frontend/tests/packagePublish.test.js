import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  hardBlockers,
  isDirectoryIndex,
  missingPublishStages,
  needsPublishCheck,
  needsSolverAcknowledgement,
  reopensPublishCheck,
} from '../src/lib/packagePublish.js';

const stages = ['research', 'flowchart', 'object', 'operation', 'rulemetric', 'validate', 'mock', 'trace', 'eval'].map((key) => ({ key }));
test('publication requires modeling through validation, not later validation exercises', () => {
  assert.deepEqual(missingPublishStages(stages, ['finish', 'wait', 'finish', 'finish', 'wait', 'finish', 'wait', 'wait', 'wait']), []);
});
test('unfinished required stages identify the reason publication is blocked', () => {
  assert.deepEqual(missingPublishStages(stages, ['finish', 'wait', 'finish', 'wait', 'wait', 'process']).map((s) => s.key), ['operation', 'validate']);
});
test('directory index recognition is case insensitive', () => {
  assert.equal(isDirectoryIndex('reports/index.md'), true);
  assert.equal(isDirectoryIndex('reports/Index.md'), true);
  assert.equal(isDirectoryIndex('reports/INDEX.MD'), true);
  assert.equal(isDirectoryIndex('reports/validation.md'), false);
});

test('structure findings block publication like solver findings and cannot be degraded', () => {
  const clean = { issues: [], solver: { enabled: false }, structure: { blocking: 0, findings: [] } };
  assert.equal(needsPublishCheck(clean), false);
  assert.equal(hardBlockers({ ...clean, structure: { blocking: 2 } }), 2);
  assert.equal(needsPublishCheck({ ...clean, structure: { blocking: 1 } }), true);
  assert.equal(hardBlockers({ issues: [], solver: { blocking: 1 }, structure: { blocking: 1 } }), 2);
  assert.equal(hardBlockers({ issues: [] }), 0);
});

test('a structure rejection at submit time reopens the publish check', () => {
  assert.equal(reopensPublishCheck({ code: 'package_structure_invalid' }), true);
  assert.equal(reopensPublishCheck({ code: 'binding_problems' }), true);
  assert.equal(reopensPublishCheck({ code: 'solver_validation_required' }), true);
  assert.equal(reopensPublishCheck({ code: 'hub_unavailable' }), false);
  assert.equal(reopensPublishCheck(new Error('network')), false);
});

test('solver packages always open the publish check for the unverified acknowledgement', () => {
  const clean = { issues: [], solver: { enabled: true, blocking: 0, requires_validation_after_publish: true }, structure: { blocking: 0 } };
  assert.equal(needsSolverAcknowledgement(clean), true);
  assert.equal(needsPublishCheck(clean), true);
  assert.equal(needsSolverAcknowledgement({ issues: [], solver: { enabled: false } }), false);
  assert.equal(reopensPublishCheck({ code: 'solver_validation_acknowledgement_required' }), true);
});
