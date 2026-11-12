// 已发布版本的独立评测状态（ADR-0092，从 develop 的 lib/packageEvaluation.js 移植）；评测状态全部为合成数据
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createApi } from '../src/api/endpoints.js';
import {
  PACKAGE_PUBLISHED,
  canReportFailure,
  currentRevisionValidation,
  hasFeedback,
  mergeStatus,
  needsPolling,
  notifyPackagePublished,
  optimizationBlock,
  publishedNotices,
  processingState,
  releaseEvaluation,
  releaseFeedback,
  releaseSolverValidation,
  solverEvaluationTarget,
  writebackNotice,
} from '../src/pages/evaluation/evaluationModel.js';

// 记下接口函数发出的请求，不发网络
function recordingApi(reply = {}) {
  const calls = [];
  const record = (method) => async (path, ...rest) => {
    calls.push({ method, path, ...(method === 'GET' ? { query: rest[0]?.query } : { body: rest[0] }) });
    if (reply instanceof Error) throw reply;
    return reply;
  };
  const client = { get: record('GET'), post: record('POST'), put: record('PUT'), patch: record('PATCH'), del: record('DELETE') };
  return { api: createApi(client), calls };
}

const entry = (artifacts) => ({
  release_id: 'release-a', version: '0.1.0', run_id: 'a'.repeat(32), status: 'completed',
  artifacts: { revision: 1, status: 'ready', stem: 'eval-20260101-000000-aaaaaa', report: 'ready', suggestions: 'ready', ...artifacts },
  summary: { total: 2, pass: 1, partial: 0, fail: 1, error: 0, manual: 0, valid: 2, score: 50, result_status: 'complete' },
});
const status = (extra = {}) => ({
  configured: true, reachable: true, ontology_id: 'pkg_demo', current_release_id: 'release-a',
  current_version: '0.1.0', active: null, releases: {}, ...extra,
});

test('unconfigured evaluation disables both buttons with the backend reason', () => {
  const state = releaseEvaluation(status({ configured: false, message: '未配置评测服务地址' }), 'release-a');
  assert.deepEqual(state.report, { disabled: true, reason: '未配置评测服务地址' });
  assert.deepEqual(state.suggestions, state.report);
});

test('a running evaluation of the version disables both buttons', () => {
  const state = releaseEvaluation(status({
    active: { run_id: 'b'.repeat(32), release_id: 'release-a', version: '0.1.0', status: 'running' },
    releases: { 'release-a': entry() },
  }), 'release-a');
  assert.equal(state.report.reason, '评测进行中，完成后可查看');
  assert.equal(state.suggestions.disabled, true);
});

test('a version without a finished evaluation stays disabled', () => {
  const state = releaseEvaluation(status(), 'release-b');
  assert.equal(state.report.reason, '该版本尚未完成评测');
  assert.equal(state.entry, null);
});

test('a ready report enables the report while suggestions are still generating', () => {
  const state = releaseEvaluation(status({ releases: { 'release-a': entry({ status: 'generating', suggestions: 'generating' }) } }), 'release-a');
  assert.equal(state.report.disabled, false);
  assert.deepEqual(state.suggestions, { disabled: true, reason: '优化建议生成中' });
});

test('failed suggestions stay openable so the user can regenerate them', () => {
  const state = releaseEvaluation(status({ releases: { 'release-a': entry({ status: 'failed', suggestions: 'failed' }) } }), 'release-a');
  assert.equal(state.suggestions.disabled, false);
});

test('polling runs only while an evaluation or its artifacts are in progress', () => {
  assert.equal(needsPolling(null), false);
  assert.equal(needsPolling(status({ configured: false })), false);
  assert.equal(needsPolling(status({ releases: { 'release-a': entry() } })), false);
  assert.equal(needsPolling(status({ active: { run_id: 'c'.repeat(32), release_id: 'release-b' } })), true);
  assert.equal(needsPolling(status({ releases: { 'release-a': entry({ suggestions: 'pending' }) } })), true);
});

test('optimization requires a chat and the current project version', () => {
  assert.equal(optimizationBlock(status(), 'release-a', false), '请在项目内打开已发布版本后执行评测优化');
  assert.equal(optimizationBlock(status(), 'release-b', true), '该版本与当前建模项目版本 0.1.0 不一致，不能评测优化');
  assert.equal(optimizationBlock(status({ current_release_id: null, current_version: null }), 'release-a', true),
    '该版本与当前建模项目版本 未发布 不一致，不能评测优化');
  assert.equal(optimizationBlock(status(), 'release-a', true), '');
});

test('index-only refreshes keep the last reconciled reachability', () => {
  assert.equal(mergeStatus(status({ reachable: false }), status({ reachable: null })).reachable, false);
  assert.equal(mergeStatus(null, status({ reachable: null })).reachable, null);
});

test('status reads pass the reconcile flag and optimization posts to the run', async () => {
  const { api, calls } = recordingApi();
  await api.evaluations('project-alpha', { reconcile: true });
  await api.evaluations('project-alpha');
  await api.optimizeEvaluation('project-alpha', 'run-a');
  assert.deepEqual(calls, [
    { method: 'GET', path: '/projects/project-alpha/evaluations', query: { reconcile: 1 } },
    { method: 'GET', path: '/projects/project-alpha/evaluations', query: { reconcile: 0 } },
    { method: 'POST', path: '/projects/project-alpha/evaluations/run-a/optimization', body: {} },
  ]);
});

test('reports, answers and legacy records read their own routes', async () => {
  const { api, calls } = recordingApi();
  await api.evaluationReport('project-alpha', 'run-a');
  await api.evaluationSuggestions('project-alpha', 'run-a');
  await api.evaluationAnswer('project-alpha', 'run-a', 'case-2');
  await api.regenerateEvaluation('project-alpha', 'run-a');
  await api.legacyEvaluations('project-alpha');
  await api.legacyEvaluation('project-alpha', 'eval-20261001-090000');
  await api.legacyEvaluationAnswer('project-alpha', 'eval-20261001-090000', 'r 1');
  await api.evaluationEntry();
  assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), [
    'GET /projects/project-alpha/evaluations/run-a/report',
    'GET /projects/project-alpha/evaluations/run-a/suggestions',
    'GET /projects/project-alpha/evaluations/run-a/answers/case-2',
    'POST /projects/project-alpha/evaluations/run-a/regenerate',
    'GET /projects/project-alpha/legacy-evaluations',
    'GET /projects/project-alpha/legacy-evaluations/eval-20261001-090000',
    'GET /projects/project-alpha/legacy-evaluations/eval-20261001-090000/answers/r%201',
    'GET /evaluation/entry',
  ]);
});

test('a version mismatch surfaces the backend message and code', async () => {
  const error = Object.assign(new Error('评测版本 0.1.0 与当前建模项目版本 0.1.1 不一致'), { code: 'version_mismatch' });
  const { api } = recordingApi(error);
  await assert.rejects(api.optimizeEvaluation('project-alpha', 'run-a'), (e) => {
    assert.equal(e.message, '评测版本 0.1.0 与当前建模项目版本 0.1.1 不一致');
    assert.equal(e.code, 'version_mismatch');
    return true;
  });
});

// 建模回写与消费反馈来源；版本和说明都是合成值
test('feedback runs are read from their own section and do not wait for an active evaluation', () => {
  const feedback = { 'release-a': entry({}) };
  const active = { release_id: 'release-a', run_id: 'b'.repeat(32) };
  assert.equal(releaseFeedback(status({ feedback }), 'release-a').report.disabled, false);
  assert.equal(releaseFeedback(status({ feedback, active }), 'release-a').entry, feedback['release-a']);
  assert.equal(releaseFeedback(status(), 'release-a').entry, null);
  assert.equal(releaseFeedback(status({ configured: false, message: '未配置评测服务地址' }), 'release-a').report.reason, '未配置评测服务地址');
  assert.equal(needsPolling(status({ feedback: { 'release-a': entry({ suggestions: 'generating' }) } })), true);
});

test('processing state shows what modeling did with the suggestions', () => {
  assert.equal(processingState(entry({})), null);
  assert.equal(processingState({ optimization: { status: 'future_status' } }), null);
  assert.equal(processingState({ optimization: { status: 'notified' } }).label, '待处理');
  assert.match(processingState({ optimization: { status: 'notified' } }).hint, /发布新版本后自动变为已处理/);
  assert.equal(processingState(entry({ status: 'generating', suggestions: 'generating' })).label, '生成中');
  assert.equal(processingState({ ...entry({ status: 'generating' }), optimization: { status: 'notified' } }).label, '待处理');
  const done = processingState({ optimization: { status: 'modeling_done', result: { version: '0.1.2', note: '合成说明' } } });
  assert.deepEqual(done, { label: '已处理 → 0.1.2', color: 'green', hint: '合成说明' });
  assert.equal(processingState({ optimization: { status: 'modeling_failed', result: { note: '合成原因' } } }).color, 'red');
});

test('a version without a run says why its cell is empty', () => {
  assert.equal(releaseEvaluation(null, 'release-a').idle, '读取中');
  assert.equal(releaseEvaluation(status({ configured: false }), 'release-a').idle, '评测服务未配置');
  assert.equal(releaseEvaluation(status({ active: { release_id: 'release-a' } }), 'release-a').idle, '评测中');
  assert.equal(releaseEvaluation(status(), 'release-b').idle, '未评测');
});

test('the feedback column appears once the project has a feedback run', () => {
  assert.equal(hasFeedback(null), false);
  assert.equal(hasFeedback(status()), false);
  assert.equal(hasFeedback(status({ feedback: { 'release-a': entry() } })), true);
});

test('only the current version can be reported as failed, until it is handled', () => {
  const notified = { optimization: { status: 'notified' } };
  assert.equal(canReportFailure(status(), 'release-a', notified), true);
  assert.equal(canReportFailure(status(), 'release-old', notified), false);
  assert.equal(canReportFailure(status(), 'release-a', { optimization: { status: 'modeling_done' } }), false);
  assert.equal(canReportFailure(status(), 'release-a', null), false);
});

test('the write-back notice tells the optimizing user what happened', () => {
  const writeback = (extra) => ({ run_id: 'a'.repeat(32), mine: true, base_release_id: 'release-a', base_version: '0.1.0', ...extra });
  assert.equal(writebackNotice(status()), null);
  assert.equal(writebackNotice(status({ writeback: writeback({ status: 'done', version: '0.1.1', mine: false }) })), null);
  assert.equal(writebackNotice(status({ writeback: writeback({ status: 'waiting' }) })).type, 'info');
  assert.equal(writebackNotice(status({ writeback: writeback({ status: 'waiting' }), current_release_id: 'release-b' })), null);
  assert.equal(writebackNotice(status({ writeback: writeback({ status: 'done', version: '0.1.1' }) })), null);
  assert.equal(writebackNotice(status({ writeback: writeback({ status: 'pending', version: '0.1.1', message: '评测服务不可用' }) })).type, 'warning');
  assert.match(writebackNotice(status({ writeback: writeback({ status: 'refused', version: '0.1.1', message: '已驳回' }) })).text, /未接受/);
});

test('a failure is reported with its reason', async () => {
  const { api, calls } = recordingApi({ optimization: {} });
  await api.reportEvaluationFailure('project_demo', 'a'.repeat(32), '合成原因');
  assert.equal(calls[0].method, 'POST');
  assert.match(calls[0].path, /\/evaluations\/a{32}\/modeling-result$/);
  assert.deepEqual(calls[0].body, { note: '合成原因' });
});

const solverEntry = (summary, extra = {}) => ({
  ...entry(), run_id: 'b'.repeat(32), run_type: 'solver',
  solver_summary: { total: 1, passed: 1, failed: 0, first_failure_code: null, first_failure_message: null, ...summary },
  ...extra,
});

test('solver validation status is shown only for versions published with a solver contract or validated', () => {
  assert.equal(releaseSolverValidation(status(), 'release-a'), null);
  assert.equal(releaseSolverValidation(status({ solver_revisions: { 'release-a': 'sha256:r1' } }), 'release-a').state, 'unverified');
  const passed = status({ solver_revisions: { 'release-a': 'sha256:r1' }, solver_releases: { 'release-a': solverEntry({}) } });
  assert.equal(releaseSolverValidation(passed, 'release-a').state, 'passed');
  const failed = status({ solver_releases: { 'release-a': solverEntry({ passed: 0, failed: 1, first_failure_code: 'solver_tool_not_called' }) } });
  assert.match(releaseSolverValidation(failed, 'release-a').reason, /solver_tool_not_called/);
  const interrupted = status({ solver_releases: { 'release-a': solverEntry({}, { status: 'interrupted' }) } });
  assert.equal(releaseSolverValidation(interrupted, 'release-a').state, 'failed');
  const running = status({ solver_revisions: { 'release-a': 'sha256:r1' }, active: { run_id: 'c'.repeat(32), release_id: 'release-a', run_type: 'solver' } });
  assert.equal(releaseSolverValidation(running, 'release-a').state, 'running');
});

test('a running solver validation does not disable the data evaluation buttons', () => {
  const value = status({ releases: { 'release-a': entry({}) }, active: { run_id: 'c'.repeat(32), release_id: 'release-a', run_type: 'solver' } });
  assert.equal(releaseEvaluation(value, 'release-a').report.disabled, false);
  assert.equal(needsPolling(value), true);
  const data = status({ active: { run_id: 'c'.repeat(32), release_id: 'release-a' } });
  assert.equal(releaseEvaluation(data, 'release-a').report.disabled, true);
});

test('current revision validation compares the contract revision with published releases', () => {
  assert.equal(currentRevisionValidation(status(), 'sha256:r1').state, 'unpublished');
  const published = status({ solver_revisions: { 'release-a': 'sha256:r1' } });
  assert.equal(currentRevisionValidation(published, 'sha256:r1').state, 'published');
  assert.equal(currentRevisionValidation(published, 'sha256:r2').state, 'unpublished');
  const passed = status({ solver_revisions: { 'release-a': 'sha256:r1' }, solver_releases: { 'release-a': solverEntry({}) } });
  assert.deepEqual(currentRevisionValidation(passed, 'sha256:r1'), { state: 'passed', releaseId: 'release-a', version: '0.1.0' });
});

test('the latest solver validation of a revision decides whether it passed', () => {
  const two = (older, newer, extra = {}) => status({
    solver_revisions: { 'release-a': 'sha256:r1', 'release-b': 'sha256:r1' },
    solver_releases: {
      'release-a': solverEntry(older, { finished_at: 1000 }),
      'release-b': solverEntry(newer, { release_id: 'release-b', version: '0.2.0', finished_at: 2000 }),
    },
    ...extra,
  });
  assert.equal(currentRevisionValidation(two({}, { passed: 0, failed: 1 }), 'sha256:r1').state, 'published');
  assert.deepEqual(currentRevisionValidation(two({ passed: 0, failed: 1 }, {}), 'sha256:r1'),
    { state: 'passed', releaseId: 'release-b', version: '0.2.0' });
  const rerunning = two({}, {}, { active: { release_id: 'release-a', run_type: 'solver', run_id: 'c'.repeat(32) } });
  assert.equal(currentRevisionValidation(rerunning, 'sha256:r1').state, 'published');
});

const solverStatus = (extra = {}) => status({ solver_revisions: { 'release-a': 'rev-a' }, solver_releases: {}, ...extra });
const staleTarget = { release: { ontology_id: 'pkg_demo', release_id: 'release-a' }, version: '0.1.0', stale: true };

test('the solver evaluation target is the release holding the current revision', () => {
  const state = solverStatus({
    current_release_id: 'release-b',
    current_version: '0.2.0',
    solver_revisions: { 'release-a': 'rev-a', 'release-b': 'rev-b' },
  });
  assert.deepEqual(solverEvaluationTarget(state, 'rev-a', false), {
    release: { ontology_id: 'pkg_demo', release_id: 'release-a' },
    stale: false,
  });
});

test('unsaved or unpublished solver changes target the latest release and are marked stale', () => {
  assert.deepEqual(solverEvaluationTarget(solverStatus(), 'rev-a', true), staleTarget);
  assert.deepEqual(solverEvaluationTarget(solverStatus(), 'rev-new', false), staleTarget);
});

test('a project that was never published has no solver evaluation target', () => {
  assert.equal(solverEvaluationTarget(status({ current_release_id: null, current_version: null }), 'rev-a', false), null);
  assert.equal(solverEvaluationTarget(null, 'rev-a', false), null);
});

test('publishing notifies open panels with the project id', () => {
  const events = [];
  globalThis.window = { dispatchEvent: (event) => events.push(event) };
  try {
    notifyPackagePublished('project_alpha');
  } finally {
    delete globalThis.window;
  }
  assert.equal(events[0].type, PACKAGE_PUBLISHED);
  assert.deepEqual(events[0].detail, { projectId: 'project_alpha' });
});

// develop 的 usePackagePublish 发布成功后的提示
test('publishing tells what was published and whether the optimization was written back', () => {
  const receipt = { version: '0.1.2', release_id: 'release-b' };
  assert.deepEqual(publishedNotices(receipt), [{ type: 'success', text: '知识包已发布' }]);
  assert.equal(publishedNotices(receipt, { allowUnbound: true })[0].text, '已降级发布，未通过检查的对象不绑定数据表');
  assert.deepEqual(publishedNotices(receipt, { solverAcknowledged: true })[0],
    { type: 'success', text: '知识包 0.1.2 已发布，求解能力待验证', solver: true });
  assert.equal(publishedNotices({ ...receipt, local_record_pending: true })[0].text, '知识包已发布，本地发布记录待恢复');
  const done = { status: 'done', release_id: 'release-b', version: '0.1.2', base_version: '0.1.1' };
  assert.equal(publishedNotices({ ...receipt, evaluation_writeback: done })[1].text, '已回写评测服务：新版本 0.1.2 处理了 0.1.1 的优化建议');
  assert.equal(publishedNotices({ ...receipt, evaluation_writeback: { ...done, release_id: 'release-a' } }).length, 1);
  assert.deepEqual(publishedNotices({ ...receipt, evaluation_writeback: { status: 'pending' } })[1],
    { type: 'warning', text: '新版本的处理结果暂未回写评测服务，打开已发布版本时会自动重试' });
});
