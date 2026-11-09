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
  taskRows,
  artifactState,
  notificationState,
  optimizationState,
  optimizationTag,
  feedbackStatus,
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
  assert.throws(() => parseCases('[{"question":"Synthetic","reference":5}]'), /格式/);
});
test('case imports accept a bare array or a cases wrapper', () => {
  const expected = [{ question: 'Synthetic question', reference: '2' }];
  assert.deepEqual(parseCases('{"cases":[{"question":"Synthetic question","groundTruth":"2"}]}'), expected);
  assert.deepEqual(parseCases('{"cases":[{"question":"Synthetic question","reference":"2"}]}'), expected);
  assert.throws(() => parseCases('{"cases":[{"question":" "}]}'), /用例 1/);
});
test('case imports report wrong structure and wrong count separately', () => {
  for (const text of ['{}', 'null', '"text"', '5', '{"cases":{}}', '{"items":[]}'])
    assert.throws(() => parseCases(text), /最外层应为用例数组/, text);
  const cases = (count) => JSON.stringify(Array.from({ length: count }, () => ({ question: 'Synthetic question' })));
  assert.throws(() => parseCases('[]'), /1-500 条用例，当前文件有 0 条/);
  assert.throws(() => parseCases('{"cases":[]}'), /1-500 条用例，当前文件有 0 条/);
  assert.throws(() => parseCases(cases(501)), /1-500 条用例，当前文件有 501 条/);
  assert.equal(parseCases(cases(500)).length, 500);
  assert.throws(() => parseCases('not json'), /不是有效的 JSON/);
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
    mode: 'dataset',
    provider: 'neo4j',
    input_ids: ['input_demo'],
  });
  assert.equal(dataset.provider, 'nl2sql_http');
  assert.deepEqual(dataset.input_ids, ['input_demo']);
  assert.ok(!('connection' in dataset));
});

test('new sources default to an external connection with the consumer graph scope properties', () => {
  const { mode, connection } = emptySource();
  assert.equal(mode, 'connection');
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

// Package task rows and run outputs; identifiers are synthetic.
test('package tasks become rows of their newest run', () => {
  const rows = taskRows([{ ontology_id: 'package_demo', run_count: 3, executing: true, latest: { id: 'run_c', status: 'running' } }]);
  assert.deepEqual(rows, [{ id: 'run_c', status: 'running', run_count: 3, package_executing: true }]);
});

test('artifact state follows the run and generation status', () => {
  assert.equal(artifactState({ status: 'running' }).report, false);
  assert.equal(artifactState({ status: 'completed' }).canRegenerate, true);
  const generating = artifactState({ status: 'completed', artifacts: { status: 'generating', report: 'ready', suggestions: 'generating' } });
  assert.deepEqual([generating.report, generating.suggestions, generating.canRegenerate], [true, false, false]);
  const failed = artifactState({ status: 'failed', artifacts: { status: 'failed', report: 'ready', suggestions: 'failed' } });
  assert.deepEqual([failed.label, failed.report, failed.suggestions, failed.canRegenerate], ['优化建议生成失败', true, false, true]);
  const ready = artifactState({ status: 'completed', artifacts: { status: 'ready', report: 'ready', suggestions: 'ready', revision: 2, generated_at: 1793500000 } });
  assert.equal(ready.suggestions, true);
  assert.match(ready.hint, /^生成于 \S+ \S+ · 第 2 次生成$/);
  const first = artifactState({ status: 'completed', artifacts: { status: 'ready', report: 'ready', suggestions: 'ready', revision: 1, generated_at: 1793500000 } });
  assert.match(first.hint, /^生成于 \S+ \S+$/);
  // While generating, the card says which batch is being waited for and for how long.
  const waiting = (progress) =>
    artifactState({ status: 'completed', artifacts: { status: 'generating', report: 'ready', suggestions: 'generating', progress } }, 1793500000).hint;
  assert.equal(waiting(null), '评测报告已生成，优化建议生成中');
  assert.equal(waiting({ batch: 1, batches: 1, started_at: 1793499990 }), '评测报告已生成，优化建议生成中 · 已等待不到 1 分钟');
  assert.equal(waiting({ batch: 2, batches: 3, started_at: 1793499790 }), '评测报告已生成，优化建议生成中 · 第 2/3 批 · 已等待 3 分钟');
  assert.equal(waiting({ batch: 3, batches: 3, merging: true, started_at: 1793499400 }), '评测报告已生成，优化建议生成中 · 合并各批结果 · 已等待 10 分钟');
  const queued = artifactState({ status: 'completed', artifacts: { status: 'generating', report: 'ready', suggestions: 'generating', queue: 2 } });
  assert.equal(queued.hint, '评测报告已生成，优化建议排队中，前面还有 2 个');
  // A changed verdict rebuilds the report only: kept suggestions stay readable, missing ones wait for regeneration.
  const stale = (suggestions) =>
    artifactState({ status: 'completed', artifacts: { status: 'ready', report: 'ready', suggestions, suggestions_stale: true, revision: 2, generated_at: 1793500000 } });
  assert.deepEqual([stale('ready').label, stale('ready').suggestions, stale('ready').canRegenerate], ['判分已修改', true, true]);
  assert.match(stale('ready').hint, /优化建议基于修改前的判分/);
  assert.deepEqual([stale('pending').suggestions, /点击“重新生成”生成优化建议/.test(stale('pending').hint)], [false, true]);
});

test('notification state explains delivery and when it can be resent', () => {
  assert.equal(notificationState(undefined), null);
  assert.equal(notificationState({ status: 'none' }), null);
  assert.equal(notificationState({ status: 'disabled' }).canRetry, undefined);
  assert.equal(notificationState({ status: 'pending', attempts: 0 }).label, '发送中');
  const retrying = notificationState({ status: 'pending', attempts: 2, last_error: '建模服务返回 503', next_attempt: 1793500000 });
  assert.equal(retrying.canRetry, true);
  assert.match(retrying.hint, /已尝试 2 次（建模服务返回 503）/);
  assert.equal(notificationState({ status: 'rejected', last_error: '建模服务返回 422' }).canRetry, true);
  assert.equal(notificationState({ status: 'delivered', delivered_at: 1793500000 }).label, '已送达');
  const failed = notificationState({ status: 'failed', attempts: 3, last_error: '建模服务返回 404，请检查评测的建模服务地址和通知凭据' });
  assert.deepEqual([failed.label, failed.canRetry], ['通知失败', true]);
  assert.match(failed.hint, /已尝试 3 次（建模服务返回 404/);
});

// How modeling handled a run's suggestions; versions and notes are synthetic.
test('optimization state shows what modeling did with the suggestions', () => {
  assert.equal(optimizationState({}), null);
  assert.equal(optimizationState({ optimization: { status: 'notified' } }).label, '待建模处理');
  assert.equal(optimizationState({ optimization: { status: 'awaiting_review' } }).label, '待负责人确认');
  const done = optimizationState({
    optimization: {
      status: 'modeling_done',
      result: { outcome: 'done', release_id: 'release_next', version: '0.1.2', note: 'Synthetic note', reported_at: 1793500000 },
      history: [{ outcome: 'failed', note: 'Synthetic failure', reported_at: 1793490000 }],
    },
  });
  assert.deepEqual([done.label, done.tone, done.releaseId], ['已处理', 'green', 'release_next']);
  assert.match(done.hint, /^新版本 0\.1\.2 · \S+ \S+ · Synthetic note · 共 2 次回写$/);
  const failed = optimizationState({ optimization: { status: 'modeling_failed', result: { outcome: 'failed', note: 'Synthetic failure' } } });
  assert.deepEqual([failed.label, failed.tone, failed.releaseId], ['处理失败', 'red', undefined]);
  assert.deepEqual(optimizationTag({ optimization: { status: 'modeling_done', result: { version: '0.1.2' } } }), {
    label: '建模已处理 → 0.1.2',
    tone: 'green',
  });
  assert.equal(optimizationTag({ optimization: { status: 'notified' } }), null);
});

test('routes and states of consumer feedback runs', () => {
  assert.deepEqual(parseRoute('#/feedback'), { page: 'feedback' });
  assert.deepEqual(parseRoute('#/feedback/run_demo'), { page: 'feedback', id: 'run_demo' });
  assert.deepEqual(parseRoute('#/new?from=run_demo&release_id=release_next'), { page: 'new', from: 'run_demo', release: 'release_next' });
  assert.equal(feedbackStatus({ status: 'collecting' }).label, '收集中');
  assert.equal(feedbackStatus({ status: 'completed' }).label, '生成报告中');
  const review = { status: 'completed', artifacts: { report: 'ready' }, optimization: { status: 'awaiting_review' } };
  assert.equal(feedbackStatus(review).label, '待负责人确认');
  assert.equal(feedbackStatus({ ...review, optimization: { status: 'dismissed', reason: 'Synthetic' } }).hint, 'Synthetic');
});
