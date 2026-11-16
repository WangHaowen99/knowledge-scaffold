import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const editor = readFileSync(
  new URL('../src/components/StudioEntryEditor.jsx', import.meta.url),
  'utf8',
);

test('Studio exit waits for draft persistence and lease release before refreshing details', () => {
  const leaveStart = editor.indexOf('  const releaseAndExit = async () => {');
  const leave = editor.slice(leaveStart, editor.indexOf('\n\n  const scheduleDraftSave', leaveStart));
  const start = editor.indexOf('  const exit = async () => {');
  const handler = editor.slice(start, editor.indexOf('\n\n  const submit', start));

  assert.ok(start >= 0);
  assert.ok(handler.indexOf('await leaseRequestRef.current') < handler.indexOf('releaseAndExit'));
  assert.match(handler, /await (saveDraft\(sections\)|draftSaveRef\.current)/);
  assert.ok(leave.indexOf('studioReleaseLease') < leave.indexOf('onExit?.()'));
  assert.match(editor, /loading=\{exiting\} onClick=\{exit\}/);
});

test('Studio review handoff releases the lease before refreshing details', () => {
  const start = editor.indexOf("} else if (result.result === 'needs_review') {");
  const handler = editor.slice(start, editor.indexOf('\n      } else {', start));

  assert.match(handler, /await releaseAndExit\(\)/);
});
