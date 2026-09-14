// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  abortStreamController,
  abortStreamControllers,
  classifyRunProgressFromPublicEvent,
  formatElapsedMs,
  isSilentStreamAbort,
  isTerminalRunStatus,
  messageStatusForTerminalRun,
  resolveRunProgressView,
  resolveRunStartedAt,
  streamStatusForAdmission,
  streamStatusForConnection,
  streamStatusForError,
  streamStatusForTerminal,
} from './streamLifecycle.js';

test('stream lifecycle distinguishes queued, running and reconnecting transport', () => {
  assert.equal(streamStatusForAdmission('queued'), 'queued');
  assert.equal(streamStatusForAdmission('running'), 'reconnecting');
  assert.equal(streamStatusForConnection(0), 'streaming');
  assert.equal(streamStatusForConnection(2), 'reconnecting');
});

test('run progress formats elapsed time and staged active status', () => {
  assert.equal(formatElapsedMs(0), '00:00');
  assert.equal(formatElapsedMs(65_000), '01:05');
  assert.equal(formatElapsedMs(3_665_000), '1:01:05');
  assert.equal(formatElapsedMs(Number.POSITIVE_INFINITY), '00:00');

  assert.equal(
    resolveRunProgressView({
      isLoading: true,
      status: 'streaming',
      startedAt: 1_000,
    }, 3_000).summary,
    '执行问答任务中 · 已耗时 00:02',
  );

  const startedAt = 1_000;
  const progress = resolveRunProgressView({
    isLoading: true,
    status: 'streaming',
    startedAt,
    progressPhase: 'business_data',
  }, startedAt + 8_000);

  assert.equal(progress.label, '查询业务数据中');
  assert.equal(progress.elapsedText, '00:08');
  assert.equal(progress.summary, '查询业务数据中 · 已耗时 00:08');
});

test('run progress restores its original start time after a page reload', () => {
  const resumedAt = Date.parse('2026-07-31T04:05:00.000Z');
  const clickedAt = resumedAt - 125_000;

  assert.equal(
    resolveRunStartedAt({ runId: `agentic-${clickedAt}-abcdef` }, resumedAt),
    clickedAt,
  );
  assert.equal(resolveRunStartedAt({ runId: 'agentic-legacy-id' }, resumedAt), resumedAt);
  assert.equal(
    resolveRunProgressView({
      isLoading: true,
      status: 'streaming',
      startedAt: clickedAt,
      progressPhase: 'running',
    }, resumedAt).summary,
    '执行问答任务中 · 已耗时 02:05',
  );
});

test('run progress keeps the current phase while elapsed time keeps increasing', () => {
  const startedAt = 10_000;
  const base = {
    isLoading: true,
    status: 'streaming',
    startedAt,
    progressPhase: 'tool',
  };

  const progress = resolveRunProgressView(base, startedAt + 10 * 60_000);
  assert.equal(progress.label, '执行工具任务中');
  assert.equal(progress.summary, '执行工具任务中 · 已耗时 10:00');
  assert.equal(
    resolveRunProgressView({ ...base, status: 'queued', queue: { position: 3 } }, startedAt + 3 * 60_000).summary,
    '排队等待中 · 第 3 位 · 已耗时 03:00',
  );
});

test('run progress classifies PublicEvents into user-visible phases', () => {
  assert.equal(
    classifyRunProgressFromPublicEvent({ event_type: 'turn.started', payload: {} }).phase,
    'analyzing',
  );
  assert.equal(
    classifyRunProgressFromPublicEvent({ event_type: 'token.delta', payload: { channel: 'content' } }).phase,
    'generating',
  );
  const businessDataEvent = {
    event_type: 'tool.started',
    payload: { tool_name: 'business_data_query' },
  };
  assert.equal(
    classifyRunProgressFromPublicEvent(businessDataEvent).phase,
    'business_data',
  );
  assert.equal(
    classifyRunProgressFromPublicEvent({ event_type: 'tool.started', payload: { tool_name: 'web_search' } }).phase,
    'web_search',
  );
  assert.equal(
    classifyRunProgressFromPublicEvent({ event_type: 'tool.started', payload: { tool_name: 'python_script' } }).phase,
    'script',
  );
  assert.equal(
    classifyRunProgressFromPublicEvent({ event_type: 'tool.finished', payload: { tool_name: 'python_script' } }).phase,
    'analyzing',
  );
  assert.equal(
    classifyRunProgressFromPublicEvent({ event_type: 'subagent.started', payload: {} }).phase,
    'subagent',
  );
  assert.equal(
    resolveRunProgressView({
      isLoading: true,
      status: 'cancel_error',
      error: '后端未确认取消，运行仍在继续',
      startedAt: 1_000,
    }, 6_000).summary,
    '后端未确认取消，运行仍在继续 · 已耗时 00:05',
  );
});

test('terminal run outcomes keep their message semantics after a 410 resume', () => {
  assert.equal(messageStatusForTerminalRun('completed'), 'finished');
  assert.equal(messageStatusForTerminalRun('failed'), 'error');
  assert.equal(messageStatusForTerminalRun('cancelled'), 'cancelled');
  assert.equal(messageStatusForTerminalRun('awaiting_input'), 'awaiting');
});

test('stream lifecycle converges all terminal run outcomes', () => {
  assert.equal(streamStatusForTerminal('completed'), 'finished');
  assert.equal(streamStatusForTerminal('success'), 'finished');
  assert.equal(streamStatusForTerminal('awaiting_input'), 'awaiting');
  assert.equal(streamStatusForTerminal('cancelled'), 'cancelled');
  assert.equal(streamStatusForTerminal('error'), 'error');
  assert.equal(streamStatusForError({ name: 'AbortError' }), 'cancelled');
  assert.equal(streamStatusForError(new Error('network')), 'error');
});

test('run terminal detection covers completed and awaiting outcomes', () => {
  for (const status of ['success', 'completed', 'error', 'cancelled', 'awaiting_input']) {
    assert.equal(isTerminalRunStatus(status), true);
  }
  assert.equal(isTerminalRunStatus('running'), false);
  assert.equal(isTerminalRunStatus('queued'), false);
});

test('scope disconnect aborts controllers silently while user abort remains visible', () => {
  const silent = new AbortController();
  const visible = new AbortController();
  abortStreamControllers(new Map([['scope', silent]]), { silent: true });
  abortStreamController(visible);

  assert.equal(silent.signal.aborted, true);
  assert.equal(visible.signal.aborted, true);
  assert.equal(isSilentStreamAbort(silent), true);
  assert.equal(isSilentStreamAbort(visible), false);
});
