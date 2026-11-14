import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const editor = readFileSync(
  new URL('../src/components/StudioEntryEditor.jsx', import.meta.url),
  'utf8',
);
const view = readFileSync(new URL('../src/components/StudioView.jsx', import.meta.url), 'utf8');

test('Studio does not load a draft bound to an obsolete baseline', () => {
  assert.match(editor, /draft\?\.state === 'stale'/);
  assert.match(editor, /setSections\(merged\)/);
  assert.match(editor, /上次草稿基于旧版本，未自动载入/);
  assert.match(view, /entry\.draft\.state === 'stale' \? '旧版本草稿已失效'/);
});
