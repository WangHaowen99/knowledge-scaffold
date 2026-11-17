import test from 'node:test';
import assert from 'node:assert/strict';
import { needsRefetch, pickSuggestions, staleSuggestions, suggestionBarHidden } from '../src/pages/workbench/suggestionModel.js';

const sug = (mode, rev, primary = null) => ({ mode, primary, secondary: [], all_commands: true, note: null, rev });

test('建议条：推来的和刷新取到的取版本号大的那份，旧历史没有版本号按 0 算', () => {
  const pushed = sug('normal', 10);
  const fetched = sug('interrupted', 12);
  assert.equal(pickSuggestions(pushed, fetched), fetched);
  assert.equal(pickSuggestions(sug('normal', 20), fetched).rev, 20);
  assert.equal(pickSuggestions({ mode: 'interrupted', primary: null, secondary: [] }, fetched), fetched, '旧历史里的那份让给刷新取到的');
  assert.equal(pickSuggestions(null, fetched), fetched);
  assert.equal(pickSuggestions(pushed, null), pushed);
  assert.equal(pickSuggestions(null, null), null);
});

test('建议条：运行中（排队、连接中）不显示，自主构建的运行控制照常显示', () => {
  assert.equal(suggestionBarHidden(sug('normal', 1), 'running'), true);
  assert.equal(suggestionBarHidden(sug('normal', 1), 'connecting'), true);
  assert.equal(suggestionBarHidden(sug('normal', 1), 'queued'), true);
  assert.equal(suggestionBarHidden(sug('running', 1), 'idle'), true);
  assert.equal(suggestionBarHidden(sug('autobuild_controls', 1), 'running'), false);
  assert.equal(suggestionBarHidden(sug('waiting_decision', 1), 'waiting_user'), false, '等你回答时给「去回答」');
  assert.equal(suggestionBarHidden(null, 'idle'), false, '没有建议时还留「全部命令」');
  assert.equal(needsRefetch(sug('running', 1), 'idle'), true);
  assert.equal(needsRefetch(sug('running', 1), 'running'), false);
  assert.equal(needsRefetch(sug('normal', 1), 'idle'), false);
});

test('建议条：点下去之前情况变了（suggestion_stale），换上接口带回的那份', () => {
  const fresh = sug('normal', 30);
  assert.equal(staleSuggestions({ code: 'suggestion_stale', details: { reason: '这一步已经不是中断状态', suggestions: fresh } }), fresh);
  assert.equal(staleSuggestions({ code: 'session_busy', details: {} }), null);
  assert.equal(staleSuggestions(null), null);
});

