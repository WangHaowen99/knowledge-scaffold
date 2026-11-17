// node --test tests/ (Node 22.6+ runs these TypeScript files directly). Synthetic ids only.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { downBackendOf, parseDown, reconcileSelection, type DownBackend } from '../src/da/scope.ts';

const listed = [{ id: 'kb-1111' }, { id: 'dap_9' }];
const weknoraDown: DownBackend = { label: 'WeKnora', id_prefix: 'wk_', knowledge_bases: [{ id: 'wk_a', name: '合成库A' }] };
const kbServiceDown: DownBackend = { label: 'kb-service', id_prefix: '', knowledge_bases: [] };

test('a selection on a backend that is down is kept and named', () => {
  const r = reconcileSelection(['kb-1111', 'wk_a', 'wk_new'], listed, [weknoraDown]);
  assert.deepEqual(r.ids, ['kb-1111', 'wk_a', 'wk_new']);
  assert.deepEqual(r.down, [
    { id: 'wk_a', name: '合成库A', backend: 'WeKnora' },
    { id: 'wk_new', name: 'wk_new', backend: 'WeKnora' },
  ]);
  assert.equal(r.lost, false);
});

test('only knowledge bases of the backend that is down: kept, not "every knowledge base"', () => {
  const r = reconcileSelection(['wk_a'], listed, [weknoraDown]);
  assert.deepEqual(r.ids, ['wk_a']);
  assert.equal(r.lost, false);
});

test('deleted knowledge bases are dropped; a selection that is gone entirely is reported, not widened', () => {
  assert.deepEqual(reconcileSelection(['kb-1111', 'da_gone'], listed, [weknoraDown]).ids, ['kb-1111']);
  const gone = reconcileSelection(['da_gone', 'kb-2222'], listed, []);
  assert.deepEqual(gone.ids, []);
  assert.equal(gone.lost, true);
});

test('an empty selection stays "every knowledge base"', () => {
  assert.deepEqual(reconcileSelection([], listed, [weknoraDown]), { ids: [], down: [], lost: false });
});

test('the unprefixed backend owns ids without a prefix only', () => {
  assert.equal(downBackendOf('3f2a-uuid-like', [kbServiceDown])?.label, 'kb-service');
  assert.equal(downBackendOf('wk_a', [kbServiceDown]), undefined);
  assert.equal(downBackendOf('wk_a', [kbServiceDown, weknoraDown])?.label, 'WeKnora');
  // a backend whose prefix is unknown cannot claim anything
  assert.equal(downBackendOf('wk_x', [{ label: 'WeKnora', id_prefix: null, knowledge_bases: [] }]), undefined);
});

test('hello.unavailable is read defensively', () => {
  assert.deepEqual(parseDown(['WeKnora', '', 3, null, { label: '' }]), [
    { label: 'WeKnora', id_prefix: null, knowledge_bases: [] },
  ]);
  assert.deepEqual(parseDown([{ label: 'WeKnora', id_prefix: 'wk_', knowledge_bases: [{ id: 'wk_a' }, { name: 'x' }] }]), [
    { label: 'WeKnora', id_prefix: 'wk_', knowledge_bases: [{ id: 'wk_a', name: 'wk_a' }] },
  ]);
  assert.deepEqual(parseDown(undefined), []);
});
