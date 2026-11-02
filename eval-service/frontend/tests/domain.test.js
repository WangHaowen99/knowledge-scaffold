import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseRoute,
  parseCases,
  caseState,
  canResume,
  emptySource,
  sourceFromRecord,
  sourcePayload,
  packageIds,
  runStartTime,
  judgeMissingReason,
  modelingProjectQueries,
  modelingProjectLabel,
  DEFAULT_CONCURRENCY,
  modelOptionLabel,
  packageVersions,
} from '../src/domain.js';

// Package identifiers and project names are synthetic.
test('modeling project lookup asks once per package in bounded batches', () => {
  const runs = [
    { package_ref: { ontology_id: 'package_b' } },
    { package_ref: { ontology_id: 'package_a' } },
    { package_ref: { ontology_id: 'package_b' } },
    { package_ref: {} },
  ];
  assert.deepEqual(packageIds(runs), ['package_a', 'package_b']);
  const ids = Array.from({ length: 21 }, (_, i) => `package_${i}`);
  const queries = modelingProjectQueries(ids);
  assert.equal(queries.length, 2);
  assert.equal(queries[0].split('&').length, 20);
  assert.equal(queries[1], 'ontology_id=package_20');
});

test('modeling project label shows one name and the total count', () => {
  assert.equal(modelingProjectLabel(undefined), '—');
  assert.equal(modelingProjectLabel({ projects: [], total: 0 }), '—');
  assert.equal(modelingProjectLabel({ projects: ['project_alpha'], total: 1 }), 'project_alpha');
  assert.equal(modelingProjectLabel({ projects: ['project_alpha', 'project_beta'], total: 7 }), 'project_alpha 等 7 个');
});

test('task URLs restore the original task independently of browser state', () => {
  assert.deepEqual(parseRoute('#/runs/run_demo'), { page: 'run', id: 'run_demo' });
  assert.deepEqual(parseRoute('#/new?from=run_demo'), { page: 'new', from: 'run_demo' });
  assert.equal(parseRoute('#/runs/../settings').page, 'tasks');
});

test('definition editor URLs restore create and edit pages without a list selection', () => {
  assert.deepEqual(parseRoute('#/eval-sets'), { page: 'sets' });
  assert.deepEqual(parseRoute('#/eval-sets/new'), { page: 'sets', id: 'new' });
  assert.deepEqual(parseRoute('#/eval-sets/set_demo'), { page: 'sets', id: 'set_demo' });
  assert.deepEqual(parseRoute('#/data-sources'), { page: 'sources' });
  assert.deepEqual(parseRoute('#/data-sources/new'), { page: 'sources', id: 'new' });
  assert.deepEqual(parseRoute('#/data-sources/source_demo'), { page: 'sources', id: 'source_demo' });
  assert.equal(parseRoute('#/eval-sets/../settings').page, 'tasks');
});
test('case imports validate questions and preserve optional references', () => {
  assert.deepEqual(parseCases('[{"question":"Synthetic question","groundTruth":"2"}]'), [
    { question: 'Synthetic question', reference: '2' },
  ]);
  assert.throws(() => parseCases('[{"question":" "}]'), /用例 1/);
  assert.throws(() => parseCases('{}'), /1-500/);
  assert.throws(() => parseCases('[{"question":"Synthetic","reference":5}]'), /格式/);
});
test('human override wins and interrupted configuration changes cannot resume', () => {
  assert.equal(
    caseState({ status: 'settled', judgement: { verdict: 'fail' }, override: { verdict: 'pass' } }),
    'pass',
  );
  assert.equal(caseState({ status: 'manual' }), 'manual');
  assert.equal(canResume({ status: 'interrupted', error: { code: 'configuration_changed' } }), false);
  assert.equal(canResume({ status: 'waiting_auth' }), true);
  assert.equal(canResume({ status: 'interrupted', error: { code: 'graph_scope_changed' } }), false);
  assert.equal(canResume({ status: 'interrupted', error: { code: 'graph_check_required' } }), false);
  assert.equal(canResume({ status: 'interrupted', error: { code: 'graph_import_unavailable' } }), true);
});
test('source forms only submit the selected provider and never reconstruct saved secrets', () => {
  const form = sourceFromRecord({
    id: 'source_demo',
    revision: 2,
    mode: 'connection',
    provider: 'nl2sql_http',
    name: 'data_source_demo',
    secret_configured: true,
    inspection_id: 'inspection_demo',
    connection_view: {
      url: 'https://service.example.net',
      scene: 'workspace_demo',
      evaluation_workspace_id: 'workspace_demo',
    },
  });
  const value = sourcePayload(form);
  assert.equal(value.inspection_id, 'inspection_demo');
  assert.ok(!('token' in value.connection));
  assert.ok(!('uri' in value.connection));
  assert.ok(!('tables' in value));
  assert.equal(value.revision, 2);
  const dataset = sourcePayload({
    ...emptySource(),
    name: 'Synthetic dataset',
    provider: 'neo4j',
    input_ids: ['input_demo'],
  });
  assert.equal(dataset.provider, 'nl2sql_http');
  assert.deepEqual(dataset.input_ids, ['input_demo']);
  assert.ok(!('connection' in dataset));
});

test('new Neo4j sources default to the consumer graph scope properties', () => {
  const { connection } = emptySource();
  assert.equal(connection.workspace_property, 'd2o_workspace_id');
  assert.equal(connection.ontology_property, 'd2o_ontology_id');
});

test('model choices show name, model and the consumer default', () => {
  assert.equal(DEFAULT_CONCURRENCY, 4);
  assert.equal(
    modelOptionLabel({ id: 'global:synthetic', name: 'Synthetic', model: 'synthetic-model', is_default: true }),
    'Synthetic · synthetic-model（默认）',
  );
  assert.equal(modelOptionLabel({ id: 'global:synthetic_alt', name: '', model: '' }), 'global:synthetic_alt');
});

test('version column shows the chosen release and the snapshot that ran', () => {
  const snapshot = { kind: 'evaluation_snapshot', version: '0.1.13', release_id: 'release_snapshot', base_release_id: 'release_demo' };
  assert.deepEqual(packageVersions({ base_ref: { version: '0.1.7', release_id: 'release_demo' }, package_ref: snapshot }), {
    base: '0.1.7', baseId: 'release_demo', snapshot: '0.1.13',
  });
  assert.deepEqual(packageVersions({ package_ref: { kind: 'release', version: '0.1.7', release_id: 'release_demo' } }), {
    base: '0.1.7', baseId: 'release_demo', snapshot: '',
  });
  // Older runs only kept the snapshot, so the base version is unknown but its release ID remains.
  assert.deepEqual(packageVersions({ package_ref: snapshot }), { base: '', baseId: 'release_demo', snapshot: '0.1.13' });
});

test('start time prefers when execution began and falls back to creation for queued runs', () => {
  const created = new Date(2026, 0, 2, 3, 4, 5).getTime() / 1000,
    started = new Date(2026, 9, 30, 14, 5, 9).getTime() / 1000;
  assert.deepEqual(runStartTime({ created_at: created, started_at: started }), { date: '2026-10-30', time: '14:05:09' });
  assert.deepEqual(runStartTime({ created_at: created }), { date: '2026-01-02', time: '03:04:05' });
  assert.equal(runStartTime({}), null);
});

test('starting without a judge explains why instead of only disabling the button', () => {
  assert.match(judgeMissingReason({ source: 'disabled', configured: false }), /管理员已停用默认判分模型/);
  assert.match(judgeMissingReason({ source: 'deployment', configured: false }), /没有可用的评测判分模型/);
  assert.match(judgeMissingReason(undefined), /没有可用的评测判分模型/);
});
