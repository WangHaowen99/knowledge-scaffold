// Sheet 范围的请求竞态（移植自 develop 的同名测试，改测 pages/resources/sheetScopeModel.js 的流程对象）：
// 网络响应用 deferred 控制先后；setter 不触发重渲染，同步的守卫在确认先于渲染时也得生效。
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { cleanScope, isSchemaTable, scopeChip, sheetScopeFlow } from '../src/pages/resources/sheetScopeModel.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function scope(names = ['table_a'], issues = []) {
  return { revision: 'synthetic_rev', included_sheets: names, pending: false, error: null,
    sheets: names.map((name) => ({ name, issues, schema_selectable: true, selectable: true })) };
}

function harness() {
  const previews = [];
  const saves = [];
  const saved = [];
  const errors = [];
  let state = { editor: null, checking: false, saving: false };
  const api = {
    resourceSchemaPreview: (pid, path) => { const r = deferred(); previews.push({ pid, path, ...r }); return r.promise; },
    setResourcePurpose: (pid, body) => { const r = deferred(); saves.push({ pid, body, ...r }); return r.promise; },
  };
  const flow = sheetScopeFlow({ api, set: (s) => { state = s; }, onSaved: (r) => saved.push(r), onError: (e) => errors.push(e) });
  flow.setProject('project_alpha');
  const m = { path: 'resource/excel/sample.xlsx', name: 'sample.xlsx', reading_mode: 'on_demand' };
  return { flow, previews, saves, saved, errors, m, state: () => state };
}

test('重新检查期间不能确认（不等重渲染），检查完能保存所选范围', async () => {
  const h = harness();
  const opening = h.flow.edit(h.m);
  h.previews[0].resolve(scope(['table_a', 'table_b']));
  await opening;
  h.flow.toggle('table_a');
  const checking = h.flow.recheck();
  assert.equal(h.state().checking, true);
  await h.flow.confirm();
  assert.equal(h.saves.length, 0);
  h.previews[1].resolve(scope(['table_a', 'table_b']));
  await checking;
  assert.equal(h.state().checking, false);
  h.flow.toggle('table_a');
  const saving = h.flow.confirm();
  assert.deepEqual(h.saves[0].body.schema_selection, { mode: 'manual', included_sheets: ['table_b'], revision: 'synthetic_rev' });
  assert.equal(h.saves[0].body.purpose, 'schema');
  h.saves[0].resolve({ material: { path: h.m.path } });
  await saving;
  assert.equal(h.state().editor, null);
  assert.equal(h.saved.length, 1);
});

for (const outcome of ['resolve', 'reject']) {
  test(`被取消的检查（${outcome}）不会重开弹窗，也不会清掉新检查的进行中状态`, async () => {
    const h = harness();
    const old = h.flow.edit(h.m);
    h.flow.cancel();
    const current = h.flow.edit(h.m);
    if (outcome === 'resolve') h.previews[0].resolve(scope());
    else h.previews[0].reject(new Error('synthetic failure'));
    await old;
    assert.equal(h.state().editor, null);
    assert.equal(h.state().checking, true);
    await h.flow.confirm();
    assert.equal(h.saves.length, 0);
    h.previews[1].resolve(scope(['table_b']));
    await current;
    assert.equal(h.state().checking, false);
    assert.deepEqual(h.state().editor.selected, ['table_b']);
  });
}

test('重新检查失败挡住保存旧范围，重试成功后恢复确认', async () => {
  const h = harness();
  const opening = h.flow.edit(h.m);
  h.previews[0].resolve(scope());
  await opening;
  const failed = h.flow.recheck();
  h.previews[1].reject(new Error('synthetic failure'));
  await failed;
  assert.equal(h.state().checking, false);
  assert.equal(h.state().editor.scope.error, 'synthetic failure');
  assert.equal(h.flow.canSave(), false);
  await h.flow.confirm();
  assert.equal(h.saves.length, 0);
  const retry = h.flow.recheck();
  h.previews[2].resolve(scope());
  await retry;
  assert.equal(h.state().editor.scope.revision, 'synthetic_rev');
  assert.equal(h.flow.canSave(), true);
});

test('切换项目丢掉旧项目的检查结果', async () => {
  const h = harness();
  const checking = h.flow.edit(h.m);
  h.flow.setProject('project_beta');
  h.previews[0].resolve(scope());
  await checking;
  assert.equal(h.state().editor, null);
  assert.equal(h.state().checking, false);
});

test('作废后的保存失败不改新弹窗', async () => {
  const h = harness();
  const opening = h.flow.edit(h.m);
  h.previews[0].resolve(scope());
  await opening;
  const saving = h.flow.confirm();
  h.flow.cancel();
  const other = { path: 'resource/excel/other.xlsx', name: 'other.xlsx' };
  const reopening = h.flow.edit(other);
  h.previews[1].resolve(scope(['other']));
  await reopening;
  const replacement = h.state().editor;
  h.saves[0].reject(new Error('synthetic failure'));
  await saving;
  assert.equal(h.state().editor, replacement);
  assert.equal(h.state().editor.scope.revision, 'synthetic_rev');
  assert.equal(h.state().saving, false);
});

test('保存失败（文件变了）要先重新检查才能再确认', async () => {
  const h = harness();
  const opening = h.flow.edit(h.m);
  h.previews[0].resolve(scope());
  await opening;
  const saving = h.flow.confirm();
  h.saves[0].reject(new Error('文件已变化'));
  await saving;
  assert.equal(h.state().editor.scope.error, '文件已变化');
  assert.equal(h.flow.canSave(), false);
});

test('改用途为数据 Schema：没问题直接保存（auto），有问题才弹窗，还没转出文本时只改用途', async () => {
  const clean = harness();
  const a = clean.flow.choose(clean.m);
  clean.previews[0].resolve(scope(['table_a']));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(clean.saves[0].body.schema_selection, { mode: 'auto' });
  clean.saves[0].resolve({ material: { path: clean.m.path } });
  await a;
  assert.equal(clean.state().editor, null);
  assert.equal(clean.saved.length, 1);

  const issues = harness();
  const b = issues.flow.choose(issues.m);
  issues.previews[0].resolve(scope(['table_a'], [{ code: 'non_identifier_header', severity: 'warning', message: 'x' }]));
  await b;
  assert.equal(issues.saves.length, 0);
  assert.ok(issues.state().editor);

  const pending = harness();
  const c = pending.flow.choose(pending.m);
  pending.previews[0].resolve({ pending: true, sheets: [], included_sheets: [] });
  await new Promise((r) => setImmediate(r));
  assert.equal(pending.saves[0].body.schema_selection, undefined);
  pending.saves[0].resolve({ material: { path: pending.m.path } });
  await c;

  const failed = harness();
  const d = failed.flow.choose(failed.m);
  failed.previews[0].reject(new Error('旧版 .xls 要另存为 .xlsx'));
  await d;
  assert.equal(failed.errors.length, 1);
  assert.equal(failed.state().editor, null);
});

test('表头有错误的 Sheet 勾不上', async () => {
  const h = harness();
  const opening = h.flow.edit(h.m);
  const s = scope(['table_a', 'dup']);
  s.sheets[1] = { name: 'dup', schema_selectable: false, issues: [{ code: 'duplicate_columns', severity: 'error', message: '重复' }] };
  s.included_sheets = ['table_a', 'dup'];
  h.previews[0].resolve(s);
  await opening;
  assert.deepEqual(h.state().editor.selected, ['table_a']);
  h.flow.toggle('dup');
  assert.deepEqual(h.state().editor.selected, ['table_a']);
});

test('摘要与判定', () => {
  assert.equal(isSchemaTable('a.XLSX'), true);
  assert.equal(isSchemaTable('a.xls'), false);
  assert.equal(scopeChip({ included_sheets: ['a'], reference_sheets: ['b', 'c'] }).text, '数据表 1 · 参考 2');
  assert.equal(scopeChip({ error: 'x' }).text, 'Sheet 范围待确认');
  assert.equal(scopeChip({ pending: true }), null);
  assert.equal(cleanScope(scope()), true);
  assert.equal(cleanScope({ ...scope(), error: 'x' }), false);
});
