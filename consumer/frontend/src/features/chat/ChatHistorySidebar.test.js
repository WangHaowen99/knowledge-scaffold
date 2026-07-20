import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const sidebarSource = readFileSync(join(currentDir, 'ChatHistorySidebar.jsx'), 'utf8');

test('history renders distinct accessible icons for queued, running and awaiting-input sessions', () => {
  assert.equal(sidebarSource.includes('Clock3'), true);
  assert.equal(sidebarSource.includes('Loader2'), true);
  assert.equal(sidebarSource.includes('MessageCircleQuestion'), true);
  assert.equal(sidebarSource.includes("className={runStatus === 'running' ? 'spin' : undefined}"), true);
  assert.equal(sidebarSource.includes('aria-label={runStatusLabel}'), true);
  assert.equal(sidebarSource.includes("queued: { label: '排队中', Icon: Clock3 }"), true);
  assert.equal(sidebarSource.includes("running: { label: '运行中', Icon: Loader2 }"), true);
  assert.equal(sidebarSource.includes("awaiting_input: { label: '需要输入', Icon: MessageCircleQuestion }"), true);
});
