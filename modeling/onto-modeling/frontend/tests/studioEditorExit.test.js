// develop !608 tests/studioEditorExit.test.js 按原意移植：本分支草稿不占租约（没有申请、释放租约这一步），
// 「离开前先把草稿存完」落在：暂存排队、换条目或离开时补存一次、放弃和重读先等排着的暂存落地。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const src = (name) => readFileSync(new URL(`../src/pages/studio/${name}`, import.meta.url), 'utf8');
const editor = src('EntryEditor.jsx');
const raw = src('RawEditor.jsx');
const between = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));

test('Studio leave waits for draft persistence before refreshing details', () => {
  for (const [name, source] of [['EntryEditor', editor], ['RawEditor', raw]]) {
    assert.match(source, /enqueueLatestDraftSave\(saveQueue, dirty,/, name);
    assert.match(source, /useEffect\(\(\) => \(\) => \{ flushRef\.current\(\); \}/, name);
  }
  const discard = between(editor, 'const discard = async', '// 别处改了这个条目');
  assert.ok(discard.indexOf('await saveQueue.current') >= 0);
  assert.ok(discard.indexOf('await saveQueue.current') < discard.indexOf('discardStudioDraft'));
  assert.match(between(editor, 'const load = useCallback', 'const e = await api.studioEntry'), /await saveQueue\.current/);
  const cancel = between(raw, 'const cancel = async', 'const download');
  assert.ok(cancel.indexOf('await saveQueue.current') < cancel.indexOf('discardStudioDraft'));
});

test('Studio review handoff refreshes details after the queued draft is saved', () => {
  const submit = between(editor, 'const submit = async', 'const discard = async');
  assert.ok(submit.indexOf('await putDraft()') < submit.indexOf('submitStudioEntry'));
  assert.ok(submit.indexOf("kind: 'review'") < submit.lastIndexOf('await load('));
});
