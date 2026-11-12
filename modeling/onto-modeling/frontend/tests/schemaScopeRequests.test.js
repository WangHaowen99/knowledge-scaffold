// Sheet 范围的请求竞态（移植自 develop 的同名测试）：develop 测的是 LeftPane 里的 openSheetScope / saveSheetScope /
// invalidateSchemaRequest，本分支同样的处理在 pages/resources/sheetScopeModel.js 的流程对象里，场景与断言逐条对应。
// 网络响应用 deferred 控制先后；setter 不触发重渲染，同步的守卫在确认先于渲染时也得生效。
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isSchemaTable, scopeLink, sheetScopeFlow } from '../src/pages/resources/sheetScopeModel.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function scope(names = ['table_a']) {
  return { file_sha256: 'synthetic_hash', included_sheets: names,
    sheets: names.map((name) => ({ name, issues: [] })) };
}

function harness() {
  const previews = [];
  const saves = [];
  const saved = [];
  let state = { editor: null, checking: false, saving: false };
  const api = {
    resourceSchemaPreview: (pid, path, force) => { const r = deferred(); previews.push({ pid, path, force, ...r }); return r.promise; },
    setResourcePurpose: (pid, body) => { const r = deferred(); saves.push({ pid, body, ...r }); return r.promise; },
  };
  const flow = sheetScopeFlow({ api, set: (s) => { state = s; }, onSaved: (r, info) => saved.push(info) });
  flow.setProject('project_alpha');
  const m = { path: 'resource/sample.xlsx', name: 'sample.xlsx', reading_mode: 'on_demand', schema_scope: scope() };
  flow.open(m);
  flow.toggle('table_a');
  // develop 的初始状态：编辑器开着，选了 table_b（不在 scope 里，模拟用户已改过勾选）
  const editor = () => state.editor;
  const recheck = () => flow.recheck();
  return { flow, previews, saves, saved, m, state: () => state, editor, recheck };
}

test('recheck blocks confirmation even before a rerender, then permits saving the chosen range', async () => {
  const h = harness();
  const checking = h.recheck();
  assert.equal(h.state().checking, true);
  assert.equal(h.previews[0].force, true);
  await h.flow.confirm();
  assert.equal(h.saves.length, 0);
  h.previews[0].resolve(scope(['table_a', 'table_b']));
  await checking;
  assert.equal(h.state().checking, false);
  h.flow.toggle('table_a');
  const saving = h.flow.confirm();
  assert.deepEqual(h.saves[0].body.schema_selection.included_sheets, ['table_b']);
  assert.equal(h.saves[0].body.schema_selection.file_sha256, 'synthetic_hash');
  h.saves[0].resolve({});
  await saving;
  assert.equal(h.state().editor, null);
});

for (const outcome of ['resolve', 'reject']) {
  test(`cancelled recheck ${outcome} cannot reopen a modal or clear a newer request's loading state`, async () => {
    const h = harness();
    const old = h.recheck();
    h.flow.cancel();
    h.flow.open(h.m);
    const current = h.recheck();
    if (outcome === 'resolve') h.previews[0].resolve(scope());
    else h.previews[0].reject(new Error('synthetic failure'));
    await old;
    assert.equal(h.state().checking, true);
    await h.flow.confirm();
    assert.equal(h.saves.length, 0);
    h.previews[1].resolve(scope(['table_b']));
    await current;
    assert.equal(h.state().checking, false);
    assert.deepEqual(h.state().editor.selected, ['table_b']);
  });
}

test('failed recheck blocks saving stale scope and a successful retry restores confirmation', async () => {
  const h = harness();
  const failed = h.recheck();
  h.previews[0].reject(new Error('synthetic failure'));
  await failed;
  assert.equal(h.state().checking, false);
  assert.equal(h.state().editor.scope.error, 'synthetic failure');
  await h.flow.confirm();
  assert.equal(h.saves.length, 0);
  const retry = h.recheck();
  h.previews[1].resolve(scope());
  await retry;
  assert.equal(h.state().editor.scope.file_sha256, 'synthetic_hash');
});

test('switching project discards the old recheck response', async () => {
  const h = harness();
  const checking = h.recheck();
  h.flow.setProject('project_beta');
  h.previews[0].resolve(scope());
  await checking;
  assert.equal(h.state().editor, null);
  assert.equal(h.state().checking, false);
});

test('a save failure from an invalidated editor does not alter the new editor', async () => {
  const h = harness();
  const saving = h.flow.confirm();
  h.flow.cancel();
  h.flow.open({ path: 'resource/other.xlsx', name: 'other.xlsx', schema_scope: scope(['other']) });
  const replacement = h.state().editor;
  h.saves[0].reject(new Error('synthetic failure'));
  await saving;
  assert.equal(h.state().editor, replacement);
  assert.equal(h.state().editor.scope.file_sha256, 'synthetic_hash');
});

// ---- 本分支补的：改用途为数据 Schema、查看范围、资料行链接 ----

test('改用途为数据 Schema：每个 Sheet 都没问题时直接按建议范围保存 manual，否则打开弹窗；出错时弹窗里显示错误', async () => {
  const clean = harness();
  clean.flow.cancel();
  const a = clean.flow.choose(clean.m);
  clean.previews[0].resolve(scope(['table_a']));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(clean.saves[0].body.schema_selection, { mode: 'manual', included_sheets: ['table_a'], file_sha256: 'synthetic_hash' });
  clean.saves[0].resolve({});
  await a;
  assert.deepEqual(clean.saved, [{ quick: true, count: 1 }]);
  assert.equal(clean.state().editor, null);

  const issues = harness();
  issues.flow.cancel();
  const b = issues.flow.choose(issues.m);
  const withWarning = scope(['table_a']);
  withWarning.sheets[0].issues = [{ code: 'non_identifier_header', severity: 'warning', message: 'x' }];
  issues.previews[0].resolve(withWarning);
  await b;
  assert.equal(issues.saves.length, 0);
  assert.deepEqual(issues.state().editor.selected, ['table_a']);

  const failed = harness();
  failed.flow.cancel();
  const c = failed.flow.choose(failed.m);
  failed.previews[0].reject(new Error('数据 Schema 支持 xlsx、xlsm 和 UTF-8 CSV。'));
  await c;
  assert.equal(failed.state().editor.scope.error, '数据 Schema 支持 xlsx、xlsm 和 UTF-8 CSV。');
  assert.equal(failed.flow.canSave(), false);
});

test('查看范围：直接用列表里的范围打开，表头有错误的 Sheet 不预选、也勾不上；保存后提示同 develop', async () => {
  const h = harness();
  const s = scope(['table_a', 'dup']);
  s.sheets[1] = { name: 'dup', schema_selectable: false, issues: [{ code: 'duplicate_columns', severity: 'error', message: '重复' }] };
  h.flow.open({ ...h.m, schema_scope: s });
  assert.deepEqual(h.state().editor.selected, ['table_a']);
  h.flow.toggle('dup');
  assert.deepEqual(h.state().editor.selected, ['table_a']);
  const saving = h.flow.confirm();
  h.saves[0].resolve({});
  await saving;
  assert.deepEqual(h.saved, [{ quick: false, count: 1 }]);
});

test('资料行链接与可选表格', () => {
  assert.equal(isSchemaTable('a.XLSX'), true);
  assert.equal(isSchemaTable('a.xls'), false);
  assert.equal(scopeLink({ included_sheets: ['a'], reference_sheets: ['b', 'c'] }).text, '数据表 1 · 参考 2 · 查看范围');
  assert.deepEqual(scopeLink({ error: 'x' }), { danger: true, text: 'Sheet 范围待确认 · 查看范围' });
  assert.equal(scopeLink({ pending: true }), null);
});
