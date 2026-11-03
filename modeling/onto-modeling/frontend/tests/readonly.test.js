import assert from 'node:assert/strict';
import test from 'node:test';
import { composerState, fnmatchToRegExp, isLocked, lockState, readonlyInfo, roundLockNote } from '../src/state/readonly.js';

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

test('两态：助手运行中给「回到对话里打断它」，合入冲突给「去处理」（F3：没有修改待确认）', () => {
  const a = readonlyInfo(lock('assistant_running', { reason_text: '助手正在改「超量与审批提示」这一段' }), { id: 'operation:overage' });
  assert.equal(a.label, '助手运行中');
  assert.equal(a.text, '助手正在改「超量与审批提示」这一段');
  assert.equal(a.actions[0].kind, 'interrupt');
  const p = readonlyInfo(lock('pending_confirm'), { id: 'operation:overage' });
  assert.equal(p.label, '有冲突');
  assert.equal(p.actions[0].label, '去处理');
  const r = readonlyInfo(lock('autobuild_slice'));
  assert.match(r.text, /超量与审批提示/);
});

// 期望值由 Python fnmatch.fnmatchcase 算出（后端 ScopeLock.paths 的判断口径）；后端把字面量的 [ ] * ? 转义成 [[] []] [*] [?]
const FNMATCH_CASES = [
  ['ext/trace/*.md', 'ext/trace/a.md', true], ['ext/trace/*.md', 'ext/trace/x/a.md', true],
  ['ontology/**/*.md', 'ontology/object/a.md', true], ['ontology/**/*.md', 'ontology/a.md', false],
  ['ontology/object/*', 'ontology/object/sub/a.md', true], ['a.b?', 'a.bc', true], ['a.b', 'axb', false], ['a.b?', 'a.b/', true],
  ['ontology/object/[[]草稿[]].md', 'ontology/object/[草稿].md', true], ['ontology/object/[[]草稿[]].md', 'ontology/object/X草稿].md', false],
  ['ontology/x[*]y[?].md', 'ontology/x*y?.md', true], ['ontology/x[*]y.md', 'ontology/xay.md', false], ['ontology/q[?].md', 'ontology/qa.md', false],
  ['[!a]b', 'cb', true], ['[!a]b', 'ab', false], ['[a-c]x', 'bx', true], ['[a-c]x', 'dx', false],
  ['[]a]', 'a', true], ['[]a]', ']', true], ['a[b', 'a[b', true], ['[', '[', true],
  ['*', 'ontology/object/a.md', true], ['research/*', 'research/blueprint.json', true],
  ['ontology/testcase/借阅.md', 'ontology/testcase/借阅.md', true], ['x\\y', 'x\\y', true],
];

test('写入范围路径按 Python fnmatch 判断：* 跨目录、[seq] 字符类、后端转义的字面量', () => {
  FNMATCH_CASES.forEach(([pat, path, want]) => assert.equal(fnmatchToRegExp(pat).test(path), want, `${pat} ~ ${path}`));
  // 区间写反（JS 不认）退回按字面比，不抛错
  assert.equal(fnmatchToRegExp('[z-a]').test('b'), false);
  assert.ok(isLocked({ state: 'pending_confirm', entries: [], artifacts: [], paths: ['ontology/object/[[]草稿[]].md'] }, { path: 'ontology/object/[草稿].md' }));
});

test('这一轮卡片的锁定说明随状态变', () => {
  assert.match(roundLockNote(lock('pending_confirm')), /选好保留哪边前/);
  assert.match(roundLockNote(lock('assistant_running')), /助手运行中/);
  assert.match(roundLockNote(lock('autobuild_slice')), /这一段完成前/);
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
