import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8');
const center = readFileSync(new URL('../src/components/CenterPane.jsx', import.meta.url), 'utf8');
const studio = readFileSync(new URL('../src/components/StudioView.jsx', import.meta.url), 'utf8');

test('version rollback rebuilds the mounted Studio state', () => {
  const start = app.indexOf('  const refreshAfterRevert = useCallback');
  const handler = app.slice(start, app.indexOf('\n  useEffect(', start));
  assert.match(handler, /setStudioResetRevision\(\(value\) => value \+ 1\)/);
  assert.match(app, /studioResetRevision=\{studioResetRevision\}/);
  assert.match(center, /key=\{`\$\{project\.id\}:\$\{studioResetRevision\}`\}/);
});

test('Studio refresh reloads an open entry without overwriting a dirty form', () => {
  const start = studio.indexOf('  const refresh = useCallback');
  const handler = studio.slice(start, studio.indexOf('\n\n  const pathByName', start));
  assert.match(handler, /refreshGraph\(\)/);
  assert.match(handler, /workspaceDirtySources\(\)\.includes\('studio-editor'\)/);
  assert.match(handler, /setEntry\(null\)/);
  assert.match(handler, /openEntry\(path, elementId\)/);
  assert.match(studio, /onClick=\{refresh\}/);
});

test('diff editor registers unsaved content with rollback protection', () => {
  assert.match(center, /const diffDirty\s*=/);
  assert.match(center, /diffDraft !== diffState\.modified/);
  assert.match(center, /setWorkspaceDirty\('diff-editor', diffDirty\)/);
  assert.match(center, /setWorkspaceDirty\('diff-editor', false\)/);
});
