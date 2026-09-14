// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  OBSERVABILITY_VIEWS,
  appendObservabilityFilters,
  buildGlobalObservabilityUrls,
  buildRunDetailUrl,
  buildSessionPerformanceUrl,
  createPerformanceWindowQuery,
  detailForSelectedRun,
  diagnosticsText,
  formatCacheHit,
  formatDuration,
  formatTimestamp,
  getFailedLocalObservabilityRuntime,
  getLocalObservabilityRuntime,
  getPageInfo,
  normalizeGlobalOverview,
  normalizeObservabilityFilters,
  normalizeRunDetail,
  resolveCollectionState,
  resolveObservabilityAccess,
  selectRunId,
} from './model.js';

test('global observability requires explicit capability for non-admin users', () => {
  assert.equal(resolveObservabilityAccess({
    view: OBSERVABILITY_VIEWS.GLOBAL,
    user: { role: 'user' },
    canViewGlobal: false,
  }).reason, 'forbidden');
  assert.equal(resolveObservabilityAccess({
    view: OBSERVABILITY_VIEWS.GLOBAL,
    user: { role: 'admin' },
    canViewGlobal: true,
  }).allowed, true);
});

test('session observability reports missing session without granting a request', () => {
  const result = resolveObservabilityAccess({
    view: OBSERVABILITY_VIEWS.SESSION,
    user: { role: 'user' },
    sessionId: '   ',
  });
  assert.equal(result.allowed, false);
  assert.equal(result.reason, 'missing-session');
});

test('window query uses exact preset and deterministic end', () => {
  const params = new URLSearchParams(createPerformanceWindowQuery('1h', 7_200_000));
  assert.equal(params.get('from'), '3600000');
  assert.equal(params.get('to'), '7200000');
});

test('filters are trimmed and empty values are omitted from query', () => {
  const normalized = normalizeObservabilityFilters({
    workspace_id: ' ws-a ',
    status: '',
    model_provider: ' glm ',
  });
  assert.deepEqual(normalized, {
    workspace_id: 'ws-a',
    status: '',
    model_provider: 'glm',
    agent_mode: '',
  });
  const params = new URLSearchParams(appendObservabilityFilters('from=1&to=2', normalized));
  assert.equal(params.get('workspace_id'), 'ws-a');
  assert.equal(params.get('model_provider'), 'glm');
  assert.equal(params.has('status'), false);
});

test('session and global URLs encode identifiers and preserve server contracts', () => {
  assert.equal(
    buildSessionPerformanceUrl('session / a', { limitRuns: 999 }),
    '/agentic/sessions/session%20%2F%20a/performance?limit_runs=200',
  );
  const urls = buildGlobalObservabilityUrls({
    windowId: '24h',
    endMs: 86_400_000,
    filters: { workspace_id: 'ws-a', status: 'error' },
    limit: 60,
    offset: 120,
  });
  const runsUrl = new URL(urls.runs, 'http://localhost');
  assert.equal(runsUrl.searchParams.get('workspace_id'), 'ws-a');
  assert.equal(runsUrl.searchParams.get('status'), 'error');
  assert.equal(runsUrl.searchParams.get('limit'), '60');
  assert.equal(runsUrl.searchParams.get('offset'), '121');
});

test('run detail URL enforces session ownership query for session view', () => {
  assert.equal(
    buildRunDetailUrl({ runId: 'run/a', sessionId: 'session b', view: OBSERVABILITY_VIEWS.SESSION }),
    '/agentic/performance/runs/run%2Fa?session_id=session+b',
  );
  const globalUrl = new URL(buildRunDetailUrl({
    runId: 'run-a',
    view: OBSERVABILITY_VIEWS.GLOBAL,
    query: 'from=1&to=2&status=error',
    workspaceId: 'ws-a',
  }), 'http://localhost');
  assert.equal(globalUrl.searchParams.get('from'), '1');
  assert.equal(globalUrl.searchParams.get('to'), '2');
  assert.equal(globalUrl.searchParams.get('workspace_id'), 'ws-a');
  assert.equal(globalUrl.searchParams.has('status'), false);
});

test('normalizers replace malformed collections with stable empty values', () => {
  const overview = normalizeGlobalOverview({ trends: null, slow_runs: {}, summary: null });
  assert.deepEqual(overview.trends, []);
  assert.deepEqual(overview.slow_runs, []);
  assert.deepEqual(overview.summary, {});
  const detail = normalizeRunDetail({ run: [], tools: null, timeline: 'bad' });
  assert.equal(detail.run, null);
  assert.deepEqual(detail.tools, []);
  assert.deepEqual(detail.timeline, []);
});

test('run selection preserves a valid selection and falls back in collection order', () => {
  const recent = [{ run_id: 'recent' }];
  const slow = [{ run_id: 'slow' }];
  assert.equal(selectRunId('recent', slow, recent), 'recent');
  assert.equal(selectRunId('missing', slow, recent), 'slow');
  assert.equal(selectRunId('', []), '');
});

test('run detail is only exposed for the currently selected run', () => {
  const detail = { run: { run_id: 'run-a' }, tools: [] };
  assert.equal(detailForSelectedRun(detail, 'run-a'), detail);
  assert.equal(detailForSelectedRun(detail, 'run-b'), null);
  assert.equal(detailForSelectedRun(detail, ''), null);
});

test('collection state keeps stale data visible when refresh fails', () => {
  assert.equal(resolveCollectionState({ loading: true, itemCount: 0 }), 'loading');
  assert.equal(resolveCollectionState({ error: 'failed', itemCount: 0 }), 'error');
  assert.equal(resolveCollectionState({ error: 'failed', itemCount: 2 }), 'stale-error');
  assert.equal(resolveCollectionState({ itemCount: 0 }), 'empty');
  assert.equal(resolveCollectionState({ itemCount: 2 }), 'ready');
});

test('pagination clamps offsets and exposes next/previous transitions', () => {
  assert.deepEqual(getPageInfo({ count: 120, limit: 50, offset: 50 }), {
    page: 2,
    pageCount: 3,
    previousOffset: 0,
    nextOffset: 100,
    hasPrevious: true,
    hasNext: true,
  });
  assert.deepEqual(getPageInfo({ count: 20, limit: 50, offset: 100 }), {
    page: 1,
    pageCount: 1,
    previousOffset: 0,
    nextOffset: 0,
    hasPrevious: false,
    hasNext: false,
  });
});

test('runtime status distinguishes local, disabled and Langfuse modes', () => {
  assert.equal(getLocalObservabilityRuntime({ observability: { enabled: true, mode: 'local' } }).tone, 'success');
  assert.equal(getLocalObservabilityRuntime({ observability: { enabled: false } }).tone, 'muted');
  assert.equal(getLocalObservabilityRuntime({ observability: { enabled: true, mode: 'langfuse' } }).tone, 'warning');
  assert.deepEqual(getFailedLocalObservabilityRuntime('配置接口不可用'), {
    loaded: true,
    enabled: null,
    mode: '',
    isWritingLocal: false,
    label: '观测配置读取失败',
    detail: '配置接口不可用',
    tone: 'error',
  });
});

test('diagnostics and metric formatters preserve useful zero and cache semantics', () => {
  assert.equal(diagnosticsText({}), '');
  assert.match(diagnosticsText({ parse_error_count: 2, truncated: true }), /损坏日志 2 条/);
  assert.equal(formatDuration(1250), '1.25 s');
  assert.equal(formatTimestamp(null), '-');
  assert.equal(formatTimestamp(undefined), '-');
  assert.equal(formatTimestamp(''), '-');
  assert.equal(formatTimestamp('not-a-number'), '-');
  assert.equal(formatCacheHit({
    llm_cache_hit_tokens: 5,
    llm_cache_total_input_tokens: 10,
    llm_cache_hit_rate: 0.5,
  }), '5 / 10 · 50%');
});
