import assert from 'node:assert/strict';
import test from 'node:test';
import { composerState, globToRegExp, isLocked, lockState, readonlyInfo, roundLockNote } from '../src/state/readonly.js';

const lock = (state, extra = {}) => ({
  state, changeset_id: 'cs-31', conversation_id: 'c1', run_id: null, slice: { id: 's5', name: '超量与审批提示' }, node: null,
  entries: [{ kind: 'operation', id: 'operation:overage', key: 'overage', name: '超量与审批提示' }],
  artifacts: [{ type: 'diagram.flowchart', key: 'overage-flow', title: '超量处理流程' }],
  paths: ['ext/trace/scenarios/**'], reason_text: '', since: null, ...extra,
});

test('没锁或 state=none 时都可编辑', () => {
  assert.equal(lockState(null), 'none');
  assert.equal(isLocked({ state: 'none', entries: [] }, { id: 'operation:overage' }), false);
  assert.equal(readonlyInfo({ state: 'none' }), null);
});

test('范围内的条目、产物、路径只读，范围外照常编辑', () => {
  const l = lock('assistant_running');
  assert.ok(isLocked(l, { id: 'operation:overage' }));
  assert.ok(isLocked(l, 'operation:overage'));
  assert.ok(isLocked(l, { type: 'diagram.flowchart', key: 'overage-flow' }));
  assert.ok(isLocked(l, { path: 'ext/trace/scenarios/s-03.md' }));
  assert.equal(isLocked(l, { id: 'object:hardness_limit' }), false);
  assert.equal(isLocked(l, { type: 'diagram.flowchart', key: 'other' }), false);
  assert.equal(readonlyInfo(l, { id: 'object:hardness_limit' }), null);
});

test('两态：助手运行中给「回到对话里打断它」，修改待确认给「去确认」', () => {
  const a = readonlyInfo(lock('assistant_running', { reason_text: '助手正在改「超量与审批提示」这一片' }), { id: 'operation:overage' });
  assert.equal(a.label, '助手运行中');
  assert.equal(a.text, '助手正在改「超量与审批提示」这一片');
  assert.equal(a.actions[0].kind, 'interrupt');
  const p = readonlyInfo(lock('pending_confirm'), { id: 'operation:overage' });
  assert.equal(p.label, '修改待确认');
  assert.equal(p.actions[0].label, '去确认');
  const r = readonlyInfo(lock('autobuild_slice'));
  assert.match(r.text, /超量与审批提示/);
});

test('glob：* 不跨目录，** 跨目录', () => {
  assert.ok(globToRegExp('ext/trace/*.md').test('ext/trace/a.md'));
  assert.equal(globToRegExp('ext/trace/*.md').test('ext/trace/x/a.md'), false);
  assert.ok(globToRegExp('ontology/**/*.md').test('ontology/object/a.md'));
  assert.ok(globToRegExp('a.b?').test('a.bc'));
  assert.equal(globToRegExp('a.b').test('axb'), false);
});

test('这一轮卡片的锁定说明随状态变', () => {
  assert.match(roundLockNote(lock('pending_confirm')), /确认生效或撤回前/);
  assert.match(roundLockNote(lock('assistant_running')), /助手运行中/);
  assert.match(roundLockNote(lock('autobuild_slice')), /这一片完成前/);
  assert.notEqual(roundLockNote(lock('pending_confirm')), roundLockNote(lock('autobuild_slice')));
});

test('输入框：运行中可补充说明（deferred），归档与只读维护时禁用', () => {
  assert.equal(composerState({ sessionState: 'running' }).delivery, 'deferred');
  assert.match(composerState({ sessionState: 'running' }).placeholder, /补充说明/);
  assert.equal(composerState({ sessionState: 'idle', autobuildRunning: true }).delivery, 'deferred');
  assert.equal(composerState({ sessionState: 'idle', archived: true }).disabled, true);
  assert.equal(composerState({ sessionState: 'idle', maintenance: { active: true, mode: 'read_only', message: '升级中' } }).placeholder, '升级中');
  assert.equal(composerState({ sessionState: 'idle', maintenance: { active: true, mode: 'reject_new_sessions', message: 'm' } }).warn, true);
  assert.equal(composerState({ sessionState: 'idle' }).disabled, false);
});
