import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

// Exercise the component's actual async handlers with deferred network responses.
// State setters deliberately do not rerender: the synchronous guard must also
// work when confirmation fires before React has committed its loading state.
const source = readFileSync(new URL('../src/components/LeftPane.jsx', import.meta.url), 'utf8');
const handler = (name, next) => source.slice(source.indexOf(`  const ${name} =`), source.indexOf(next, source.indexOf(`  const ${name} =`)));
const handlers = [
  handler('invalidateSchemaRequest', '  const currentProjectId'),
  handler('openSheetScope', '  const changeDirective'),
  handler('saveSheetScope', '  // 资料变更提醒'),
].join('\n');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function scope(names = ['table_a']) {
  return { file_sha256: 'synthetic_hash', included_sheets: names,
    sheets: names.map((name) => ({ name, issues: [] })) };
}

function harness() {
  const previews = [], saves = [];
  const state = { editor: { projectId: 'project_alpha', path: 'resource/sample.xlsx',
    scope: scope(), selected: ['table_b'] }, checking: false, saving: false };
  const context = {
    project: { id: 'project_alpha' }, currentProjectId: { current: 'project_alpha' },
    schemaRequest: { current: 0 }, schemaChecking: { current: false },
    sheetEditor: state.editor, savingSheets: false, directives: {},
    setCheckingSheets: (value) => { state.checking = value; },
    setSavingSheets: (value) => { state.saving = value; },
    setSheetEditor: (value) => { state.editor = typeof value === 'function' ? value(state.editor) : value; },
    loadDirectives: async () => {}, onRefresh: () => {}, message: { success: () => {} },
    api: {
      previewResourceSchema: () => { const request = deferred(); previews.push(request); return request.promise; },
      setResourceDirective: (...args) => { const request = deferred(); saves.push({ args, ...request }); return request.promise; },
    },
  };
  const actions = runInNewContext(`${handlers}\n({openSheetScope, saveSheetScope, invalidateSchemaRequest})`, context);
  const recheck = () => actions.openSheetScope('resource/sample.xlsx', 'on_demand', false, true);
  return { context, state, previews, saves, actions, recheck };
}

test('recheck blocks confirmation even before a rerender, then permits saving the chosen range', async () => {
  const h = harness();
  const checking = h.recheck();
  assert.equal(h.state.checking, true);
  await h.actions.saveSheetScope();
  assert.equal(h.saves.length, 0);
  h.previews[0].resolve(scope());
  await checking;
  assert.equal(h.state.checking, false);
  h.state.editor.selected = ['table_b'];
  h.context.sheetEditor = h.state.editor;
  const version = h.context.schemaRequest.current;
  const saving = h.actions.saveSheetScope();
  assert.ok(h.context.schemaRequest.current > version);
  assert.deepEqual(h.saves[0].args[4].included_sheets, ['table_b']);
  h.saves[0].resolve({});
  await saving;
  assert.equal(h.state.editor, null);
});

for (const outcome of ['resolve', 'reject']) {
  test(`cancelled recheck ${outcome} cannot reopen a modal or clear a newer request's loading state`, async () => {
    const h = harness();
    const old = h.recheck();
    h.actions.invalidateSchemaRequest();
    h.state.editor = null;
    const current = h.recheck();
    h.previews[0][outcome](outcome === 'resolve' ? scope() : new Error('synthetic failure'));
    await old;
    assert.equal(h.state.editor, null);
    assert.equal(h.state.checking, true);
    await h.actions.saveSheetScope();
    assert.equal(h.saves.length, 0);
    h.previews[1].resolve(scope(['table_b']));
    await current;
    assert.equal(h.state.checking, false);
    assert.deepEqual(h.state.editor.selected, ['table_b']);
  });
}

test('failed recheck blocks saving stale scope and a successful retry restores confirmation', async () => {
  const h = harness();
  const failed = h.recheck();
  h.previews[0].reject(new Error('synthetic failure'));
  await failed;
  assert.equal(h.state.checking, false);
  assert.equal(h.state.editor.scope.error, 'synthetic failure');
  h.context.sheetEditor = h.state.editor;
  await h.actions.saveSheetScope();
  assert.equal(h.saves.length, 0);
  const retry = h.recheck();
  h.previews[1].resolve(scope());
  await retry;
  assert.equal(h.state.editor.scope.file_sha256, 'synthetic_hash');
});

test('switching project discards the old recheck response', async () => {
  const h = harness();
  const checking = h.recheck();
  h.context.currentProjectId.current = 'project_beta';
  h.actions.invalidateSchemaRequest();
  h.state.editor = null;
  h.previews[0].resolve(scope());
  await checking;
  assert.equal(h.state.editor, null);
  assert.equal(h.state.checking, false);
});

test('a save failure from an invalidated editor does not alter the new editor', async () => {
  const h = harness();
  const saving = h.actions.saveSheetScope();
  h.actions.invalidateSchemaRequest();
  const replacement = { path: 'resource/other.xlsx', scope: scope(['other']) };
  h.state.editor = replacement;
  h.saves[0].reject(new Error('synthetic failure'));
  await saving;
  assert.equal(h.state.editor, replacement);
  assert.equal(h.state.editor.scope.file_sha256, 'synthetic_hash');
});

test('version rollback preview blockers are handled before reading change details', () => {
  const start = source.indexOf('      const preview = await api.revertPreview');
  const guard = source.slice(start, source.indexOf('      const { added, modified, deleted } = preview.changes;', start));
  assert.match(guard, /if \(!preview\?\.ready\)/);
  assert.match(guard, /preview\?\.blockers/);
  assert.match(guard, /message\.warning/);
});
