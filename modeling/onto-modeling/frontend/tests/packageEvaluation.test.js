// Synthetic evaluation statuses; no real project data.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { api } from '../src/lib/api.js';
import { mergeStatus, needsPolling, optimizationBlock, releaseEvaluation } from '../src/lib/packageEvaluation.js';

const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
beforeEach(() => {
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
});
afterEach(() => Object.assign(globalThis, originals));

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
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, method: options.method });
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await api.packageEvaluations('project-alpha', { reconcile: true });
  await api.packageEvaluations('project-alpha');
  await api.optimizePackageEvaluation('project-alpha', 'run-a');
  assert.deepEqual(calls, [
    { path: '/api/projects/project-alpha/package/evaluations?reconcile=1', method: undefined },
    { path: '/api/projects/project-alpha/package/evaluations?reconcile=0', method: undefined },
    { path: '/api/projects/project-alpha/package/evaluations/run-a/optimization', method: 'POST' },
  ]);
});

test('a version mismatch surfaces the backend message and code', async () => {
  globalThis.fetch = async () => ({
    ok: false, status: 409,
    json: async () => ({ error: '评测版本 0.1.0 与当前建模项目版本 0.1.1 不一致', code: 'version_mismatch' }),
  });
  await assert.rejects(api.optimizePackageEvaluation('project-alpha', 'run-a'), (error) => {
    assert.equal(error.message, '评测版本 0.1.0 与当前建模项目版本 0.1.1 不一致');
    assert.equal(error.code, 'version_mismatch');
    return true;
  });
});
