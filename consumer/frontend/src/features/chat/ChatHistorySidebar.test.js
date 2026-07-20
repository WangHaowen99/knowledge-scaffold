import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const sidebarSource = readFileSync(join(currentDir, 'ChatHistorySidebar.jsx'), 'utf8');

test('history renders distinct accessible icons for queued and running sessions', () => {
  assert.equal(sidebarSource.includes('Clock3'), true);
  assert.equal(sidebarSource.includes('Loader2'), true);
  assert.equal(sidebarSource.includes("className={runStatus === 'running' ? 'spin' : undefined}"), true);
  assert.equal(sidebarSource.includes('aria-label={runStatusLabel}'), true);
  assert.equal(sidebarSource.includes("runStatus === 'queued' ? '排队中' : '运行中'"), true);
});
