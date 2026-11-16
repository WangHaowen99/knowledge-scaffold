// develop !608 tests/studioDraftReconcile.test.js 按原意移植到本分支：develop 读的是旧前端 StudioEntryEditor / StudioView 的源码，
// 本分支的 Studio 在 pages/studio：EntryEditor（选中条目就是编辑器，挂载时读 ST-04 最新内容）、RawEditor、StudioPage。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { STALE_DRAFT_TEXT, isStaleDraft, openDraft } from '../src/pages/studio/studioModel.js';

const src = (name) => readFileSync(new URL(`../src/pages/studio/${name}`, import.meta.url), 'utf8');
const editor = src('EntryEditor.jsx');
const raw = src('RawEditor.jsx');
const page = src('StudioPage.jsx');
const table = src('StudioTablePage.jsx');

test('Studio does not load a draft bound to an obsolete baseline', () => {
  const stale = { state: 'stale', form: { name: '旧' }, base_revision: 'old' };
  const active = { state: 'active', form: { name: '新' }, base_revision: 'now' };
  assert.deepEqual(openDraft(stale), { active: null, stale });
  assert.deepEqual(openDraft(active), { active, stale: null });
  assert.deepEqual(openDraft(null), { active: null, stale: null });
  assert.equal(isStaleDraft(stale), true);
  assert.equal(isStaleDraft(active), false);
  assert.match(STALE_DRAFT_TEXT, /上次草稿基于旧版本，未自动载入/);
  // 结构化编辑：stale 草稿不载入，按当前正式内容打开并提示；新的暂存以当前版本为底
  assert.match(editor, /const \{ active: d, stale \} = openDraft\(e\.draft\)/);
  assert.match(editor, /setForm\(d\?\.form \|\| formFromEntry\(e\)\)/);
  assert.match(editor, /baseRef\.current = d\?\.base_revision \|\| e\.revision/);
  assert.match(editor, /\{STALE_DRAFT_TEXT\}/);
  // 原文编辑同口径
  assert.match(raw, /setText\(restored \? d\.content : f\.content\)/);
  assert.match(raw, /\{STALE_DRAFT_TEXT\}/);
  // 条目摘要：旧版本草稿已失效
  assert.match(page, /isStaleDraft\(entry\.draft\) \? <Chip[^>]*>旧版本草稿已失效/);
});

test('Studio refreshes reviewed content and re-reads an entry before editing', () => {
  // 打开编辑器（换条目）就重新读 ST-04；别处改了（workspace.changed，含助手复核合入）且本地没改动时重读
  assert.match(editor, /const e = await api\.studioEntry\(pid, id\)/);
  assert.match(table, /env\.type === 'workspace\.changed'/);
  assert.match(table, /<EntryEditor key=\{selectedId\} id=\{selectedId\}[^>]*tick=\{tick\}/);
  assert.match(editor, /if \(tick && !dirty\.current && !raw\) load\(/);
  // 图谱页的条目摘要同样随 workspace.changed 重读
  assert.match(page, /env\.type === 'workspace\.changed'/);
});
